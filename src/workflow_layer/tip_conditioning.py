from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TypeAlias
import math

from analysis_layer.signal_windows import analyze_signal_window, compare_pulse_windows
from workflow_layer.feedback_observation import (observe_feedback, validate_feedback_observation_policy,
                                                 feedback_assessment_rows)

from analysis_layer import TipQualityAnalyzer, TipQualityAssessment, TipQualityDecision
from data_layer import TipConditioningRunRecord, TipConditioningRunState
from instrument_layer import (
    BiasPulseRecipe,
    ConditioningActionResult,
    FeedbackPokeRecipe,
    NanonisDevice,
    ScanAcquisitionResult,
    TipShaperRecipe,
    ZPokeRecipe,
)


TipConditioningAction: TypeAlias = (
    BiasPulseRecipe | FeedbackPokeRecipe | TipShaperRecipe | ZPokeRecipe
)


def pulse_and_assess(device, observer, *, polarity, observation_policy,
                     pulse_voltage_v=10., duration_ms=100., settling_s=0., baseline_s=.2, analysis_options=None,
                     stop_requested=lambda: False, progress=None):
    """One site-selected pulse and one fresh observation, never a retry loop.

    +/-10 V are the site's convenient presets, not physical optimality claims. Acquisition
    reuses one finite observer across baseline, command and post-action samples.
    duration_s is the post-action window; settling excludes only its prefix.
    An assessment candidate permits planning a next step, not automatic motion.
    """
    if polarity not in {"positive", "negative"}:
        raise ValueError("polarity must be positive or negative")
    if not math.isfinite(pulse_voltage_v) or pulse_voltage_v <= 0:
        raise ValueError("pulse_voltage_v must be a positive magnitude")
    if set(analysis_options or {}) - {"current_full_scale_a", "analysis_limits"}:
        raise ValueError("Only current_full_scale_a and analysis_limits may override assessment")
    if not math.isfinite(settling_s) or not 0 <= settling_s < observation_policy["duration_s"]:
        raise ValueError("settling_s must leave a nonempty observation window")
    # Validate the entire observation request before executing an irreversible pulse.
    if "settling_s" in observation_policy and observation_policy["settling_s"] != settling_s:
        raise ValueError("Pulse assessment and observation settling times must agree")
    observation_policy = validate_feedback_observation_policy(observer,
        dict(observation_policy, settling_s=settling_s))
    # Exercise analysis input validation before the action as well.
    analyze_signal_window([], minimum_duration_s=observation_policy["interval_s"],
                          maximum_sample_gap_s=observation_policy["maximum_sample_gap_s"],
                          **(analysis_options or {}))
    if (device is observer or device.backend is observer.backend
            or device.config.host != observer.config.host
            or (not device.is_fake_backend and device.config.port == observer.config.port)):
        raise ValueError("Pulse capture requires a distinct observer connection to the same host")
    if not observer.get_z_controller_on() or observer.get_scan_status().running:
        raise RuntimeError("Pulse assessment requires stationary engaged feedback")
    if stop_requested():
        raise RuntimeError("Conditioning cancelled before pulse")
    target = pulse_voltage_v if polarity == "positive" else -pulse_voltage_v
    device.safety_gate.require("bias_pulse", dict(target_bias_v=target, duration_ms=duration_ms))
    observation = observe_feedback(observer, **observation_policy,
        stop_requested=lambda: stop_requested() or device.control_stop_requested(), progress=progress,
        conditioning_action=lambda: device.apply_bias_pulse(target, duration_ms), baseline_s=baseline_s)
    action = observation.pop("pulse_result", None)
    result = {"pulse": action, "observation": observation, "decision": "stop_and_review",
              "tip_quality": "awaiting_imaging_and_spectroscopy", "settling_s": settling_s}
    rows = observation["rows"]
    phases = observation.get("row_phases", [])
    selected = feedback_assessment_rows(observation, settling_s)
    state_checks = observation.get("state_checks", [])
    result["response"] = compare_pulse_windows(
        [r for r, p in zip(rows, phases) if p == "before"],
        [r for r, p in zip(rows, phases) if p == "during"], selected,
        minimum_duration_s=observation_policy["interval_s"],
        maximum_sample_gap_s=observation_policy["maximum_sample_gap_s"],
        expected_current_a=state_checks[0]["setpoint_a"] if state_checks else None,
        current_full_scale_a=(analysis_options or {}).get("current_full_scale_a"))
    result["response"]["observation_status"] = observation["status"]
    if observation["status"] in {"aborted", "cancelled"}:
        result["response"]["comparison_valid"] = False
    if observation["status"] != "completed":
        return result
    result["assessment"] = analyze_signal_window(selected,
        minimum_duration_s=max(observation_policy["interval_s"],
            observation_policy["duration_s"] - settling_s - 2 * observation_policy["interval_s"]),
        maximum_sample_gap_s=observation_policy["maximum_sample_gap_s"],
        expected_current_a=state_checks[0]["setpoint_a"],
        z_limits_m=(observation["z_limits"]["low_limit_m"], observation["z_limits"]["high_limit_m"]),
        **(analysis_options or {}))
    if result["assessment"]["status"] == "stable_candidate":
        result["decision"] = "candidate_for_next_stage"
    elif result["assessment"]["status"] == "unstable":
        result["decision"] = "reassess_within_explicit_attempt_budget"
    else:
        result["decision"] = "insufficient_acceptance_evidence"
    return result


@dataclass(frozen=True)
class TipConditioningCommand:
    site_x_m: float
    site_y_m: float
    action: TipConditioningAction


