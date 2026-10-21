"""A finite, explicitly requested pulse burst using the existing single action.

Short feedback checks gate execution, not scientific tip-quality acceptance.
There is no retry of an uncertain command and no autonomous campaign budget.
"""
import math
import time

from workflow_layer.feedback_observation import validate_feedback_observation_policy
from analysis_layer.signal_windows import analyze_signal_window


def feedback_brief(observation):
    metrics = observation.get("analysis", {}).get("metrics", {})
    return {"status": observation["status"],
            "current_median_a": metrics.get("current_median_a"),
            "current_difference_noise_a": metrics.get("current_difference_noise_a"),
            "current_rms_a": metrics.get("current_rms_a"),
            "current_rms_fraction": metrics.get("current_rms_fraction"),
            "current_abs_p95_a": metrics.get("current_abs_p95_a"),
            "current_setpoint_error_fraction": metrics.get("current_setpoint_error_fraction"),
            "z_median_m": metrics.get("z_median_m"),
            "z_drift_m_per_s": metrics.get("z_drift_m_per_s"),
            "error": observation.get("error"),
            "tip_quality": "not_accepted"}


def run_pulse_burst(device, observer, *, count, polarity, observation_policy,
                    pulse_voltage_v, duration_ms, settling_s, maximum_elapsed_s,
                    continue_on_quality_failure=False, stop_requested=lambda: False,
                    analysis_options=None, stop_on_signal_candidate=False,
                    baseline_s=.2,
                    clock=time.monotonic):
    """Requested count is a burst size, not an unrecoverable-tip threshold.

Every pulse retains the typed audit and independent observation. Continuing
after a completed but poor-quality observation must be explicitly selected.
An invalid/unknown state stops the burst. The typed action and observer own
physical protection; this loop must not override their decision on review.
"""
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("count must be a positive integer")
    if not math.isfinite(maximum_elapsed_s) or maximum_elapsed_s <= 0:
        raise ValueError("maximum_elapsed_s must be positive and finite")
    if not isinstance(continue_on_quality_failure, bool):
        raise ValueError("continue_on_quality_failure must be boolean")
    if not isinstance(stop_on_signal_candidate, bool):
        raise ValueError("stop_on_signal_candidate must be boolean")
    if set(analysis_options or {}) - {"current_full_scale_a", "analysis_limits"}:
        raise ValueError("Only existing signal analysis options are allowed")
    if stop_on_signal_candidate and (not analysis_options or any(
            analysis_options.get(k) is None for k in ("current_full_scale_a", "analysis_limits"))):
        raise ValueError("Candidate-driven stopping requires explicit complete signal criteria")
    if polarity not in {"positive", "negative"}:
        raise ValueError("Invalid pulse polarity")
    if not math.isfinite(pulse_voltage_v) or pulse_voltage_v <= 0:
        raise ValueError("pulse_voltage_v must be a positive magnitude")
    if not math.isfinite(settling_s) or settling_s < 0:
        raise ValueError("settling_s must be nonnegative and finite")
    if observation_policy.get("settling_s", settling_s) != settling_s:
        raise ValueError("Settling parameters must agree")
    policy = validate_feedback_observation_policy(observer, dict(observation_policy, settling_s=settling_s))
    if not math.isfinite(baseline_s) or baseline_s < 3 * policy["interval_s"]:
        raise ValueError("Pulse baseline needs at least three sample intervals")
    analyze_signal_window([], minimum_duration_s=policy["interval_s"],
        maximum_sample_gap_s=policy["maximum_sample_gap_s"], **(analysis_options or {}))
    # Validate all physical parameters before the first pulse; each real action
    # will independently recheck authorization, state, stop marker and bounds.
    device.safety_gate.require("bias_pulse", dict(
        target_bias_v=pulse_voltage_v if polarity == "positive" else -pulse_voltage_v,
        duration_ms=duration_ms))
    reserve = baseline_s + policy["duration_s"] + duration_ms / 1000
    if maximum_elapsed_s < reserve:
        raise ValueError("Burst time budget cannot contain one requested check")
    report = {"status": "running", "requested_count": count, "completed_count": 0,
              "pulses": [], "quick_checks": [], "protective_errors": [],
              "tip_quality": "awaiting_imaging_and_spectroscopy"}
    started = clock()

    try:
        for index in range(count):
            if stop_requested():
                report["status"] = "cancelled"
                break
            if clock() - started + reserve > maximum_elapsed_s:
                report["status"] = "burst_time_budget_reached"
                break
            # Local import avoids a registry cycle. Each full observation is
            # durably audited before the next pulse, even if a later one fails.
            from workflow_layer.control_step import execute_control_step
            result = execute_control_step(device, {
                "action": "pulse_and_assess", "purpose": f"Requested finite burst pulse {index + 1}/{count}",
                "arguments": dict(polarity=polarity, observation_policy=policy,
                    pulse_voltage_v=pulse_voltage_v, duration_ms=duration_ms,
                    settling_s=settling_s, baseline_s=baseline_s,
                    analysis_options=analysis_options)}, observer=observer)["result"]
            report["pulses"].append(result)
            if "pulse" in result and result["pulse"] is None:
                report["status"] = "pulse_not_sent"
                break
            report["completed_count"] += 1
            brief = feedback_brief(result["observation"])
            assessment = result.get("assessment", {})
            brief["signal_assessment"] = assessment.get("status", "not_assessed")
            brief["threshold_failures"] = assessment.get("threshold_failures", [])
            brief["response"] = result.get("response")
            report["quick_checks"].append({"pulse": index + 1, **brief})
            if brief["status"] not in {"completed", "not_recovered", "unsettled"}:
                report["status"] = "state_fault"
                break
            if stop_on_signal_candidate and brief["status"] == "completed" and assessment.get("status") == "invalid_data":
                report["status"] = "invalid_assessment_evidence"
                break
            if stop_on_signal_candidate and result.get("decision") == "candidate_for_next_stage":
                report["status"] = "signal_candidate_for_review"
                break
            if brief["status"] != "completed" and not continue_on_quality_failure:
                report["status"] = "quality_review"
                break
        else:
            report["status"] = "burst_completed"
    except BaseException as error:
        report["status"] = "failed"
        error.acquired_evidence = (getattr(error, "acquired_evidence", None) or {}) | {"pulse_burst": report}
        raise
    finally:
        report["elapsed_s"] = clock() - started
    if (report["status"] in {"quality_review", "burst_completed", "burst_time_budget_reached"}
            and report["quick_checks"] and report["quick_checks"][-1]["status"] in {"not_recovered", "unsettled"}):
        report["status"] = "unresolved_quality_at_burst_end"
    elif stop_on_signal_candidate and report["status"] in {"burst_completed", "burst_time_budget_reached"}:
        report["status"] = "no_signal_candidate_within_budget"
    return report