@dataclass(frozen=True)
class TipConditioningCycleResult:
    precheck_scan: ScanAcquisitionResult
    precheck: TipQualityAssessment
    run_record: TipConditioningRunRecord
    postcheck_scan: ScanAcquisitionResult | None = None
    postcheck: TipQualityAssessment | None = None


class AutomaticTipConditioningWorkflow:
    """One conditioning action with explicit precheck and mandatory later postcheck."""

    def __init__(self, device: NanonisDevice, *, minimum_precheck_confidence: float) -> None:
        if not 0.0 <= minimum_precheck_confidence <= 1.0:
            raise ValueError("minimum_precheck_confidence must be between 0 and 1")
        self.device = device
        self.minimum_precheck_confidence = float(minimum_precheck_confidence)

    def execute_action(
        self,
        command: TipConditioningCommand,
        precheck: TipQualityAssessment,
    ) -> TipConditioningRunRecord:
        action_name = type(command.action).__name__
        if precheck.decision is not TipQualityDecision.DEGRADED:
            return TipConditioningRunRecord(
                action_name,
                TipConditioningRunState.PRECHECK_REJECTED,
                precheck.score,
                precheck.confidence,
                reason="tip_not_confirmed_degraded",
                site_x_m=command.site_x_m,
                site_y_m=command.site_y_m,
            )
        if precheck.confidence < self.minimum_precheck_confidence:
            return TipConditioningRunRecord(
                action_name,
                TipConditioningRunState.PRECHECK_REJECTED,
                precheck.score,
                precheck.confidence,
                reason="precheck_confidence_too_low",
                site_x_m=command.site_x_m,
                site_y_m=command.site_y_m,
            )
        if self.device.get_scan_status().running:
            return TipConditioningRunRecord(
                action_name,
                TipConditioningRunState.PRECHECK_REJECTED,
                precheck.score,
                precheck.confidence,
                reason="scan_is_running",
                site_x_m=command.site_x_m,
                site_y_m=command.site_y_m,
            )

        move_result = None
        try:
            move_result = self.device.move_tip_xy(command.site_x_m, command.site_y_m, wait=True)
            action_result = self._execute(command.action)
        except Exception as exc:
            self.device.stop_all_motion(withdraw=False)
            return TipConditioningRunRecord(
                action_name,
                TipConditioningRunState.ABORTED,
                precheck.score,
                precheck.confidence,
                reason=f"{type(exc).__name__}: {exc}",
                site_x_m=command.site_x_m,
                site_y_m=command.site_y_m,
                move_result=move_result,
            )

        return TipConditioningRunRecord(
            action_name,
            TipConditioningRunState.AWAITING_POSTCHECK,
            precheck.score,
            precheck.confidence,
            reason="action_completed_postcheck_required",
            site_x_m=command.site_x_m,
            site_y_m=command.site_y_m,
            move_result=move_result,
            action_result=action_result,
        )

    def run_once(
        self,
        command: TipConditioningCommand,
        analyzer: TipQualityAnalyzer,
        *,
        channel_index: int,
        minimum_score_improvement: float = 0.0,
        scan_timeout_ms: int = 120000,
    ) -> TipConditioningCycleResult:
        """Acquire precheck, execute at most one action, then force a fresh postcheck scan."""

        precheck_scan = self.device.acquire_scan(channel_index, timeout_ms=scan_timeout_ms)
        precheck = analyzer.assess(precheck_scan.frame_data)
        pending = self.execute_action(command, precheck)
        if pending.state is not TipConditioningRunState.AWAITING_POSTCHECK:
            return TipConditioningCycleResult(precheck_scan, precheck, pending)

        try:
            postcheck_scan = self.device.acquire_scan(channel_index, timeout_ms=scan_timeout_ms)
            postcheck = analyzer.assess(postcheck_scan.frame_data)
        except Exception as exc:
            self.device.stop_all_motion(withdraw=False)
            aborted = replace(
                pending,
                state=TipConditioningRunState.ABORTED,
                reason=f"postcheck_failed: {type(exc).__name__}: {exc}",
            )
            return TipConditioningCycleResult(precheck_scan, precheck, aborted)

        completed = self.complete_postcheck(
            pending,
            postcheck,
            minimum_score_improvement=minimum_score_improvement,
        )
        return TipConditioningCycleResult(
            precheck_scan,
            precheck,
            completed,
            postcheck_scan,
            postcheck,
        )

    def complete_postcheck(
        self,
        record: TipConditioningRunRecord,
        postcheck: TipQualityAssessment,
        *,
        minimum_score_improvement: float = 0.0,
    ) -> TipConditioningRunRecord:
        if record.state is not TipConditioningRunState.AWAITING_POSTCHECK:
            raise ValueError("only an awaiting_postcheck record can be completed")
        improved = postcheck.score - record.pre_score >= minimum_score_improvement
        succeeded = postcheck.decision is TipQualityDecision.GOOD and improved
        return replace(
            record,
            state=(
                TipConditioningRunState.SUCCEEDED
                if succeeded
                else TipConditioningRunState.FAILED_POSTCHECK
            ),
            post_score=postcheck.score,
            post_confidence=postcheck.confidence,
            reason="postcheck_passed" if succeeded else "postcheck_failed",
        )

    def _execute(self, action: TipConditioningAction) -> ConditioningActionResult:
        if isinstance(action, BiasPulseRecipe):
            return self.device.apply_bias_pulse(action.target_bias_v, action.duration_ms)
        if isinstance(action, FeedbackPokeRecipe):
            return self.device.run_feedback_poke(action)
        if isinstance(action, TipShaperRecipe):
            return self.device.run_tip_shaper(action, wait=True)
        if isinstance(action, ZPokeRecipe):
            return self.device.run_z_poke(action)
        raise TypeError(f"unsupported tip conditioning action: {type(action).__name__}")
