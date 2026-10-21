from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
import ast
from functools import wraps
import inspect
import math
from pathlib import Path
import re
import struct
import time
from typing import Any

from .action_events import (
    ActionEventEmitter,
    ActionEventSink,
    ActionIntent,
    ActionName,
)
from .backends import FakeNanonisBackend, NanonisBackend, OfficialNanonisBackend
from .backends import VersionQueryUnavailableError
from .capabilities import NanonisCapability, build_nanonis_capability_registry
from .config import NanonisConnectionConfig, load_connection_config
from .models import (
    AutoApproachResult,
    AtomTrackingProperties,
    BiasPulseRecipe,
    BiasSpectroscopyAcquisition,
    BiasSpectroscopyAdvancedSettings,
    BiasSpectroscopyData,
    BiasSpectroscopyProperties,
    BiasSpectroscopyRecipe,
    BiasSpectroscopySettings,
    BiasSpectroscopyTiming,
    ConditioningActionResult,
    CoarseMoveResult,
    CoarseXYDirection,
    DataLogChannel,
    DataLogChannels,
    DataLogProperties,
    DataLogRecipe,
    DataLogStatus,
    FollowMeMoveResult,
    FollowMeSpeed,
    FeedbackPokeRecipe,
    MotorPosition,
    PiezoRange,
    DriftCompensationState,
    DriftCompensationResult,
    LockInModulatorState,
    LockInDemodulatorState,
    LockInFilterState,
    NanonisStatusSnapshot,
    NanonisSpectrumData,
    NanonisSpectrumSettings,
    NanonisTipStateSnapshot,
    NanonisVersion,
    RecentDataFile,
    ScanAcquisitionResult,
    ScanBuffer,
    ScanFrame,
    ScanFrameData,
    ScanSpeed,
    ScanStartResult,
    ScanStatus,
    SignalReference,
    SignalValue,
    ZControllerStatus,
    ZPositionLimits,
    ZHomeProperties,
    ZHomeResult,
    TipShaperProperties,
    TipShaperRecipe,
    XYPosition,
    ZPokeRecipe,
)
from .safety import SafetyCheckResult, SafetyGate, build_default_safety_gate


class InstrumentPreconditionError(RuntimeError):
    """The observed instrument state does not permit the requested action."""


class InstrumentPostconditionError(RuntimeError):
    """The action did not produce the required read-back state."""


class _ZLimitViolation(InstrumentPreconditionError):
    """A finite readback outside enabled limits, distinct from a read failure."""


class NanonisCommandError(RuntimeError):
    """The official client returned a Nanonis-side command error."""


def _audited_action(
    action_name: ActionName,
    *,
    intent: ActionIntent = ActionIntent.EXECUTE,
    callback_arguments: tuple[str, ...] = (),
):
    """Wrap one typed device action without coupling the instrument layer to storage."""

    def decorate(function):
        signature = inspect.signature(function)

        @wraps(function)
        def wrapped(self, *args, **kwargs):
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            arguments = dict(bound.arguments)
            arguments.pop("self", None)
            for name in callback_arguments:
                arguments[name] = {"callback_supplied": arguments[name] is not None}
            return self.action_events.execute(
                action_name,
                arguments=arguments,
                operation=lambda: function(self, *args, **kwargs),
                source=f"NanonisDevice.{function.__name__}",
                intent=intent,
                fail_open_on_sink_error=(
                    intent is ActionIntent.PROTECTIVE
                ),
            )

        return wrapped

    return decorate


class NanonisDevice:
    """Safe high-level Nanonis interface for SPM lab automation."""

    def __init__(
        self,
        backend: NanonisBackend | None = None,
        *,
        config: NanonisConnectionConfig | None = None,
        live: bool = False,
        safety_gate: SafetyGate | None = None,
        action_event_sink: ActionEventSink | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        self.config = config or load_connection_config()
        self.safety_gate = safety_gate or build_default_safety_gate()
        self.action_events = ActionEventEmitter(action_event_sink)
        self._stop_requested = stop_requested
        self._paused_scan_context = None
        if backend is not None:
            self.backend = backend
        elif live or self.config.backend == "live":
            self.backend = OfficialNanonisBackend(self.config)
        else:
            self.backend = FakeNanonisBackend()

    def close(self) -> None:
        self.backend.close()

    def control_stop_requested(self) -> bool:
        """Cooperative cancellation shared by typed actions and their workflows."""
        return self._stop_requested is not None and bool(self._stop_requested())

    @property
    def is_fake_backend(self) -> bool:
        return isinstance(self.backend, FakeNanonisBackend)

    def __enter__(self) -> "NanonisDevice":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def get_version(self) -> NanonisVersion:
        try:
            response = _response_parts(self.backend.Util_VersionGet())
        except VersionQueryUnavailableError as exc:
            return NanonisVersion("", "", None, None, available=False, error=str(exc))
        product_line = _first_text(response)
        version = _second_text(response)
        ints = [int(value) for value in response if isinstance(value, int)]
        host_release = ints[-2] if len(ints) >= 2 else 0
        rt_release = ints[-1] if len(ints) >= 1 else 0
        return NanonisVersion(product_line, version, host_release, rt_release)

    def get_acquisition_period_s(self) -> float:
        return float(_response_parts(self.backend.Util_AcqPeriodGet())[0])

    def get_session_path(self) -> str | None:
        response = _response_parts(self.backend.Util_SessionPathGet())
        text = _first_text(response)
        return text or None

    def get_bias_v(self) -> float:
        return float(_response_parts(self.backend.Bias_Get())[0])

    @_audited_action(ActionName.BIAS_SET)
    def set_bias_v(
        self,
        target_bias_v: float,
        *,
        readback_tolerance_v: float = 1e-6,
    ) -> float:
        values = self._require_bounded_action(
            "bias_set",
            {
                "target_bias_v": target_bias_v,
                "readback_tolerance_v": readback_tolerance_v,
            },
        )
        self.backend.Bias_Set(values["target_bias_v"])
        readback_v = self.get_bias_v()
        if not math.isclose(
            readback_v,
            values["target_bias_v"],
            rel_tol=0.0,
            abs_tol=values["readback_tolerance_v"],
        ):
            raise InstrumentPostconditionError(
                f"bias read-back {readback_v:g} V does not match "
                f"target {values['target_bias_v']:g} V"
            )
        return readback_v

    def get_current_a(self) -> float:
        return float(_response_parts(self.backend.Current_Get())[0])

    def get_z_position_m(self) -> float:
        return float(_response_parts(self.backend.ZCtrl_ZPosGet())[0])

    def get_z_controller_status(self) -> ZControllerStatus:
        return ZControllerStatus.from_code(int(_response_parts(self.backend.ZCtrl_StatusGet())[0]))

    def get_z_controller_on(self) -> bool:
        return bool(int(_response_parts(self.backend.ZCtrl_OnOffGet())[0]))

    @_audited_action(ActionName.Z_CONTROLLER_SET, intent=ActionIntent.CONFIGURE)
    def set_z_controller_on(self, enabled: bool, *, timeout_ms: int = 1000) -> bool:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        values = self._require_bounded_action(
            "z_controller_set",
            {"enabled": int(enabled), "timeout_ms": timeout_ms},
        )
        # The RT controller may lag the acknowledged host command, including
        # the configured switch-off delay. Poll only; never resend the command.
        deadline = time.perf_counter() + values["timeout_ms"] / 1000.0
        _response_parts(self.backend.ZCtrl_OnOffSet(int(values["enabled"])))
        while True:
            if self.control_stop_requested():
                raise InstrumentPostconditionError("Z controller switch cancelled")
            readback = self.get_z_controller_on()
            if readback == bool(values["enabled"]):
                return readback
            if time.perf_counter() >= deadline:
                raise InstrumentPostconditionError(
                    f"Z controller read-back {readback!r} does not match target {bool(values['enabled'])!r}"
                )
            time.sleep(.01)

    def get_z_limits_enabled(self) -> bool:
        return bool(int(_response_parts(self.backend.ZCtrl_LimitsEnabledGet())[0]))

    def get_z_home_properties(self) -> ZHomeProperties:
        values = _response_parts(self.backend.ZCtrl_HomePropsGet())
        if (len(values) != 2 or type(values[0]) not in (int, float) or values[0] not in (0, 1)
                or isinstance(values[1], bool)):
            raise NanonisCommandError(f"Invalid ZCtrl.HomePropsGet response: {values!r}")
        try:
            position = float(values[1])
        except (TypeError, ValueError, OverflowError) as error:
            raise NanonisCommandError(f"Invalid ZCtrl.HomePropsGet response: {values!r}") from error
        if not math.isfinite(position):
            raise NanonisCommandError(f"Invalid ZCtrl.HomePropsGet position: {values!r}")
        return ZHomeProperties(bool(values[0]), position)

    @_audited_action(ActionName.Z_HOME)
    def home_z_controller(self, *, maximum_displacement_m: float) -> ZHomeResult:
        """Send one native Home using existing settings, then report current state.

        Home has no wait/stop argument. Acknowledgment does not establish settled
        contact; this method does not add feedback switching or automatic cleanup.
        """
        limit = self._require_bounded_action("z_home", {
            "maximum_displacement_m": maximum_displacement_m})["maximum_displacement_m"]
        self._require_idle_scan_configuration()
        if self.get_scan_status().raw_code != 0 or self._paused_scan_context is not None:
            raise InstrumentPreconditionError("Home requires a stopped, unpaused scan")
        if any(self.get_atom_tracking_status(control) for control in (0, 1, 2)):
            raise InstrumentPreconditionError("Stop Atom Tracking before Home")
        before = self.get_tip_state_snapshot()
        self._require_safe_tip_state(before)
        if ((before.z_controller_status, before.z_controller_on)
                not in {(ZControllerStatus.ON, True), (ZControllerStatus.OFF, False)}):
            raise InstrumentPreconditionError("Home requires a known stable feedback state")
        properties = self.get_z_home_properties()
        target = before.z_position_m + properties.position_m if properties.relative else properties.position_m
        delta = target - before.z_position_m
        if not math.isfinite(target) or abs(delta) > limit:
            raise InstrumentPreconditionError("Configured Home exceeds maximum_displacement_m")
        if not before.z_limits.low_limit_m <= target <= before.z_limits.high_limit_m:
            raise InstrumentPreconditionError("Configured Home target is outside read-back Z limits")
        parameters = {"maximum_displacement_m": limit, "delta_z_m": delta, "target_z_m": target}
        self._require_bounded_action("z_home", parameters)
        bias = self.get_bias_v()
        if not math.isfinite(bias):
            raise InstrumentPreconditionError("Home requires a finite bias readback")
        evidence = {"properties": properties, "tip_before": before, "target_z_m": target,
                    "bounded_parameters": parameters, "bias_before_v": bias,
                    "command_sent": False, "response_received": False}
        self.action_events.record_instantaneous(ActionName.INSTRUMENT_STATE_SNAPSHOT,
            arguments={"for_action": "z_home", "authorization": getattr(self.safety_gate, "authorization_evidence", None)},
            result=evidence, source="NanonisDevice.home_z_controller.precheck")
        if self.get_z_home_properties() != properties:
            raise InstrumentPreconditionError("Home settings changed before command")
        current_z = self.get_z_position_m()
        target = current_z + properties.position_m if properties.relative else properties.position_m
        delta = target - current_z
        if not math.isfinite(current_z) or not math.isfinite(target) or abs(delta) > limit:
            raise InstrumentPreconditionError("Home travel changed beyond the requested bound")
        if not before.z_limits.low_limit_m <= target <= before.z_limits.high_limit_m:
            raise InstrumentPreconditionError("Home target changed outside read-back Z limits")
        parameters.update(delta_z_m=delta, target_z_m=target)
        evidence.update(command_reference_z_m=current_z, target_z_m=target)
        self._require_bounded_action("z_home", parameters)
        try:
            evidence["command_sent"] = True
            _response_parts(self.backend.ZCtrl_Home())
            evidence["response_received"] = True
            after = self._conditioning_postcheck()
            evidence["tip_after"] = after
            self._require_idle_scan_configuration()
            if self.get_z_home_properties() != properties:
                raise InstrumentPostconditionError("Home settings changed during command")
            bias_after = self.get_bias_v()
            evidence["bias_after_v"] = bias_after
            if not math.isfinite(bias_after) or not math.isclose(bias_after, bias, rel_tol=0., abs_tol=1e-6):
                raise InstrumentPostconditionError("Bias changed during Home")
        except BaseException as error:
            error.acquired_evidence = evidence
            raise
        return ZHomeResult(properties, target, before, after)

    def get_z_limits(self) -> ZPositionLimits:
        high_limit_m, low_limit_m = _response_parts(self.backend.ZCtrl_LimitsGet())[:2]
        return ZPositionLimits(high_limit_m=float(high_limit_m), low_limit_m=float(low_limit_m))

    def get_tip_lift_m(self) -> float:
        return float(_response_parts(self.backend.ZCtrl_TipLiftGet())[0])

    def get_tip_state_snapshot(self) -> NanonisTipStateSnapshot:
        return NanonisTipStateSnapshot(
            z_controller_status=self.get_z_controller_status(),
            z_controller_on=self.get_z_controller_on(),
            z_position_m=self.get_z_position_m(),
            z_limits_enabled=self.get_z_limits_enabled(),
            z_limits=self.get_z_limits(),
            tip_lift_m=self.get_tip_lift_m(),
            current_a=self.get_current_a(),
        )

    def get_z_setpoint_a(self) -> float:
        return float(_response_parts(self.backend.ZCtrl_SetpntGet())[0])

    @_audited_action(ActionName.Z_SETPOINT_SET)
    def set_z_setpoint_a(
        self,
        target_setpoint_a: float,
        *,
        readback_tolerance_a: float = 1e-15,
    ) -> float:
        values = self._require_bounded_action(
            "z_setpoint_set",
            {
                "target_setpoint_a": target_setpoint_a,
                "readback_tolerance_a": readback_tolerance_a,
            },
        )
        self.backend.ZCtrl_SetpntSet(values["target_setpoint_a"])
        readback_a = self.get_z_setpoint_a()
        if not math.isclose(
            readback_a,
            values["target_setpoint_a"],
            rel_tol=0.0,
            abs_tol=values["readback_tolerance_a"],
        ):
            raise InstrumentPostconditionError(
                f"Z setpoint read-back {readback_a:g} A does not match "
                f"target {values['target_setpoint_a']:g} A"
            )
        return readback_a

    def get_auto_approach_running(self) -> bool:
        return bool(int(_response_parts(self.backend.AutoApproach_OnOffGet())[0]))

    @_audited_action(ActionName.AUTO_APPROACH_STOP, intent=ActionIntent.PROTECTIVE)
    def stop_auto_approach(self) -> None:
        _response_parts(self.backend.AutoApproach_OnOffSet(0))
        if self.get_auto_approach_running():
            raise InstrumentPostconditionError("Auto Approach remained running after stop")

    @_audited_action(ActionName.AUTO_APPROACH_START)
    def run_auto_approach(
        self,
        *,
        timeout_ms: int = 300000,
        poll_interval_ms: int = 100,
        completion_mode: str = "engaged",
    ) -> AutoApproachResult:
        """Run one bounded Auto Approach cycle under the active SafetyGate."""

        if completion_mode not in {"engaged", "withdrawn"}:
            raise ValueError("Auto Approach completion_mode must be engaged or withdrawn")

        values = self._require_bounded_action(
            "auto_approach",
            {
                "timeout_ms": timeout_ms,
                "poll_interval_ms": poll_interval_ms,
            },
        )
        if self.get_scan_status().running:
            raise InstrumentPreconditionError("Auto Approach requires the scan to be stopped")
        if self.get_bias_spectroscopy_status():
            raise InstrumentPreconditionError(
                "Auto Approach requires Bias Spectroscopy to be stopped"
            )
        if self.get_auto_approach_running():
            raise InstrumentPreconditionError("Auto Approach is already running")
        if self.get_z_controller_on():
            raise InstrumentPreconditionError(
                "Auto Approach requires the Z controller to be off before starting"
            )
        before = self.get_tip_state_snapshot()
        self._require_safe_tip_state(before)
        started_at = time.perf_counter()
        deadline = started_at + values["timeout_ms"] / 1000.0
        try:
            _response_parts(self.backend.AutoApproach_OnOffSet(1))
            while self.get_auto_approach_running():
                if self._stop_requested is not None and self._stop_requested():
                    raise InstrumentPostconditionError("Auto Approach cancelled by stop request")
                if time.perf_counter() >= deadline:
                    raise InstrumentPostconditionError("Auto Approach timed out")
                time.sleep(values["poll_interval_ms"] / 1000.0)
            after = self._conditioning_postcheck()
            if completion_mode == "engaged":
                if not after.z_controller_on or after.z_controller_status is not ZControllerStatus.ON:
                    raise InstrumentPostconditionError(
                        "Auto Approach ended without stable Z-controller ON status"
                    )
            elif (after.z_controller_on or after.z_controller_status is not ZControllerStatus.OFF
                    or not math.isclose(after.z_position_m, after.z_limits.high_limit_m,
                                        rel_tol=0.0, abs_tol=1e-12)):
                raise InstrumentPostconditionError("Auto Approach did not end withdrawn at the upper Z limit")
        except BaseException as error:
            for stop in (self.stop_auto_approach,):
                try:
                    stop()
                except Exception as stop_error:
                    error.add_note(f"protective stop failed: {stop_error}")
            raise
        return AutoApproachResult(
            elapsed_s=time.perf_counter() - started_at,
            tip_state_before=before,
            tip_state_after=after,
            bounded_parameters=values,
            completion_mode=completion_mode,
        )

    def get_scan_status(self) -> ScanStatus:
        raw_code = int(_response_parts(self.backend.Scan_StatusGet())[0])
        return ScanStatus(running=raw_code == 1, raw_code=raw_code)

    def get_scan_frame(self) -> ScanFrame:
        center_x, center_y, width, height, angle = _response_parts(self.backend.Scan_FrameGet())[:5]
        return ScanFrame(
            center_x_m=float(center_x),
            center_y_m=float(center_y),
            width_m=float(width),
            height_m=float(height),
            angle_deg=float(angle),
        )

    def get_piezo_range(self) -> PiezoRange:
        values = _response_parts(self.backend.Piezo_RangeGet())
        return PiezoRange(*(float(v) for v in values[:3]))

    def get_drift_compensation(self) -> DriftCompensationState:
        """Read drift status in SI units; seven-field servers omit the limit."""
        response = self.backend.Piezo_DriftCompGet()
        values = _response_parts(response)
        try:
            if len(values) not in (7, 8):
                raise ValueError("expected seven or eight fields")
            flags = [_binary_status(values[i], "Piezo.DriftCompGet") for i in (0, 4, 5, 6)]
            indices = (1, 2, 3, 7) if len(values) == 8 else (1, 2, 3)
            numeric = [float(values[i]) for i in indices]
            if any(isinstance(values[i], bool) for i in indices) or not all(
                    math.isfinite(v) for v in numeric) or (len(values) == 8 and not 0 < numeric[3] <= 100):
                raise ValueError("invalid velocity or saturation limit")
        except (ValueError, TypeError, OverflowError) as error:
            raw = bytes(response[1]).hex() if len(response) == 3 and isinstance(response[1], (bytes, bytearray)) else None
            raise NanonisCommandError(f"Invalid Piezo.DriftCompGet response: {values!r}; raw_body_hex={raw}") from error
        return DriftCompensationState(flags[0], *numeric[:3], *flags[1:], numeric[3] if len(values) == 8 else None)

    @_audited_action(ActionName.DRIFT_COMPENSATION_SET, intent=ActionIntent.CONFIGURE)
    def set_drift_compensation(self, *, enabled: bool, vx_m_per_s: float,
                              vy_m_per_s: float, vz_m_per_s: float,
                              saturation_limit_percent: float) -> DriftCompensationResult:
        """Configure persistent compensation; no measurement, offset reset or rearm.

        Live execution needs separate set AND stop authorization, allowing one
        bounded cleanup on a failed write/readback while control is retained.
        These are not commissioned defaults.
        """
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        request = dict(enabled=int(enabled), vx_m_per_s=vx_m_per_s, vy_m_per_s=vy_m_per_s,
                       vz_m_per_s=vz_m_per_s, saturation_limit_percent=saturation_limit_percent)
        values = self._require_bounded_action("drift_compensation_set", request)
        self._require_bounded_action("drift_compensation_stop",
                                     {"saturation_limit_percent": saturation_limit_percent})
        self._require_idle_scan_configuration()
        if self.get_scan_status().raw_code != 0 or self._paused_scan_context is not None:
            raise InstrumentPreconditionError("Drift configuration requires a stopped, unpaused scan")
        if any(self.get_atom_tracking_status(control) for control in (0, 1, 2)):
            raise InstrumentPreconditionError("Stop Atom Tracking before configuring drift compensation")
        tip = self.get_tip_state_snapshot()
        self._require_safe_tip_state(tip)
        if enabled and (not tip.z_controller_on or tip.z_controller_status is not ZControllerStatus.ON):
            raise InstrumentPreconditionError("Enabling drift requires stable Z feedback ON")
        before = self.get_drift_compensation()
        if before.saturation_limit_percent is None:
            raise InstrumentPreconditionError("This drift response omits the saturation limit; its write protocol is unverified")
        if enabled and before.saturated:
            raise InstrumentPreconditionError("Drift is saturated; explicit reconciliation is required, no automatic rearm")
        self.action_events.record_instantaneous(ActionName.INSTRUMENT_STATE_SNAPSHOT,
            arguments={"for_action": "drift_compensation_set", "requested": request,
                       "authorization": getattr(self.safety_gate, "authorization_evidence", None)},
            result={"drift": before, "tip": tip, "frame": self.get_scan_frame(),
                    "xy": self.get_follow_me_position()},
            source="NanonisDevice.set_drift_compensation.precheck")
        # Recheck cancellation/expiry immediately before the one requested write.
        self._require_bounded_action("drift_compensation_set", request)
        acquired_evidence = {"requested": request, "before": before, "tip_before": tip,
                             "write_sent": False, "response_received": False}
        try:
            # Set enum: 0=unchanged, 1=on, 2=off. Get uses 0/1.
            acquired_evidence["write_sent"] = True
            _response_parts(self.backend.Piezo_DriftCompSet(1 if enabled else 2,
                values["vx_m_per_s"], values["vy_m_per_s"], values["vz_m_per_s"],
                values["saturation_limit_percent"]))
            acquired_evidence["response_received"] = True
            if self.control_stop_requested():
                raise InstrumentPostconditionError("Drift configuration cancelled after write; no automatic cleanup")
            after = self.get_drift_compensation()
            acquired_evidence["after"] = after
            _require_drift_readback(after, enabled=enabled, **{k: v for k, v in values.items() if k != "enabled"})
            self._require_idle_scan_configuration()
            tip_after = self.get_tip_state_snapshot()
            acquired_evidence["tip_after"] = tip_after
            if self.control_stop_requested():
                raise InstrumentPostconditionError("Drift configuration cancelled during readback; no automatic cleanup")
            self._require_safe_tip_state(tip_after)
            if enabled and (not tip_after.z_controller_on or tip_after.z_controller_status is not ZControllerStatus.ON):
                raise InstrumentPostconditionError("Z feedback changed while enabling drift")
        except BaseException as error:
            error.acquired_evidence = acquired_evidence
            acquired_evidence["recovery_required"] = True
            # Do not repeat the unknown update, stop an unrelated scan, or treat
            # a software/readback failure as evidence for withdrawing the tip.
            # Explicit interruption hands control back without automatic writes.
            if isinstance(error, Exception):
                try:
                    acquired_evidence["cleanup_after"] = self.stop_drift_compensation(
                        saturation_limit_percent=saturation_limit_percent)
                except BaseException as stop_error:
                    acquired_evidence["cleanup_error"] = {"type": type(stop_error).__name__,
                                                          "message": str(stop_error)}
                    if not isinstance(stop_error, Exception):
                        acquired_evidence["cleanup_interrupted"] = True
                    error.add_note(f"Drift stop could not be confirmed: {stop_error}")
                # Preserve the existing measured Z-limit protection only. The
                # stop marker also takes precedence over this automatic action.
                if isinstance(error, _ZLimitViolation) and not acquired_evidence.get("cleanup_interrupted"):
                    try:
                        if not self.control_stop_requested():
                            self.withdraw_tip(wait=False)
                    except BaseException as protective_error:
                        acquired_evidence["withdraw_error"] = str(protective_error)
                        error.add_note(f"protective withdraw failed: {protective_error}")
            for name, read in (("drift_on_failure", self.get_drift_compensation),
                               ("tip_state_on_failure", self.get_tip_state_snapshot)):
                try:
                    acquired_evidence[name] = read()
                except BaseException as read_error:
                    acquired_evidence[name + "_error"] = {"type": type(read_error).__name__,
                                                          "message": str(read_error)}
                    error.add_note(f"{name} unavailable: {read_error}")
            raise
        return DriftCompensationResult(before, after)

    @_audited_action(ActionName.DRIFT_COMPENSATION_STOP, intent=ActionIntent.PROTECTIVE)
    def stop_drift_compensation(self, *, saturation_limit_percent: float | None = None) -> DriftCompensationState:
        """One bounded OFF/zero-speed request, even if busy, unless control stopped.

        Requires authorization, audit and a recognized pre-read layout. With no
        explicit limit, preserve the current modern limit or omit it on a legacy
        endpoint. Never issue an Offset or repeat an unconfirmed write.
        """
        request = {} if saturation_limit_percent is None else {"saturation_limit_percent": saturation_limit_percent}
        self._require_bounded_action("drift_compensation_stop", request)
        before = self.get_drift_compensation()
        if before.saturation_limit_percent is None and saturation_limit_percent is not None:
            raise InstrumentPreconditionError("Seven-field drift protocol cannot set a saturation limit; omit it to stop")
        if before.saturation_limit_percent is None and self.get_z_controller_on():
            raise InstrumentPreconditionError("This seven-field endpoint requires Z feedback OFF before switching drift compensation")
        limit = before.saturation_limit_percent if saturation_limit_percent is None else saturation_limit_percent
        request = {} if limit is None else {"saturation_limit_percent": limit}
        self.action_events.record_instantaneous(ActionName.INSTRUMENT_STATE_SNAPSHOT,
            arguments={"for_action": "drift_compensation_stop", "requested": request,
                       "authorization": getattr(self.safety_gate, "authorization_evidence", None)},
            result={"drift": before, "wire_layout": "four_arguments" if limit is None else "five_arguments"},
            source="NanonisDevice.stop_drift_compensation.precheck")
        self._require_bounded_action("drift_compensation_stop", request)
        _response_parts(self.backend.Piezo_DriftCompSet(2, 0., 0., 0., limit))
        after = self.get_drift_compensation()
        _require_drift_readback(after, enabled=False, vx_m_per_s=0., vy_m_per_s=0., vz_m_per_s=0.,
                                saturation_limit_percent=limit)
        return after

    def _require_idle_scan_configuration(self) -> None:
        if self.get_scan_status().running or self.get_auto_approach_running() or self.get_bias_spectroscopy_status():
            raise InstrumentPreconditionError("Scan configuration requires stopped scan, approach and spectroscopy")

    @_audited_action(ActionName.SCAN_FRAME_SET, intent=ActionIntent.CONFIGURE)
    def set_scan_frame(self, *, center_x_m: float, center_y_m: float, width_m: float,
                       height_m: float, angle_deg: float = 0.) -> ScanFrame:
        values = self._require_bounded_action("scan_frame_set", dict(center_x_m=center_x_m,
            center_y_m=center_y_m, width_m=width_m, height_m=height_m, angle_deg=angle_deg))
        self._require_idle_scan_configuration()
        frame = ScanFrame(**values)
        self.safety_gate.require_scan_frame_envelope(frame, self.get_piezo_range())
        speed = self.get_scan_speed()
        if speed.keep_parameter_constant == 1:
            target_forward_time = speed.forward_line_time_s
            target_backward_time = speed.backward_line_time_s
            target_forward_speed = frame.width_m / target_forward_time if target_forward_time > 0 else float("nan")
            target_backward_speed = frame.width_m / target_backward_time if target_backward_time > 0 else float("nan")
        elif speed.keep_parameter_constant == 0:
            target_forward_speed, target_backward_speed = speed.forward_m_per_s, speed.backward_m_per_s
            target_forward_time = frame.width_m / target_forward_speed if target_forward_speed > 0 else float("nan")
            target_backward_time = frame.width_m / target_backward_speed if target_backward_speed > 0 else float("nan")
        else:
            raise InstrumentPreconditionError("Unknown scan speed mode")
        # A size change can implicitly change line time or velocity. Check that
        # resulting speed without treating it as a separate write permission.
        self.safety_gate.require("scan_speed_set", dict(
            forward_line_time_s=target_forward_time, backward_line_time_s=target_backward_time,
            forward_m_per_s=target_forward_speed, backward_m_per_s=target_backward_speed))
        try:
            self._set_scan_frame(frame)  # Shared setter and readback; never start a scan here.
        except BaseException as error:
            try:
                self.stop_scan()
            except Exception as stop_error:
                error.add_note(f"protective scan stop failed: {stop_error}")
            raise
        return self.get_scan_frame()

    @_audited_action(ActionName.SCAN_SPEED_SET, intent=ActionIntent.CONFIGURE)
    def set_scan_line_times(self, *, forward_line_time_s: float, backward_line_time_s: float) -> ScanSpeed:
        self._require_idle_scan_configuration()
        frame = self.get_scan_frame()
        # Gate checks times before division and checks derived linear speeds too.
        preliminary = self.safety_gate.get_action("scan_speed_set")
        for parameter in preliminary.parameters:
            if parameter.name in {"forward_line_time_s", "backward_line_time_s"}:
                value = forward_line_time_s if parameter.name == "forward_line_time_s" else backward_line_time_s
                _, violation = parameter.validate("scan_speed_set", value)
                if violation:
                    from .safety import SafetyGateError
                    raise SafetyGateError(SafetyCheckResult("scan_speed_set", False, {}, (violation,)))
        values = self._require_bounded_action("scan_speed_set", dict(
            forward_line_time_s=forward_line_time_s, backward_line_time_s=backward_line_time_s,
            forward_m_per_s=frame.width_m/forward_line_time_s,
            backward_m_per_s=frame.width_m/backward_line_time_s))
        ratio = values["backward_m_per_s"] / values["forward_m_per_s"]
        try:
            # Set enum 2 means keep line time; Get enum 1 represents the same mode.
            _response_parts(self.backend.Scan_SpeedSet(values["forward_m_per_s"], values["backward_m_per_s"],
                values["forward_line_time_s"], values["backward_line_time_s"], 2, ratio))
            after = self.get_scan_speed()
            _require_scan_frame_close(self.get_scan_frame(), frame)
            for name, expected in values.items():
                if not math.isclose(getattr(after, name), expected, rel_tol=1e-5, abs_tol=1e-12):
                    raise InstrumentPostconditionError(f"Scan speed {name} did not match readback")
            if after.keep_parameter_constant != 1 or not math.isclose(after.speed_ratio, ratio, rel_tol=1e-5):
                raise InstrumentPostconditionError("Scan speed mode/ratio did not match readback")
        except BaseException as error:
            try:
                self.stop_scan()
            except Exception as stop_error:
                error.add_note(f"protective scan stop failed: {stop_error}")
            raise
        return after

    def get_lock_in_modulator_enabled(self, number: int = 1) -> bool:
        """Read only on/off, independently of optional phase-register configuration."""
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError("Modulator number must be a positive integer")
        values = _response_parts(self.backend.LockIn_ModOnOffGet(number))
        if len(values) != 1 or values[0] not in (0, 1):
            raise NanonisCommandError(f"Invalid lock-in on/off response: {values!r}")
        return bool(values[0])

    def get_lock_in_modulator(self, number: int = 1) -> LockInModulatorState:
        """Read modulator settings only; amplitude retains its selected signal's units.

        Frequency is read from the assigned phase register, not assumed to be
        the modulator number. Demodulator filters/phase and STS settings are separate.
        """
        values = self._read_lock_in_settings(number, (
            ("enabled", "LockIn_ModOnOffGet", lambda r: bool(_lock_in_int(r, 0, 1))),
            ("signal", "LockIn_ModSignalGet", self._lock_in_signal),
            ("amplitude", "LockIn_ModAmpGet", lambda r: _lock_in_float(r, minimum=0.)),
            ("phase_register", "LockIn_ModPhasRegGet", lambda r: _lock_in_int(r, 1, 8)),
            ("harmonic", "LockIn_ModHarmonicGet", lambda r: _lock_in_int(r, 1)),
            ("phase_deg", "LockIn_ModPhasGet", _lock_in_float),
        ))
        return LockInModulatorState(number=number, **values)

    def get_lock_in_demodulator(self, number: int = 1) -> LockInDemodulatorState:
        """Read the input, reference and filter chain; never change lock-in settings."""
        values = self._read_lock_in_settings(number, (
            ("signal", "LockIn_DemodSignalGet", self._lock_in_signal),
            ("harmonic", "LockIn_DemodHarmonicGet", lambda r: _lock_in_int(r, 1)),
            ("phase_register", "LockIn_DemodPhasRegGet", lambda r: _lock_in_int(r, 1, 8)),
            ("phase_deg", "LockIn_DemodPhasGet", _lock_in_float),
            ("high_pass", "LockIn_DemodHPFilterGet", _lock_in_filter),
            ("low_pass", "LockIn_DemodLPFilterGet", _lock_in_filter),
            ("sync_filter_enabled", "LockIn_DemodSyncFilterGet", lambda r: bool(_lock_in_int(r, 0, 1))),
            ("rt_signals", "LockIn_DemodRTSignalsGet", lambda r: ("X/Y", "R/phi")[_lock_in_int(r, 0, 1)]),
        ))
        return LockInDemodulatorState(number=number, **values)

    def get_lock_in_phase_frequency_hz(self, register: int) -> float:
        """Read one explicitly selected protocol register, without inferring its binding."""
        if type(register) is not int or not 1 <= register <= 8:
            raise ValueError("Phase register must be an integer from 1 to 8")
        response = _response_parts(self.backend.LockIn_ModPhasFreqGet(register))
        frequency = _lock_in_float(response, minimum=0.)
        if frequency == 0:
            raise NanonisCommandError(f"Invalid phase-register frequency: {response!r}")
        return frequency

    def _lock_in_signal(self, response):
        index = _lock_in_int(response, 0, 127)
        signal = self.resolve_signals([index])[0]
        if signal.error is not None or signal.index is None:
            raise NanonisCommandError(f"Unresolved lock-in signal {index}: {signal.error}")
        return signal

    def _read_lock_in_settings(self, number, fields):
        """Retain partial reads; bad enums do not hide independent settings.

        Only a completed command/validation error permits subsequent reads.
        Transport/parser failures stop immediately with the evidence obtained so far.
        """
        if type(number) is not int or number < 1:
            raise ValueError("Lock-in number must be a positive integer")
        evidence = {"number": number, "raw_fields": {}, "values": {}, "errors": {}}
        try:
            for field, command, decode in fields:
                try:
                    response = getattr(self.backend, command)(number)
                    # Preserve vendor error packets as well as decoded field values.
                    if len(response) == 3 and isinstance(response[1], (bytes, bytearray)):
                        evidence.setdefault("raw_body_hex", {})[field] = bytes(response[1]).hex()
                    parts = tuple(_response_parts(response))
                    evidence["raw_fields"][field] = parts
                    evidence["values"][field] = decode(parts)
                except NanonisCommandError as error:
                    evidence["errors"][field] = str(error)
            # A register outside 1-8 is retained, never shifted or queried as 0.
            if "phase_register" in evidence["values"]:
                try:
                    evidence["values"]["phase_register_frequency_hz"] = self.get_lock_in_phase_frequency_hz(
                        evidence["values"]["phase_register"])
                except NanonisCommandError as error:
                    evidence["errors"]["phase_register_frequency_hz"] = str(error)
            if evidence["errors"]:
                raise NanonisCommandError(f"Incomplete lock-in configuration: {evidence['errors']}")
            return evidence["values"]
        except Exception as error:
            error.acquired_evidence = evidence
            raise

    def get_scan_buffer(self) -> ScanBuffer:
        response = _response_parts(self.backend.Scan_BufferGet())
        return ScanBuffer(
            channel_indexes=_coerce_int_values(response[1]),
            pixels=int(response[2]),
            lines=int(response[3]),
        )

    def get_scan_speed(self) -> ScanSpeed:
        response = _response_parts(self.backend.Scan_SpeedGet())
        return ScanSpeed(
            forward_m_per_s=float(response[0]),
            backward_m_per_s=float(response[1]),
            forward_line_time_s=float(response[2]),
            backward_line_time_s=float(response[3]),
            keep_parameter_constant=int(response[4]),
            speed_ratio=float(response[5]),
        )

    def get_scan_xy_position(self, wait_for_newest_data: bool = False) -> XYPosition:
        response = _response_parts(self.backend.Scan_XYPosGet(1 if wait_for_newest_data else 0))
        return XYPosition(x_m=float(response[0]), y_m=float(response[1]))

    def get_follow_me_position(self, wait_for_newest_data: bool = False) -> XYPosition:
        response = _response_parts(self.backend.FolMe_XYPosGet(1 if wait_for_newest_data else 0))
        return XYPosition(x_m=float(response[0]), y_m=float(response[1]))

    def get_follow_me_speed(self) -> FollowMeSpeed:
        response = _response_parts(self.backend.FolMe_SpeedGet())
        return FollowMeSpeed(speed_m_per_s=float(response[0]), custom_speed=bool(int(response[1])))

    def get_signal_catalog(self) -> list[str]:
        response = _response_parts(self.backend.Signals_NamesGet())
        names = response[2] if len(response) >= 3 else response
        names = _coerce_signal_names(names)
        return [str(name) for name in names]

    def resolve_signals(self, names_or_indexes: Sequence[str | int]) -> list[SignalReference]:
        catalog = self.get_signal_catalog()
        references: list[SignalReference] = []

        for item in names_or_indexes:
            if isinstance(item, int):
                name = catalog[item] if 0 <= item < len(catalog) else None
                error = None if name is not None else "signal_index_out_of_range"
                references.append(SignalReference(requested=item, index=int(item), name=name, error=error))
                continue

            try:
                index = catalog.index(str(item))
            except ValueError:
                references.append(
                    SignalReference(requested=str(item), index=None, name=str(item), error="signal_not_found")
                )
            else:
                references.append(SignalReference(requested=str(item), index=index, name=str(item)))

        return references

    def read_resolved_signals(
        self,
        references: Sequence[SignalReference],
        wait_newest: bool = False,
    ) -> list[SignalValue]:
        read_items = [reference for reference in references if reference.error is None and reference.index is not None]
        if not read_items:
            return [
                SignalValue(
                    index=reference.index if reference.index is not None else -1,
                    name=reference.name,
                    value=None,
                    error=reference.error,
                )
                for reference in references
            ]

        indexes = [int(reference.index) for reference in read_items]
        response = _response_parts(self.backend.Signals_ValsGet(indexes, 1 if wait_newest else 0))
        raw_values = _coerce_signal_values(response[1] if len(response) > 1 else [])
        value_by_index = {
            int(reference.index): SignalValue(
                index=int(reference.index),
                name=reference.name,
                value=float(raw_values[pos]),
            )
            for pos, reference in enumerate(read_items)
            if pos < len(raw_values)
        }

        output: list[SignalValue] = []
        for reference in references:
            if reference.error is not None:
                output.append(
                    SignalValue(
                        index=reference.index if reference.index is not None else -1,
                        name=reference.name,
                        value=None,
                        error=reference.error,
                    )
                )
            elif reference.index is not None and reference.index in value_by_index:
                output.append(value_by_index[reference.index])
            elif reference.index is not None:
                output.append(
                    SignalValue(index=reference.index, name=reference.name, value=None, error="value_not_returned")
                )
        return output

    def read_signals(
        self,
        names_or_indexes: Sequence[str | int],
        wait_newest: bool = False,
    ) -> list[SignalValue]:
        return self.read_resolved_signals(self.resolve_signals(names_or_indexes), wait_newest=wait_newest)

    def get_data_log_channels(self) -> DataLogChannels:
        """Return Data Logger channels using the full 0..127 RT signal index space."""

        response = _response_parts(self.backend.DataLog_ChsGet())
        expected = int(response[0])
        indexes = _coerce_int_values(response[1] if len(response) > 1 else ())
        if len(indexes) != expected:
            raise ValueError(f"DataLog.ChsGet returned {len(indexes)} indexes; expected {expected}")
        catalog = self.get_signal_catalog()
        return DataLogChannels(
            tuple(
                DataLogChannel(index=index, name=catalog[index] if index < len(catalog) else None)
                for index in indexes
            )
        )

    def get_data_log_properties(self) -> DataLogProperties:
        response = _response_parts(self.backend.DataLog_PropsGet())
        duration_s = int(response[1]) * 3600.0 + int(response[2]) * 60.0 + float(response[3])
        return DataLogProperties(
            timed=bool(int(response[0])),
            duration_s=duration_s,
            averaging=int(response[4]),
            basename=_coerce_text(response[6]),
            comment=_coerce_text(response[8]),
        )

    def get_data_log_status(self) -> DataLogStatus:
        response = _response_parts(self.backend.DataLog_StatusGet())
        elapsed_s = int(response[2]) * 3600.0 + int(response[3]) * 60.0 + float(response[4])
        saved_file_path = _coerce_text(response[8])
        return DataLogStatus(
            start_time=_coerce_text(response[1]),
            elapsed_s=elapsed_s,
            stop_time=_coerce_text(response[6]),
            saved_file_path=saved_file_path or None,
            saved_points=int(response[9]),
        )

    @_audited_action(ActionName.DATA_LOG_CONFIGURE, intent=ActionIntent.CONFIGURE)
    def configure_data_log(self, recipe: DataLogRecipe) -> tuple[DataLogChannels, DataLogProperties]:
        """Configure a lossless Data Logger profile; live use remains SafetyGate-disabled."""

        _validate_data_log_recipe(recipe)
        references = self.resolve_signals(recipe.signals)
        errors = [reference for reference in references if reference.error is not None or reference.index is None]
        if errors:
            details = ", ".join(f"{item.requested!r}: {item.error}" for item in errors)
            raise ValueError(f"Data Logger signals could not be resolved: {details}")
        indexes = [int(reference.index) for reference in references if reference.index is not None]
        if len(set(indexes)) != len(indexes):
            raise ValueError("Data Logger signals must resolve to distinct RT indexes")

        values = self._require_bounded_action(
            "data_log_configure",
            {
                "timed": int(recipe.timed),
                "duration_s": recipe.duration_s,
                "averaging": recipe.averaging,
                "channel_count": len(indexes),
                "minimum_channel_index": min(indexes),
                "maximum_channel_index": max(indexes),
            },
        )
        hours, remainder = divmod(values["duration_s"], 3600.0)
        minutes, seconds = divmod(remainder, 60.0)
        self.backend.DataLog_ChsSet(indexes)
        self.backend.DataLog_PropsSet(
            2 if recipe.timed else 1,
            int(hours),
            int(minutes),
            float(seconds),
            int(values["averaging"]),
            recipe.basename,
            recipe.comment,
            list(recipe.modules),
        )

        channels = self.get_data_log_channels()
        properties = self.get_data_log_properties()
        if channels.indexes != tuple(indexes):
            raise InstrumentPostconditionError("Data Logger channel read-back does not match the requested indexes")
        if properties.averaging != 1:
            raise InstrumentPostconditionError("Data Logger averaging read-back is not one")
        if properties.timed != recipe.timed:
            raise InstrumentPostconditionError("Data Logger acquisition-mode read-back does not match")
        return channels, properties

    @_audited_action(ActionName.DATA_LOG_START)
    def start_data_log(self) -> DataLogStatus:
        """Start Data Logger after revalidating its lossless configuration."""

        channels = self.get_data_log_channels()
        properties = self.get_data_log_properties()
        self._require_bounded_action(
            "data_log_start",
            {
                "averaging": properties.averaging,
                "channel_count": len(channels.channels),
            },
        )
        self.backend.DataLog_Start()
        return self.get_data_log_status()

    @_audited_action(ActionName.DATA_LOG_STOP, intent=ActionIntent.PROTECTIVE)
    def stop_data_log(self) -> DataLogStatus:
        """Protective stop for Data Logger file acquisition."""

        self.backend.DataLog_Stop()
        return self.get_data_log_status()

    def grab_scan_frame(self, channel_index: int, direction: str = "forward") -> ScanFrameData:
        direction_code = _direction_to_code(direction)
        response = _response_parts(self.backend.Scan_FrameDataGrab(int(channel_index), direction_code))
        return ScanFrameData(
            channel_index=int(channel_index),
            channel_name=str(response[1]),
            direction="forward" if direction_code == 1 else "backward",
            rows=int(response[2]),
            columns=int(response[3]),
            data=response[4],
            raw_scan_direction=int(response[5]),
        )

    def get_spectrum_analyzer_channel(self, instance: int = 1) -> int:
        instance = _validate_spectrum_analyzer_instance(instance)
        return int(_response_parts(self.backend.SpectrumAnlzr_ChGet(instance))[0])

    def get_spectrum_analyzer_frequency_ranges(self, instance: int = 1) -> tuple[int, tuple[float, ...]]:
        instance = _validate_spectrum_analyzer_instance(instance)
        response = _response_parts(self.backend.SpectrumAnlzr_FreqRangeGet(instance))
        return int(response[0]), _counted_float_values("SpectrumAnlzr.FreqRangeGet", response)

    def get_spectrum_analyzer_frequency_resolutions(
        self,
        instance: int = 1,
    ) -> tuple[int, tuple[float, ...]]:
        instance = _validate_spectrum_analyzer_instance(instance)
        response = _response_parts(self.backend.SpectrumAnlzr_FreqResGet(instance))
        return int(response[0]), _counted_float_values("SpectrumAnlzr.FreqResGet", response)

    def get_spectrum_analyzer_fft_window(self, instance: int = 1) -> int:
        instance = _validate_spectrum_analyzer_instance(instance)
        return int(_response_parts(self.backend.SpectrumAnlzr_FFTWindowGet(instance))[0])

    def get_spectrum_analyzer_averaging(self, instance: int = 1) -> tuple[int, int, int]:
        instance = _validate_spectrum_analyzer_instance(instance)
        response = _response_parts(self.backend.SpectrumAnlzr_AveragGet(instance))
        return int(response[0]), int(response[1]), int(response[2])

    def get_spectrum_analyzer_ac_coupling(self, instance: int = 1) -> bool:
        instance = _validate_spectrum_analyzer_instance(instance)
        return bool(int(_response_parts(self.backend.SpectrumAnlzr_ACCouplingGet(instance))[0]))

    def get_spectrum_analyzer_settings(self, instance: int = 1) -> NanonisSpectrumSettings:
        instance = _validate_spectrum_analyzer_instance(instance)
        range_index, frequency_ranges = self.get_spectrum_analyzer_frequency_ranges(instance)
        resolution_index, frequency_resolutions = self.get_spectrum_analyzer_frequency_resolutions(instance)
        averaging_mode, weighting_mode, averaging_count = self.get_spectrum_analyzer_averaging(instance)
        return NanonisSpectrumSettings(
            instance=instance,
            channel_index=self.get_spectrum_analyzer_channel(instance),
            frequency_range_index=range_index,
            frequency_ranges_hz=frequency_ranges,
            frequency_resolution_index=resolution_index,
            frequency_resolutions_hz=frequency_resolutions,
            fft_window_index=self.get_spectrum_analyzer_fft_window(instance),
            averaging_mode=averaging_mode,
            weighting_mode=weighting_mode,
            averaging_count=averaging_count,
            ac_coupling=self.get_spectrum_analyzer_ac_coupling(instance),
        )

    def get_spectrum_analyzer_data(self, instance: int = 1) -> NanonisSpectrumData:
        instance = _validate_spectrum_analyzer_instance(instance)
        response = _response_parts(self.backend.SpectrumAnlzr_DataGet(instance))
        density = _counted_float_values("SpectrumAnlzr.DataGet", response, count_index=2, values_index=3)
        return NanonisSpectrumData(
            instance=instance,
            f0_hz=float(response[0]),
            df_hz=float(response[1]),
            density=density,
        )

    def get_bias_spectroscopy_status(self) -> bool:
        return bool(int(_response_parts(self.backend.BiasSpectr_StatusGet())[0]))

    def get_bias_spectroscopy_advanced_settings(self) -> BiasSpectroscopyAdvancedSettings:
        """Read inherited STS settings without changing them or starting spectroscopy."""
        flags = _response_parts(self.backend.BiasSpectr_AdvPropsGet())
        if len(flags) != 4 or any(value not in (0, 1) for value in flags):
            raise NanonisCommandError(f"Invalid BiasSpectr.AdvPropsGet flags: {flags!r}")
        mode_response = _response_parts(self.backend.BiasSpectr_MLSModeGet())
        if len(mode_response) != 2:
            raise NanonisCommandError(f"Invalid BiasSpectr.MLSModeGet response: {mode_response!r}")
        mode = _coerce_text(mode_response[1])
        if mode not in ("Linear", "MLS") or mode_response[0] != len(mode):
            raise NanonisCommandError(f"Unknown BiasSpectr sweep mode: {mode_response!r}")
        per_segment_value = None
        if mode == "MLS":
            per_segment = _response_parts(self.backend.BiasSpectr_MLSLockinPerSegGet())
            if len(per_segment) != 1 or per_segment[0] not in (0, 1):
                raise NanonisCommandError(f"Invalid BiasSpectr lock-in per segment flag: {per_segment!r}")
            per_segment_value = bool(per_segment[0])
        return BiasSpectroscopyAdvancedSettings(*(bool(value) for value in flags),
                                                mode, per_segment_value)

    @_audited_action(ActionName.BIAS_SPECTROSCOPY_LOCK_IN_CONFIGURE, intent=ActionIntent.CONFIGURE)
    def set_bias_spectroscopy_lock_in_run(self, enabled: bool) -> dict[str, Any]:
        """Configure auto-run only, never start/stop a modulator or alter feedback.

        OFF does not disable a modulator already running. Experiments claiming
        unmodulated acquisition must independently verify the relevant modulator.
        """
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        self._require_bounded_action("bias_spectroscopy_lock_in_configure", {"enabled": int(enabled)})
        if (self.get_scan_status().running or self.get_auto_approach_running()
                or self.get_bias_spectroscopy_status()):
            raise InstrumentPreconditionError("Lock-in Run configuration requires idle scan, approach and spectroscopy")
        tip = self.get_tip_state_snapshot()
        self._require_safe_tip_state(tip)
        if not tip.z_controller_on or tip.z_controller_status is not ZControllerStatus.ON:
            raise InstrumentPreconditionError("Lock-in Run configuration requires settled feedback ON")
        before = self.get_bias_spectroscopy_advanced_settings()
        if before.sweep_mode != "Linear" or not before.reset_bias or not before.z_controller_hold:
            raise InstrumentPreconditionError("Lock-in Run configuration requires Linear, reset bias and feedback hold")
        evidence = {"before": before, "requested_enabled": enabled, "command_sent": False}
        try:
            if self.control_stop_requested():
                raise InstrumentPreconditionError("Lock-in Run configuration cancelled")
            # 0 preserves each unrelated advanced property; 1/2 mean on/off.
            evidence["command_sent"] = True
            _response_parts(self.backend.BiasSpectr_AdvPropsSet(0, 0, 0, 1 if enabled else 2))
            after = self.get_bias_spectroscopy_advanced_settings()
            evidence["after"] = after
            if (after.lock_in_run != enabled or after.reset_bias != before.reset_bias
                    or after.z_controller_hold != before.z_controller_hold
                    or after.record_final_z != before.record_final_z
                    or after.sweep_mode != before.sweep_mode
                    or after.lock_in_per_segment != before.lock_in_per_segment):
                raise InstrumentPostconditionError("Lock-in Run or preserved advanced settings did not match readback")
        except BaseException as error:
            error.acquired_evidence = evidence
            # Unknown configuration is retained for independent readback, not
            # retried, reverted automatically, or treated as a contact hazard.
            raise
        return evidence

    def get_bias_spectroscopy_properties(self) -> BiasSpectroscopyProperties:
        response = _response_parts(self.backend.BiasSpectr_PropsGet())
        if len(response) != 12 or any(response[index] not in (0, 1) for index in (0, 2, 10, 11)):
            raise NanonisCommandError(f"Invalid BiasSpectr.PropsGet response: {response!r}")
        return BiasSpectroscopyProperties(
            save_all=bool(int(response[0])),
            number_of_sweeps=int(response[1]),
            backward_sweep=bool(int(response[2])),
            number_of_points=int(response[3]),
            parameter_names=_counted_text_values("BiasSpectr.PropsGet parameters", response, 5, 6),
            fixed_parameter_names=_counted_text_values("BiasSpectr.PropsGet fixed parameters", response, 8, 9),
            autosave=bool(int(response[10])),
            show_save_dialog=bool(int(response[11])),
        )

    def get_bias_spectroscopy_limits(self) -> tuple[float, float]:
        response = _response_parts(self.backend.BiasSpectr_LimitsGet())
        return float(response[0]), float(response[1])

    def get_bias_spectroscopy_channels(self) -> tuple[tuple[int, ...], tuple[str, ...]]:
        response = _response_parts(self.backend.BiasSpectr_ChsGet())
        if len(response) != 5:
            raise NanonisCommandError(f"Invalid BiasSpectr.ChsGet response: {response!r}")
        try:
            # The official parser returns singleton tuples; fake returns integers.
            indexes = tuple(_coerce_int_values(response[1]))
            names = _counted_text_values("BiasSpectr.ChsGet names", response, 3, 4)
            if len(indexes) != int(response[0]) or len(names) != len(indexes):
                raise ValueError("Channel indexes and names must match the reported count")
        except (TypeError, ValueError) as error:
            raise NanonisCommandError(f"Invalid BiasSpectr.ChsGet response: {response!r}") from error
        return indexes, names

    def get_bias_spectroscopy_timing(self) -> BiasSpectroscopyTiming:
        response = _response_parts(self.backend.BiasSpectr_TimingGet())
        return BiasSpectroscopyTiming(*[float(value) for value in response[:8]])

    def get_bias_spectroscopy_settings(self) -> BiasSpectroscopySettings:
        start_bias_v, end_bias_v = self.get_bias_spectroscopy_limits()
        indexes, names = self.get_bias_spectroscopy_channels()
        return BiasSpectroscopySettings(
            running=self.get_bias_spectroscopy_status(),
            properties=self.get_bias_spectroscopy_properties(),
            start_bias_v=start_bias_v,
            end_bias_v=end_bias_v,
            channel_indexes=indexes,
            channel_names=names,
            timing=self.get_bias_spectroscopy_timing(),
        )

    def get_tip_shaper_properties(self) -> TipShaperProperties:
        response = _response_parts(self.backend.TipShaper_PropsGet())
        return TipShaperRecipe(
            switch_off_delay_s=float(response[0]),
            change_bias=bool(int(response[1])),
            bias_v=float(response[2]),
            tip_lift_m=float(response[3]),
            lift_time_1_s=float(response[4]),
            bias_lift_v=float(response[5]),
            bias_settling_time_s=float(response[6]),
            lift_height_m=float(response[7]),
            lift_time_2_s=float(response[8]),
            end_wait_time_s=float(response[9]),
            restore_feedback=bool(int(response[10])),
        )

    def get_atom_tracking_status(self, control: int) -> bool:
        control = _validate_atom_tracking_control(control)
        return _binary_status(_response_parts(self.backend.AtomTrack_StatusGet(control))[0], "AtomTrack.StatusGet")

    def get_atom_tracking_properties(self) -> AtomTrackingProperties:
        response = _response_parts(self.backend.AtomTrack_PropsGet())
        return AtomTrackingProperties(
            integral_gain=float(response[0]),
            frequency_hz=float(response[1]),
            amplitude_m=float(response[2]),
            phase_deg=float(response[3]),
            switch_off_delay_s=float(response[4]),
        )

    def list_recent_data_files(
        self,
        patterns: Iterable[str] = ("*.sxm", "*.dat", "*.3ds"),
        limit: int = 20,
    ) -> list[RecentDataFile]:
        session_path = self.get_session_path()
        if not session_path:
            return []
        root = Path(session_path)
        if not root.exists():
            return []

        files: dict[Path, RecentDataFile] = {}
        for pattern in patterns:
            for path in root.glob(pattern):
                if path.is_file():
                    stat = path.stat()
                    files[path] = RecentDataFile(
                        path=path,
                        modified_time_s=stat.st_mtime,
                        size_bytes=stat.st_size,
                    )
        return sorted(files.values(), key=lambda item: item.modified_time_s, reverse=True)[: int(limit)]

    def get_status_snapshot(
        self,
        signal_names_or_indexes: Sequence[str | int] = ("Current (A)", "Z (m)", "Bias (V)"),
        recent_file_limit: int = 20,
    ) -> NanonisStatusSnapshot:
        return NanonisStatusSnapshot(
            version=self.get_version(),
            session_path=self.get_session_path(),
            bias_v=self.get_bias_v(),
            current_a=self.get_current_a(),
            z_position_m=self.get_z_position_m(),
            z_controller_status=self.get_z_controller_status(),
            z_setpoint_a=self.get_z_setpoint_a(),
            scan_status=self.get_scan_status(),
            scan_frame=self.get_scan_frame(),
            scan_buffer=self.get_scan_buffer(),
            signals=self.read_signals(signal_names_or_indexes),
            recent_files=self.list_recent_data_files(limit=recent_file_limit),
        )

    def validate_control_action(
        self,
        action: str,
        parameters: dict[str, Any],
        *,
        require_live_enabled: bool = False,
    ) -> SafetyCheckResult:
        return self.safety_gate.validate(
            action,
            parameters,
            require_live_enabled=require_live_enabled,
        )

    def validate_bias_spectroscopy_recipe(self, recipe: BiasSpectroscopyRecipe) -> SafetyCheckResult:
        """Validate the existing bounded recipe without issuing any instrument command."""
        return self.validate_control_action("bias_spectroscopy", _bias_spectroscopy_parameters(recipe))

    def get_capabilities(self) -> list[NanonisCapability]:
        return build_nanonis_capability_registry(safety_gate=self.safety_gate)

    def validate_tip_xy(self, target_x_m: float, target_y_m: float, *,
                        position_tolerance_fraction: float = 0.02,
                        require_live_enabled: bool = False) -> SafetyCheckResult:
        """Read the current frame and preflight the existing Follow Me bounds."""
        fractions = _scan_frame_fractions(self.get_scan_frame(), target_x_m, target_y_m)
        return self.validate_control_action("follow_me_move", {
            "frame_x_fraction": fractions[0], "frame_y_fraction": fractions[1],
            "position_tolerance_fraction": position_tolerance_fraction},
            require_live_enabled=require_live_enabled)

    @_audited_action(ActionName.FOLLOW_ME_MOVE)
    def move_tip_xy(
        self,
        target_x_m: float,
        target_y_m: float,
        *,
        wait: bool = True,
        position_tolerance_fraction: float = 0.02,
    ) -> FollowMeMoveResult:
        """Move to a physical point inside the current rotated scan frame and verify read-back."""

        if not wait:
            raise ValueError("audited Follow Me movement requires wait=True")
        frame = self.get_scan_frame()
        frame_x_fraction, frame_y_fraction = _scan_frame_fractions(
            frame,
            float(target_x_m),
            float(target_y_m),
        )
        values = self._require_bounded_action(
            "follow_me_move",
            {
                "frame_x_fraction": frame_x_fraction,
                "frame_y_fraction": frame_y_fraction,
                "position_tolerance_fraction": position_tolerance_fraction,
            },
        )
        if self.get_scan_status().running:
            raise InstrumentPreconditionError("Follow Me movement requires the scan to be stopped")
        self._require_safe_tip_state(self.get_tip_state_snapshot())
        before = self.get_follow_me_position(wait_for_newest_data=True)
        target = XYPosition(float(target_x_m), float(target_y_m))
        try:
            self.backend.FolMe_XYPosSet(target.x_m, target.y_m, 1 if wait else 0)
            after = self.get_follow_me_position(wait_for_newest_data=wait)
        except Exception:
            self.stop_follow_me()
            raise
        if wait:
            tolerance_m = values["position_tolerance_fraction"] * max(frame.width_m, frame.height_m)
            error_m = math.hypot(after.x_m - target.x_m, after.y_m - target.y_m)
            if error_m > tolerance_m:
                self.stop_follow_me()
                raise InstrumentPostconditionError(
                    f"Follow Me read-back error {error_m:g} m exceeds tolerance {tolerance_m:g} m"
                )
        return FollowMeMoveResult(
            requested_frame_x_fraction=frame_x_fraction,
            requested_frame_y_fraction=frame_y_fraction,
            target=target,
            position_before=before,
            position_after=after,
            bounded_parameters=values,
        )

    def acquire_bias_spectrum(self, recipe: BiasSpectroscopyRecipe) -> BiasSpectroscopyData:
        """Run one audited in-memory transaction and return only its spectroscopy data."""

        return self.acquire_bias_spectrum_transaction(recipe).data

    @_audited_action(ActionName.BIAS_SPECTROSCOPY_ACQUIRE, callback_arguments=("observation_ready",))
    def acquire_bias_spectrum_transaction(
        self,
        recipe: BiasSpectroscopyRecipe,
        *,
        observation_ready: Callable[[], bool] | None = None,
    ) -> BiasSpectroscopyAcquisition:
        """Acquire in memory, then restore the inherited save flags while still in control."""

        values = self._require_bounded_action("bias_spectroscopy", _bias_spectroscopy_parameters(recipe))
        if not self.is_fake_backend and observation_ready is None:
            raise InstrumentPreconditionError("Live spectroscopy requires an independent active current guard")
        if self.get_scan_status().running:
            raise InstrumentPreconditionError("Bias Spectroscopy requires the scan to be stopped")
        settings_before = self.get_bias_spectroscopy_settings()
        if settings_before.running:
            raise InstrumentPreconditionError("Bias Spectroscopy is already running")
        tip_before = self.get_tip_state_snapshot()
        self._require_safe_tip_state(tip_before)
        if not tip_before.z_controller_on or tip_before.z_controller_status is not ZControllerStatus.ON:
            raise InstrumentPreconditionError("Bias Spectroscopy requires settled feedback ON")
        if self.get_auto_approach_running():
            raise InstrumentPreconditionError("Bias Spectroscopy requires Auto Approach to be stopped")
        advanced = self.get_bias_spectroscopy_advanced_settings()
        if advanced.sweep_mode != "Linear" or not advanced.reset_bias or not advanced.z_controller_hold:
            raise InstrumentPreconditionError("This bounded spectroscopy requires Linear, reset bias and feedback hold")
        references = self.resolve_signals(recipe.channel_indexes)
        if len(references) != len(recipe.channel_indexes) or any(r.error is not None for r in references):
            raise InstrumentPreconditionError("Bias Spectroscopy channels are unavailable in the current RT catalog")
        if not self.is_fake_backend and self.config.timeout_s <= recipe.estimated_duration_s:
            raise InstrumentPreconditionError("Connection timeout must exceed estimated spectroscopy duration")
        original_bias = self.get_bias_v()
        target_z_m = tip_before.z_position_m + recipe.timing.z_offset_m
        if tip_before.z_limits_enabled and not (
            tip_before.z_limits.low_limit_m <= target_z_m <= tip_before.z_limits.high_limit_m
        ):
            raise InstrumentPreconditionError("Bias Spectroscopy Z offset would exceed the enabled Z limits")

        acquired_evidence = {"settings_before": settings_before, "advanced_settings": advanced,
            "tip_state_before": tip_before, "bias_before_v": original_bias,
            "start_sent": False, "response_received": False}
        try:
            _response_parts(self.backend.BiasSpectr_PropsSet(
                1 if recipe.save_all else 2,
                int(recipe.number_of_sweeps),
                1 if recipe.backward_sweep else 2,
                int(recipe.number_of_points),
                float(recipe.timing.z_offset_m),
                2,
                2,
            ))
            _response_parts(self.backend.BiasSpectr_LimitsSet(float(recipe.start_bias_v), float(recipe.end_bias_v)))
            _response_parts(self.backend.BiasSpectr_ChsSet([int(index) for index in recipe.channel_indexes]))
            timing = recipe.timing
            _response_parts(self.backend.BiasSpectr_TimingSet(
                timing.z_averaging_time_s,
                timing.z_offset_m,
                timing.initial_settling_time_s,
                timing.maximum_slew_rate_v_per_s,
                timing.settling_time_s,
                timing.integration_time_s,
                timing.end_settling_time_s,
                timing.z_control_time_s,
            ))
            settings_applied = self.get_bias_spectroscopy_settings()
            acquired_evidence["settings_applied"] = settings_applied
            _require_spectroscopy_readback(recipe, settings_applied)
            if self.control_stop_requested() or (observation_ready is not None and not observation_ready()):
                raise InstrumentPreconditionError("Spectroscopy cancelled before Start")
            acquired_evidence["start_sent"] = True
            response = _response_parts(self.backend.BiasSpectr_Start(1, ""))
            acquired_evidence.update(response_received=True, raw_response_parts=response)
            names = _counted_text_values("BiasSpectr.Start channels", response, 1, 2)
            rows = int(response[3])
            columns = int(response[4])
            data = _coerce_matrix("BiasSpectr.Start data", response[5], rows, columns)
            if rows != len(names) or len(set(names)) != len(names) or rows < 1 or columns < 1:
                raise InstrumentPostconditionError("Bias Spectroscopy returned invalid channel metadata")
            if any(not math.isfinite(value) for row in data for value in row):
                raise InstrumentPostconditionError("Bias Spectroscopy returned non-finite acquired data")
            parameters = _counted_float_values(
                "BiasSpectr.Start parameters",
                response,
                count_index=6,
                values_index=7,
            )
            if self.get_bias_spectroscopy_status():
                raise InstrumentPostconditionError("Bias Spectroscopy remained running after Start returned")
            deadline = time.perf_counter() + values["restoration_timeout_ms"] / 1000.0
            tip_after = self.get_tip_state_snapshot()
            restoration_states = [tip_after]
            acquired_evidence["restoration_states"] = restoration_states
            while not tip_after.z_controller_on or tip_after.z_controller_status is not ZControllerStatus.ON:
                self._require_safe_tip_state(tip_after)
                if (self.control_stop_requested() or (observation_ready is not None and not observation_ready())
                        or time.perf_counter() >= deadline or
                        math.isclose(tip_after.z_position_m, tip_after.z_limits.high_limit_m, rel_tol=0, abs_tol=1e-12)):
                    raise InstrumentPostconditionError("Bias Spectroscopy did not restore feedback within the bounded confirmation")
                time.sleep(min(0.02, max(0, deadline - time.perf_counter())))
                tip_after = self.get_tip_state_snapshot()
                restoration_states.append(tip_after)
            self._require_safe_tip_state(tip_after)
            if observation_ready is not None and not observation_ready():
                raise InstrumentPostconditionError("Independent spectroscopy current guard aborted")
            restored_bias = self.get_bias_v()
            if not math.isclose(restored_bias, original_bias, rel_tol=0.0, abs_tol=1e-6):
                raise InstrumentPostconditionError("Bias Spectroscopy did not restore bias")
            self._restore_spectroscopy_save_settings(settings_before.properties, acquired_evidence)
        except BaseException as error:
            # A postcondition failure must never discard an already returned spectrum.
            error.acquired_evidence = acquired_evidence
            # Configuration/serialization errors and delayed feedback restoration
            # are not evidence of dangerous contact. Stop only a possibly active
            # sweep; preserve the junction for an explicit, observed recovery.
            if acquired_evidence["start_sent"] and not self.control_stop_requested():
                try:
                    running = self.get_bias_spectroscopy_status()
                except Exception as status_error:
                    running = True
                    error.add_note(f"spectroscopy status unavailable: {status_error}")
                if running:
                    try:
                        if not self.control_stop_requested():
                            self.stop_bias_spectroscopy()
                    except Exception as stop_error:
                        error.add_note(f"spectroscopy stop failed: {stop_error}")
            if "save_settings_restoration" not in acquired_evidence:
                try:
                    self._restore_spectroscopy_save_settings(settings_before.properties, acquired_evidence)
                except Exception as restore_error:
                    error.add_note(f"spectroscopy save settings recovery required: {restore_error}")
            for name, read in (("tip_state_on_failure", self.get_tip_state_snapshot),
                               ("bias_on_failure_v", self.get_bias_v)):
                try:
                    acquired_evidence[name] = read()
                except Exception as stop_error:
                    error.add_note(f"{name} unavailable: {stop_error}")
            acquired_evidence["recovery_required"] = True
            raise
        return BiasSpectroscopyAcquisition(
            recipe=recipe,
            settings_before=settings_before,
            data=BiasSpectroscopyData(names, rows, columns, data, parameters),
            tip_state_before=tip_before,
            tip_state_after=tip_after,
            bounded_parameters=values,
            settings_applied=settings_applied,
            advanced_settings_before=advanced,
            bias_before_v=original_bias,
            bias_after_v=restored_bias,
            save_settings_restoration=acquired_evidence["save_settings_restoration"],
        )

    def _restore_spectroscopy_save_settings(self, original, acquired_evidence) -> None:
        """Restore only save flags, once, within the audited acquisition transaction."""
        evidence = {"status": "pending", "command_sent": False,
                    "autosave": original.autosave, "show_save_dialog": original.show_save_dialog}
        acquired_evidence["save_settings_restoration"] = evidence
        try:
            if self.control_stop_requested():
                evidence["status"] = "deferred_operator_takeover"
                raise InstrumentPostconditionError("Save settings restoration deferred: control stopped")
            if self.get_bias_spectroscopy_status():
                evidence["status"] = "deferred_sweep_running"
                raise InstrumentPostconditionError("Save settings restoration requires a stopped sweep")
            current = self.get_bias_spectroscopy_properties()
            evidence["properties_before_restore"] = current
            expected = replace(current, autosave=original.autosave,
                               show_save_dialog=original.show_save_dialog)
            if current == expected:
                evidence.update(status="unchanged", properties_after_restore=current)
                return
            # Do not overwrite a save preference changed externally during acquisition.
            if current.autosave or current.show_save_dialog:
                evidence["status"] = "deferred_external_change"
                raise InstrumentPostconditionError("Save settings changed outside this transaction")
            timing = self.get_bias_spectroscopy_timing()
            if not math.isfinite(timing.z_offset_m):
                raise InstrumentPostconditionError("Cannot preserve non-finite spectroscopy Z offset")
            if self.control_stop_requested():
                evidence["status"] = "deferred_operator_takeover"
                raise InstrumentPostconditionError("Save settings restoration deferred: control stopped")
            # Official PropsSet has no no-change sentinel for Z offset; preserve its readback.
            evidence["z_offset_m"] = timing.z_offset_m
            evidence["command_sent"] = True
            _response_parts(self.backend.BiasSpectr_PropsSet(
                0, 0, 0, 0, timing.z_offset_m,
                1 if original.autosave else 2, 1 if original.show_save_dialog else 2,
            ))
            after = self.get_bias_spectroscopy_properties()
            timing_after = self.get_bias_spectroscopy_timing()
            evidence.update(properties_after_restore=after, timing_after_restore=timing_after)
            if after != expected or timing_after != timing:
                raise InstrumentPostconditionError("Spectroscopy save settings restoration readback mismatch")
            evidence["status"] = "restored"
        except BaseException as error:
            if evidence["status"] == "pending":
                evidence["status"] = "failed"
            evidence["error"] = str(error)
            if evidence["command_sent"]:
                # Independent evidence after uncertain acknowledgment; never retry the write.
                try:
                    evidence["properties_on_failure"] = self.get_bias_spectroscopy_properties()
                except Exception as read_error:
                    evidence["readback_error"] = str(read_error)
            raise

    @_audited_action(
        ActionName.BIAS_SPECTROSCOPY_STOP,
        intent=ActionIntent.PROTECTIVE,
    )
    def stop_bias_spectroscopy(self) -> None:
        self.backend.BiasSpectr_Stop()

    @_audited_action(ActionName.ATOM_TRACKING_STOP, intent=ActionIntent.PROTECTIVE)
    def stop_atom_tracking(self, control: int) -> None:
        """Protective stop for modulation (0), controller (1), or drift measurement (2)."""

        self.backend.AtomTrack_CtrlSet(_validate_atom_tracking_control(control), 0)

    @_audited_action(ActionName.BIAS_PULSE)
    def apply_bias_pulse(self, target_bias_v: float, duration_ms: float) -> ConditioningActionResult:
        """Apply an absolute bias pulse while holding the Z controller."""

        values = self._require_bounded_action(
            "bias_pulse",
            {"target_bias_v": target_bias_v, "duration_ms": duration_ms},
        )
        before = self._conditioning_precheck(require_controller_on=True)
        original_bias = self.get_bias_v()
        _response_parts(self.backend.Bias_Pulse(
            1, values["duration_ms"] / 1000.0, values["target_bias_v"], 1, 2))
        after = self._conditioning_postcheck()
        if not after.z_controller_on or after.z_controller_status is not ZControllerStatus.ON:
            raise InstrumentPostconditionError("Bias Pulse did not restore stable feedback")
        if not math.isclose(self.get_bias_v(), original_bias, rel_tol=0.0, abs_tol=1e-6):
            raise InstrumentPostconditionError("Bias Pulse did not restore the original bias")
        return ConditioningActionResult("bias_pulse", before, after, values)

    @_audited_action(ActionName.Z_POKE)
    def run_z_poke(self, recipe: ZPokeRecipe) -> ConditioningActionResult:
        """Move Z by one bounded delta, dwell, and restore the original position."""

        values = self._require_bounded_action(
            "z_poke",
            {"delta_z_m": recipe.delta_z_m, "duration_ms": recipe.duration_ms},
        )
        before = self._conditioning_precheck(require_controller_on=False)
        if before.z_controller_on:
            raise InstrumentPreconditionError("ZCtrl.ZPosSet requires the Z controller to be off")
        target_z_m = before.z_position_m + values["delta_z_m"]
        if not before.z_limits_enabled:
            raise InstrumentPreconditionError("Z poke requires enabled Z limits")
        if not (before.z_limits.low_limit_m <= target_z_m <= before.z_limits.high_limit_m):
            raise InstrumentPreconditionError("Z poke target would exceed the enabled Z limits")
        moved = False
        try:
            self.backend.ZCtrl_ZPosSet(target_z_m)
            moved = True
            time.sleep(values["duration_ms"] / 1000.0)
        except Exception:
            self.withdraw_tip(wait=False)
            raise
        finally:
            if moved:
                try:
                    self.backend.ZCtrl_ZPosSet(before.z_position_m)
                except Exception:
                    self.withdraw_tip(wait=False)
                    raise
        after = self.get_tip_state_snapshot()
        self._require_safe_tip_state(after)
        if not math.isclose(after.z_position_m, before.z_position_m, rel_tol=0.0, abs_tol=1e-12):
            self.withdraw_tip(wait=False)
            raise InstrumentPostconditionError("Z poke did not restore the original Z position")
        return ConditioningActionResult("z_poke", before, after, values)

    @_audited_action(ActionName.TIP_SHAPER)
    def run_tip_shaper(
        self,
        recipe: TipShaperRecipe,
        *,
        wait: bool = True,
    ) -> ConditioningActionResult:
        if not wait:
            raise ValueError("audited Tip Shaper execution requires wait=True")
        return self._run_tip_shaper_transaction(
            recipe,
            action_label="tip_shaper",
        )

    @_audited_action(ActionName.Z_POKE)
    def run_feedback_poke(
        self,
        recipe: FeedbackPokeRecipe,
    ) -> ConditioningActionResult:
        """Run a feedback-managed poke without a bias discontinuity at the turn-around."""

        if recipe.approach_depth_m <= 0:
            raise ValueError("approach_depth_m must be positive")
        original_bias_v = self.get_bias_v()
        tip_shaper = recipe.to_tip_shaper_recipe(current_bias_v=original_bias_v)
        result = self._run_tip_shaper_transaction(
            tip_shaper,
            action_label="feedback_poke",
        )
        restored_bias_v = self.get_bias_v()
        if not math.isclose(restored_bias_v, original_bias_v, rel_tol=0.0, abs_tol=1e-6):
            raise InstrumentPostconditionError(
                "feedback-managed poke did not restore the original bias"
            )
        return result

    def _run_tip_shaper_transaction(
        self,
        recipe: TipShaperRecipe,
        *,
        action_label: str,
    ) -> ConditioningActionResult:
        """Shared transaction used by the generic Tip Shaper and leveled poke."""

        values = self._tip_shaper_bounded_values(recipe)
        before = self._conditioning_precheck(require_controller_on=True)
        timeout_ms = max(1, math.ceil(recipe.estimated_duration_s * 1000.0) + 5000)
        _response_parts(
            self.backend.TipShaper_PropsSet(
                values["switch_off_delay_s"],
                1 if values["change_bias"] else 2,
                values["bias_v"],
                values["tip_lift_m"],
                values["lift_time_1_s"],
                values["bias_lift_v"],
                values["bias_settling_time_s"],
                values["lift_height_m"],
                values["lift_time_2_s"],
                values["end_wait_time_s"],
                1 if values["restore_feedback"] else 2,
            )
        )
        # Recheck cancellation/authorization after configuration, before Start.
        self._tip_shaper_bounded_values(recipe)
        _response_parts(self.backend.TipShaper_Start(1, timeout_ms))
        after = self._conditioning_postcheck()
        if recipe.restore_feedback and (not after.z_controller_on
                or after.z_controller_status is not ZControllerStatus.ON):
            raise InstrumentPostconditionError("Tip Shaper did not restore stable feedback")
        return ConditioningActionResult(action_label, before, after, values)

    def _tip_shaper_bounded_values(self, recipe: TipShaperRecipe) -> dict[str, float]:
        return self._require_bounded_action(
            "tip_shaper",
            {
                "switch_off_delay_s": recipe.switch_off_delay_s,
                "change_bias": int(recipe.change_bias),
                "bias_v": recipe.bias_v,
                "tip_lift_m": recipe.tip_lift_m,
                "lift_time_1_s": recipe.lift_time_1_s,
                "bias_lift_v": recipe.bias_lift_v,
                "bias_settling_time_s": recipe.bias_settling_time_s,
                "lift_height_m": recipe.lift_height_m,
                "lift_time_2_s": recipe.lift_time_2_s,
                "end_wait_time_s": recipe.end_wait_time_s,
                "restore_feedback": int(recipe.restore_feedback),
                "estimated_duration_s": recipe.estimated_duration_s,
            },
        )

    @_audited_action(ActionName.SCAN_START)
    def start_scan(self, *, direction_up: bool = True, timeout_ms: int = 10000) -> ScanStartResult:
        """Start once, then await bounded confirmation of the current scan.

        This method deliberately has no frame, speed, buffer, or save-setting
        arguments.  Its only instrument write is ``Scan.Action(Start, direction)``.
        """

        values = self._require_bounded_action(
            "scan_start_current_frame",
            {"direction_up": int(direction_up), "timeout_ms": timeout_ms},
        )
        status_before = self.get_scan_status()
        if status_before.running:
            raise InstrumentPreconditionError("a scan is already running")
        if self.get_bias_spectroscopy_status():
            raise InstrumentPreconditionError(
                "cannot start a scan while Bias Spectroscopy is running"
            )
        tip_state = self.get_tip_state_snapshot()
        self._require_safe_tip_state(tip_state)
        if not tip_state.z_controller_on:
            raise InstrumentPreconditionError(
                "starting a scan requires the Z controller to be on"
            )

        configured_frame = self.get_scan_frame()
        configured_buffer = self.get_scan_buffer()
        configured_speed = self.get_scan_speed()
        self._paused_scan_context = None
        _response_parts(self.backend.Scan_Action(0, 1 if direction_up else 0))
        deadline = time.perf_counter() + values["timeout_ms"] / 1000.0
        try:
            status_after = self.get_scan_status()
            while not status_after.running:
                if self.control_stop_requested():
                    raise InstrumentPostconditionError("Scan start cancelled before confirmation")
                if not self.get_z_controller_on():
                    raise InstrumentPostconditionError("Feedback lost during scan start")
                if time.perf_counter() >= deadline:
                    raise InstrumentPostconditionError("Scan start was not confirmed before timeout")
                time.sleep(min(0.02, max(0, deadline - time.perf_counter())))
                status_after = self.get_scan_status()
        except BaseException as error:
            try:
                self.stop_scan(direction_up=direction_up)
            except Exception as stop_error:
                error.add_note(f"protective scan stop failed: {stop_error}")
            raise
        return ScanStartResult(
            direction="up" if direction_up else "down",
            status_before=status_before,
            status_after=status_after,
            configured_frame=configured_frame,
            configured_buffer=configured_buffer,
            configured_speed=configured_speed,
            bounded_parameters=values,
        )

    @_audited_action(ActionName.SCAN_PAUSE, intent=ActionIntent.PAUSE)
    def pause_scan(self, *, timeout_ms: int = 2000) -> dict[str, Any]:
        """Pause through Scan.Action(2), retaining an in-connection resume context.

        StatusGet cannot distinguish stopped from paused. The context therefore
        only certifies our own acknowledged pause; external control is not covered.
        """
        values = self._require_bounded_action("scan_pause", {"timeout_ms": timeout_ms})
        self._paused_scan_context = None
        if not self.get_scan_status().running:
            raise InstrumentPreconditionError("Pause requires a running scan")
        frame, buffer, speed = self.get_scan_frame(), self.get_scan_buffer(), self.get_scan_speed()
        if not buffer.channel_indexes:
            raise InstrumentPreconditionError("Pause requires a readable scan channel")
        direction = self.grab_scan_frame(buffer.channel_indexes[0], "forward").raw_scan_direction
        if direction not in (0, 1):
            raise InstrumentPreconditionError("Unknown scan direction")
        try:
            _response_parts(self.backend.Scan_Action(2, direction))
            deadline = time.perf_counter() + values["timeout_ms"] / 1000
            while True:
                status = self.get_scan_status()
                if status.raw_code == 0:
                    break
                if status.raw_code != 1 or time.perf_counter() >= deadline or self.control_stop_requested():
                    raise InstrumentPostconditionError("Scan pause could not be confirmed")
                time.sleep(.02)
        except BaseException:
            self.stop_scan(direction_up=bool(direction))
            raise
        self._paused_scan_context = (frame, buffer, speed, direction)
        return dict(status=status, frame=frame, buffer=buffer, speed=speed, direction=direction)

    @_audited_action(ActionName.SCAN_RESUME, intent=ActionIntent.RESUME)
    def resume_scan(self) -> dict[str, Any]:
        """Resume our paused raster; never replace Resume with Start."""
        self._require_bounded_action("scan_resume", {})
        context = self._paused_scan_context
        if context is None:
            raise InstrumentPreconditionError("Resume requires a confirmed pause on this connection")
        frame, buffer, speed, direction = context
        self._conditioning_precheck(require_controller_on=True)
        if self.get_auto_approach_running():
            raise InstrumentPreconditionError("Resume requires stopped Auto Approach")
        if (self.get_scan_frame(), self.get_scan_buffer(), self.get_scan_speed()) != (frame, buffer, speed):
            self._paused_scan_context = None
            raise InstrumentPreconditionError("Paused scan configuration changed; cannot resume")
        self._paused_scan_context = None
        try:
            _response_parts(self.backend.Scan_Action(3, direction))
            status = self.get_scan_status()
            if status.raw_code != 1:
                raise InstrumentPostconditionError("Scan resume did not return a running scan")
        except BaseException:
            self.stop_scan(direction_up=bool(direction))
            raise
        return dict(status=status, frame=frame, buffer=buffer, speed=speed, direction=direction)

    @_audited_action(ActionName.SCAN_ACQUIRE)
    def acquire_scan(
        self,
        channel_index: int,
        *,
        direction_up: bool = True,
        timeout_ms: int = 120000,
    ) -> ScanAcquisitionResult:
        """Acquire the currently configured frame; frame/speed and autosave settings are unchanged."""

        values = self._require_bounded_action(
            "scan_acquisition",
            {"direction_up": int(direction_up), "timeout_ms": timeout_ms},
        )
        if self.get_scan_status().running:
            raise InstrumentPreconditionError("A scan is already running")
        if self.get_bias_spectroscopy_status():
            raise InstrumentPreconditionError("A Bias Spectroscopy measurement is already running")
        configured_frame = self.get_scan_frame()
        try:
            self._paused_scan_context = None
            self.backend.Scan_Action(0, 1 if direction_up else 0)
            response = _response_parts(self.backend.Scan_WaitEndOfScan(int(values["timeout_ms"])))
        except Exception:
            self.stop_scan(direction_up=direction_up)
            raise
        timed_out = bool(int(response[0]))
        if timed_out or self.get_scan_status().running:
            self.stop_scan(direction_up=direction_up)
            raise InstrumentPostconditionError("scan acquisition timed out or remained running")
        file_path = str(response[2]) if len(response) > 2 and str(response[2]) else None
        frame_data = self.grab_scan_frame(channel_index, "forward")
        _require_scan_frame_close(self.get_scan_frame(), configured_frame)
        if self.get_scan_status().running:
            raise InstrumentPostconditionError("scan restarted while collecting frame data")
        return ScanAcquisitionResult(
            timed_out=False,
            file_path=file_path,
            frame_data=frame_data,
            bounded_parameters=values,
            configured_frame=configured_frame,
        )

    @_audited_action(ActionName.LOCAL_SCAN_ACQUIRE)
    def acquire_local_scan(
        self,
        channel_index: int,
        *,
        center_x_fraction: float = 0.0,
        center_y_fraction: float = 0.0,
        width_fraction: float = 0.25,
        height_fraction: float = 0.25,
        direction_up: bool = True,
        timeout_ms: int = 120000,
    ) -> ScanAcquisitionResult:
        """Acquire a contained subframe and restore the operator's original frame."""

        values = self._require_bounded_action(
            "local_scan_acquisition",
            {
                "center_x_fraction": center_x_fraction,
                "center_y_fraction": center_y_fraction,
                "width_fraction": width_fraction,
                "height_fraction": height_fraction,
                "direction_up": int(direction_up),
                "timeout_ms": timeout_ms,
            },
        )
        if (
            abs(values["center_x_fraction"]) + values["width_fraction"] > 1.0
            or abs(values["center_y_fraction"]) + values["height_fraction"] > 1.0
        ):
            raise InstrumentPreconditionError(
                "requested local scan subframe would extend outside the current frame"
            )
        if self.get_scan_status().running:
            raise InstrumentPreconditionError("a scan is already running")
        original = self.get_scan_frame()
        configured = _subframe_from_fractions(
            original,
            values["center_x_fraction"],
            values["center_y_fraction"],
            values["width_fraction"],
            values["height_fraction"],
        )
        try:
            self._set_scan_frame(configured)
            acquisition = self.acquire_scan(
                channel_index,
                direction_up=bool(values["direction_up"]),
                timeout_ms=int(values["timeout_ms"]),
            )
        except Exception:
            try:
                self.stop_scan(direction_up=direction_up)
            finally:
                self._set_scan_frame(original)
            raise
        self._set_scan_frame(original)
        restored = self.get_scan_frame()
        _require_scan_frame_close(restored, original)
        return ScanAcquisitionResult(
            timed_out=acquisition.timed_out,
            file_path=acquisition.file_path,
            frame_data=acquisition.frame_data,
            bounded_parameters=values,
            configured_frame=configured,
            restored_frame=restored,
        )

    def _set_scan_frame(self, frame: ScanFrame) -> None:
        _response_parts(
            self.backend.Scan_FrameSet(
                frame.center_x_m,
                frame.center_y_m,
                frame.width_m,
                frame.height_m,
                frame.angle_deg,
            )
        )
        _require_scan_frame_close(self.get_scan_frame(), frame)

    def _conditioning_precheck(self, *, require_controller_on: bool) -> NanonisTipStateSnapshot:
        if self.get_scan_status().running:
            raise InstrumentPreconditionError("conditioning requires the scan to be stopped")
        if self.get_bias_spectroscopy_status():
            raise InstrumentPreconditionError("conditioning requires Bias Spectroscopy to be stopped")
        snapshot = self.get_tip_state_snapshot()
        self._require_safe_tip_state(snapshot)
        if require_controller_on and (not snapshot.z_controller_on
                or snapshot.z_controller_status is not ZControllerStatus.ON):
            raise InstrumentPreconditionError("conditioning action requires stable Z-controller ON status")
        return snapshot

    def _conditioning_postcheck(self) -> NanonisTipStateSnapshot:
        if self.control_stop_requested():
            raise InstrumentPostconditionError("Control cancelled after action; no automatic cleanup")
        after = self.get_tip_state_snapshot()
        if self.control_stop_requested():
            raise InstrumentPostconditionError("Control cancelled during readback; no automatic cleanup")
        try:
            self._require_safe_tip_state(after)
        except _ZLimitViolation as error:
            try:
                self.withdraw_tip(wait=False)
            except Exception as stop_error:
                error.add_note(f"protective withdraw also failed: {stop_error}")
            raise
        return after

    @staticmethod
    def _require_safe_tip_state(snapshot: NanonisTipStateSnapshot) -> None:
        if snapshot.z_controller_status in {ZControllerStatus.SAFE_TIP, ZControllerStatus.WITHDRAWING}:
            raise InstrumentPreconditionError(f"unsafe Z controller state: {snapshot.z_controller_status.value}")
        if not math.isfinite(snapshot.z_position_m) or not math.isfinite(snapshot.current_a):
            raise InstrumentPreconditionError("tip state contains a non-finite Z position or current")
        if snapshot.z_limits_enabled and (not all(math.isfinite(v) for v in
                (snapshot.z_limits.low_limit_m, snapshot.z_limits.high_limit_m))
                or snapshot.z_limits.low_limit_m >= snapshot.z_limits.high_limit_m):
            raise InstrumentPreconditionError("enabled Z limits are invalid")
        if snapshot.z_limits_enabled and not (
            snapshot.z_limits.low_limit_m <= snapshot.z_position_m <= snapshot.z_limits.high_limit_m
        ):
            raise _ZLimitViolation("Z position is outside the enabled Z limits")

    def _require_bounded_action(self, action: str, parameters: dict[str, Any]) -> dict[str, float]:
        if self._stop_requested is not None and self._stop_requested():
            raise InstrumentPreconditionError("Control step cancelled before execution")
        values = self.safety_gate.require(
            action,
            parameters,
            require_live_enabled=not self.is_fake_backend,
        )
        if not self.is_fake_backend and self.action_events.sink is None:
            from .action_events import ActionAuditRequiredError

            raise ActionAuditRequiredError(
                f"{action} requires a durable action-event sink"
            )
        return values

    def get_motor_position(self, group: int = 1, *, timeout_ms: int = 500) -> MotorPosition:
        """Read one Motor Control group; raises when the PMD4 module has no API access."""

        group_index = _motor_group_index(group)
        if int(timeout_ms) < 1 or int(timeout_ms) > 5000:
            raise ValueError("Motor Control position timeout_ms must be in 1..5000")
        x_m, y_m, z_m = _response_parts(
            self.backend.Motor_PosGet(group_index, int(timeout_ms))
        )[:3]
        return MotorPosition(group=int(group), x_m=float(x_m), y_m=float(y_m), z_m=float(z_m))

    @_audited_action(ActionName.COARSE_XY_MOVE)
    def move_coarse_x_plus(self, number_of_steps: int, *, group: int = 1) -> CoarseMoveResult:
        return self._move_coarse_xy(CoarseXYDirection.X_PLUS, number_of_steps, group)

    @_audited_action(ActionName.COARSE_XY_MOVE)
    def move_coarse_x_minus(self, number_of_steps: int, *, group: int = 1) -> CoarseMoveResult:
        return self._move_coarse_xy(CoarseXYDirection.X_MINUS, number_of_steps, group)

    @_audited_action(ActionName.COARSE_XY_MOVE)
    def move_coarse_y_plus(self, number_of_steps: int, *, group: int = 1) -> CoarseMoveResult:
        return self._move_coarse_xy(CoarseXYDirection.Y_PLUS, number_of_steps, group)

    @_audited_action(ActionName.COARSE_XY_MOVE)
    def move_coarse_y_minus(self, number_of_steps: int, *, group: int = 1) -> CoarseMoveResult:
        return self._move_coarse_xy(CoarseXYDirection.Y_MINUS, number_of_steps, group)

    def validate_coarse_xy_move(self, direction: CoarseXYDirection | str, number_of_steps: int,
                               *, group: int = 1, require_live_enabled: bool = False) -> SafetyCheckResult:
        direction = CoarseXYDirection(direction)
        return self.validate_control_action("coarse_xy_move", {
            "direction_code": direction.nanonis_code, "number_of_steps": number_of_steps, "group": group,
        }, require_live_enabled=require_live_enabled)

    def validate_coarse_z_retract(self, number_of_steps: int, *, group: int = 1,
                                 require_live_enabled: bool = False) -> SafetyCheckResult:
        return self.validate_control_action("coarse_z_retract", {
            "direction_code": 4, "number_of_steps": number_of_steps, "group": group,
        }, require_live_enabled=require_live_enabled)

    @_audited_action(ActionName.COARSE_Z_RETRACT)
    def retract_coarse_z(self, number_of_steps: int, *, group: int = 1) -> CoarseMoveResult:
        """Request Z+ motor steps only; reviewed retreat sign, not live-commissioned clearance."""
        check = self.validate_coarse_z_retract(number_of_steps, group=group)
        check.raise_for_violations()
        return self._move_coarse("coarse_z_retract", "z_retract", check.normalized_parameters)

    def _move_coarse_xy(
        self,
        direction: CoarseXYDirection,
        number_of_steps: int,
        group: int,
    ) -> CoarseMoveResult:
        check = self.validate_coarse_xy_move(direction, number_of_steps, group=group)
        check.raise_for_violations()
        return self._move_coarse("coarse_xy_move", direction.value, check.normalized_parameters)

    def _coarse_motion_state(self) -> NanonisTipStateSnapshot:
        if self.get_scan_status().running:
            raise InstrumentPreconditionError("coarse motion requires the scan to be stopped")
        if self.get_bias_spectroscopy_status():
            raise InstrumentPreconditionError("coarse motion requires Bias Spectroscopy to be stopped")
        if self.get_auto_approach_running():
            raise InstrumentPreconditionError("coarse motion requires Auto Approach to be stopped")
        snapshot = self.get_tip_state_snapshot()
        self._require_safe_tip_state(snapshot)
        if snapshot.z_controller_on or snapshot.z_controller_status is not ZControllerStatus.OFF:
            raise InstrumentPreconditionError("coarse motion requires the Z controller to be stably off")
        return snapshot

    def _move_coarse(self, action: str, direction: str, parameters: dict[str, float]) -> CoarseMoveResult:
        values = self._require_bounded_action(action, parameters)
        before = self._coarse_motion_state()
        try:
            _response_parts(
                self.backend.Motor_StartMove(
                    int(values["direction_code"]),
                    int(values["number_of_steps"]),
                    _motor_group_index(int(values["group"])),
                    1,
                )
            )
            after = self._coarse_motion_state()
        except BaseException as error:
            try:
                self.stop_motor()
            except Exception as stop_error:
                error.add_note(f"motor stop also failed: {stop_error}")
            raise
        return CoarseMoveResult(
            direction=direction,
            nanonis_direction_code=int(values["direction_code"]),
            number_of_steps=int(values["number_of_steps"]),
            group=int(values["group"]),
            waited_until_finished=True,
            completion_evidence="command_returned_after_wait; physical displacement not read back",
            bounded_parameters=values,
            tip_state_before=before,
            tip_state_after=after,
        )

    @_audited_action(ActionName.SCAN_STOP, intent=ActionIntent.PROTECTIVE)
    def stop_scan(self, direction_up: bool = True) -> None:
        self._paused_scan_context = None
        self.backend.Scan_Action(1, 1 if direction_up else 0)

    @_audited_action(ActionName.FOLLOW_ME_STOP, intent=ActionIntent.PROTECTIVE)
    def stop_follow_me(self) -> None:
        self.backend.FolMe_Stop()

    @_audited_action(ActionName.MOTOR_STOP, intent=ActionIntent.PROTECTIVE)
    def stop_motor(self) -> None:
        self.backend.Motor_StopMove()

    @_audited_action(ActionName.WITHDRAW, intent=ActionIntent.PROTECTIVE)
    def withdraw_tip(self, wait: bool = False, timeout_ms: int = 5000) -> None:
        self.backend.ZCtrl_Withdraw(1 if wait else 0, int(timeout_ms))

    @_audited_action(ActionName.ALL_MOTION_STOP, intent=ActionIntent.PROTECTIVE)
    def stop_all_motion(self, withdraw: bool = False) -> None:
        self.stop_scan()
        self.stop_follow_me()
        self.stop_motor()
        if withdraw:
            self.withdraw_tip()


def _direction_to_code(direction: str) -> int:
    normalized = direction.lower()
    if normalized in {"forward", "fwd", "up"}:
        return 1
    if normalized in {"backward", "bwd", "down"}:
        return 0
    raise ValueError("direction must be 'forward' or 'backward'")


def _validate_spectrum_analyzer_instance(instance: int) -> int:
    instance = int(instance)
    if instance not in {1, 2}:
        raise ValueError("Spectrum Analyzer instance must be 1 or 2")
    return instance


def _validate_atom_tracking_control(control: int) -> int:
    control = int(control)
    if control not in {0, 1, 2}:
        raise ValueError("Atom Tracking control must be 0 (modulation), 1 (controller), or 2 (drift)")
    return control


def _motor_group_index(group: int) -> int:
    group = int(group)
    if group < 1 or group > 6:
        raise ValueError("Motor Control group must be in the 1..6 user-facing range")
    return group - 1


def _first_text(values: Sequence[Any]) -> str:
    for value in values:
        if isinstance(value, str):
            return value
    return ""


def _second_text(values: Sequence[Any]) -> str:
    seen_first = False
    for value in values:
        if isinstance(value, str):
            if seen_first:
                return value
            seen_first = True
    return ""


def _response_parts(response: Sequence[Any]) -> Sequence[Any]:
    if (
        len(response) >= 3
        and isinstance(response[0], str)
        and isinstance(response[1], (bytes, bytearray))
        and isinstance(response[2], list)
    ):
        if response[0].strip():
            raise NanonisCommandError(response[0].strip())
        return response[2]
    return response


def _lock_in_int(response, minimum, maximum=None):
    if (len(response) != 1 or type(response[0]) is not int or response[0] < minimum
            or (maximum is not None and response[0] > maximum)):
        raise NanonisCommandError(f"Invalid lock-in integer readback: {response!r}; expected {minimum}..{maximum}")
    return response[0]


def _lock_in_float(response, *, minimum=None):
    if (len(response) != 1 or isinstance(response[0], bool)
            or not isinstance(response[0], (int, float)) or not math.isfinite(response[0])
            or (minimum is not None and response[0] < minimum)):
        raise NanonisCommandError(f"Invalid lock-in numeric readback: {response!r}")
    return float(response[0])


def _lock_in_filter(response):
    if len(response) != 2:
        raise NanonisCommandError(f"Invalid lock-in filter layout: {response!r}")
    order = _lock_in_int(response[:1], 0, 8)
    cutoff = _lock_in_float(response[1:], minimum=0.)
    if order and cutoff == 0:
        raise NanonisCommandError(f"Active lock-in filter needs positive cutoff: {response!r}")
    return LockInFilterState(order, cutoff)


def _binary_status(value: Any, command: str) -> bool:
    if value not in (0, 1):
        raise NanonisCommandError(f"Invalid {command} boolean status: {value!r}")
    return bool(value)


def _require_drift_readback(actual: DriftCompensationState, *, enabled: bool,
                            vx_m_per_s: float, vy_m_per_s: float, vz_m_per_s: float,
                              saturation_limit_percent: float | None) -> None:
    if (actual.saturation_limit_percent is None) != (saturation_limit_percent is None):
        raise InstrumentPostconditionError("Drift response layout changed during write")
    if actual.enabled != enabled or (enabled and actual.saturated):
        raise InstrumentPostconditionError(f"Drift enable/saturation readback mismatch: {actual!r}")
    for name, expected in (("vx_m_per_s", vx_m_per_s), ("vy_m_per_s", vy_m_per_s),
                             ("vz_m_per_s", vz_m_per_s), ("saturation_limit_percent", saturation_limit_percent)):
        if expected is None:
            continue
        # Exact float32 wire round-trip, not an adjustable physical tolerance.
        wire_value = struct.unpack(">f", struct.pack(">f", expected))[0]
        if struct.unpack(">f", struct.pack(">f", getattr(actual, name)))[0] != wire_value:
            raise InstrumentPostconditionError(
                f"Drift {name} readback {getattr(actual, name)!r} differs from requested {expected!r}; {actual!r}")


def _coerce_signal_names(names: Any) -> list[str]:
    if isinstance(names, list):
        return [str(name) for name in names]
    if isinstance(names, tuple):
        return [str(name) for name in names]
    if isinstance(names, str):
        try:
            parsed = ast.literal_eval(names)
        except (SyntaxError, ValueError):
            return [names]
        if isinstance(parsed, list):
            return [str(name) for name in parsed]
    return [str(names)]


def _coerce_signal_values(values: Any) -> list[float]:
    output: list[float] = []
    for value in values:
        if isinstance(value, (list, tuple)) and len(value) == 1:
            value = value[0]
        output.append(float(value))
    return output


def _coerce_int_values(values: Any) -> list[int]:
    if hasattr(values, "tolist"):
        values = values.tolist()
    if not isinstance(values, (list, tuple)):
        values = [values]
    output: list[int] = []
    for value in values:
        while isinstance(value, (list, tuple)) and len(value) == 1:
            value = value[0]
        output.append(int(value))
    return output


def _coerce_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    while isinstance(value, (list, tuple)) and len(value) == 1:
        value = value[0]
    return str(value)


def _validate_data_log_recipe(recipe: DataLogRecipe) -> None:
    if not recipe.signals:
        raise ValueError("Data Logger requires at least one signal")
    if recipe.averaging != 1:
        raise ValueError("lossless Data Logger recording requires averaging=1")
    if recipe.timed and not 0.0 < float(recipe.duration_s) <= 86400.0:
        raise ValueError("timed Data Logger duration_s must be in (0, 86400]")
    if not recipe.timed and float(recipe.duration_s) != 0.0:
        raise ValueError("continuous Data Logger duration_s must be zero")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}", recipe.basename):
        raise ValueError("Data Logger basename must be 1-127 safe filename characters")
    if len(recipe.comment.encode("utf-8")) > 4096:
        raise ValueError("Data Logger comment must be at most 4096 UTF-8 bytes")
    for module in recipe.modules:
        if not module or any(ord(character) < 32 for character in module):
            raise ValueError("Data Logger module names must be non-empty and contain no control characters")


def _counted_float_values(
    command: str,
    response: Sequence[Any],
    *,
    count_index: int = 1,
    values_index: int = 2,
) -> tuple[float, ...]:
    expected_count = int(response[count_index])
    values = tuple(_coerce_signal_values(response[values_index]))
    if len(values) != expected_count:
        raise ValueError(f"{command} returned {len(values)} values; expected {expected_count}")
    return values


def _counted_text_values(
    command: str,
    response: Sequence[Any],
    count_index: int,
    values_index: int,
) -> tuple[str, ...]:
    expected_count = int(response[count_index])
    raw_values = response[values_index]
    values = tuple(str(value) for value in raw_values)
    if len(values) != expected_count:
        raise ValueError(f"{command} returned {len(values)} values; expected {expected_count}")
    return values


def _coerce_matrix(
    command: str,
    raw_data: Any,
    rows: int,
    columns: int,
) -> tuple[tuple[float, ...], ...]:
    data = tuple(tuple(float(value) for value in row) for row in raw_data)
    if len(data) != rows or any(len(row) != columns for row in data):
        shape = (len(data), tuple(len(row) for row in data))
        raise ValueError(f"{command} returned shape {shape}; expected ({rows}, {columns})")
    return data


def _scan_frame_fractions(frame: ScanFrame, x_m: float, y_m: float) -> tuple[float, float]:
    if frame.width_m <= 0.0 or frame.height_m <= 0.0:
        raise ValueError("scan frame width and height must be positive")
    angle_rad = math.radians(frame.angle_deg)
    dx = x_m - frame.center_x_m
    dy = y_m - frame.center_y_m
    # Nanonis defines positive frame angles as clockwise rotations.
    local_x = dx * math.cos(angle_rad) - dy * math.sin(angle_rad)
    local_y = dx * math.sin(angle_rad) + dy * math.cos(angle_rad)
    return 2.0 * local_x / frame.width_m, 2.0 * local_y / frame.height_m


def _subframe_from_fractions(
    frame: ScanFrame,
    center_x_fraction: float,
    center_y_fraction: float,
    width_fraction: float,
    height_fraction: float,
) -> ScanFrame:
    """Map normalized coordinates in a clockwise-rotated frame back to global XY."""

    angle_rad = math.radians(frame.angle_deg)
    local_x = center_x_fraction * frame.width_m / 2.0
    local_y = center_y_fraction * frame.height_m / 2.0
    dx = local_x * math.cos(angle_rad) + local_y * math.sin(angle_rad)
    dy = -local_x * math.sin(angle_rad) + local_y * math.cos(angle_rad)
    return ScanFrame(
        center_x_m=frame.center_x_m + dx,
        center_y_m=frame.center_y_m + dy,
        width_m=frame.width_m * width_fraction,
        height_m=frame.height_m * height_fraction,
        angle_deg=frame.angle_deg,
    )


def _require_scan_frame_close(actual: ScanFrame, expected: ScanFrame) -> None:
    fields = (
        "center_x_m",
        "center_y_m",
        "width_m",
        "height_m",
        "angle_deg",
    )
    for field_name in fields:
        actual_value = getattr(actual, field_name)
        expected_value = getattr(expected, field_name)
        tolerance = 1e-4 if field_name == "angle_deg" else 1e-15
        if not math.isclose(
            actual_value,
            expected_value,
            rel_tol=1e-5,
            abs_tol=tolerance,
        ):
            raise InstrumentPostconditionError(
                f"scan frame {field_name} read-back {actual_value:g} does not match "
                f"target {expected_value:g}"
            )


def _require_spectroscopy_readback(recipe: BiasSpectroscopyRecipe, settings: BiasSpectroscopySettings) -> None:
    props = settings.properties
    if (settings.running or settings.channel_indexes != recipe.channel_indexes or
            props.number_of_points != recipe.number_of_points or props.number_of_sweeps != recipe.number_of_sweeps or
            props.backward_sweep != recipe.backward_sweep or props.save_all != recipe.save_all or
            props.autosave or props.show_save_dialog):
        raise InstrumentPostconditionError("Bias Spectroscopy configuration readback mismatch")
    pairs = [(recipe.start_bias_v, settings.start_bias_v), (recipe.end_bias_v, settings.end_bias_v)]
    pairs.extend((getattr(recipe.timing, field), getattr(settings.timing, field))
                 for field in recipe.timing.__dataclass_fields__)
    if any(not math.isfinite(actual) or not math.isclose(expected, actual, rel_tol=1e-6, abs_tol=1e-12)
           for expected, actual in pairs):
        raise InstrumentPostconditionError("Bias Spectroscopy limit/timing readback mismatch")


def _bias_spectroscopy_parameters(recipe: BiasSpectroscopyRecipe) -> dict[str, float]:
    _validate_bias_spectroscopy_recipe(recipe)
    return {
        "start_bias_v": recipe.start_bias_v,
        "end_bias_v": recipe.end_bias_v,
        "z_offset_m": recipe.timing.z_offset_m,
        "number_of_sweeps": recipe.number_of_sweeps,
        "number_of_points": recipe.number_of_points,
        "channel_count": len(recipe.channel_indexes),
        "minimum_channel_index": min(recipe.channel_indexes),
        "maximum_channel_index": max(recipe.channel_indexes),
        "maximum_slew_rate_v_per_s": recipe.timing.maximum_slew_rate_v_per_s,
        "integration_time_s": recipe.timing.integration_time_s,
        "restoration_timeout_ms": recipe.restoration_timeout_ms,
        "estimated_duration_s": recipe.estimated_duration_s,
    }


def _validate_bias_spectroscopy_recipe(recipe: BiasSpectroscopyRecipe) -> None:
    if not recipe.channel_indexes:
        raise ValueError("Bias Spectroscopy requires at least one channel")
    if len(set(recipe.channel_indexes)) != len(recipe.channel_indexes):
        raise ValueError("Bias Spectroscopy channel indexes must be unique")
    if any(type(index) is not int for index in recipe.channel_indexes):
        raise ValueError("Bias Spectroscopy channel indexes must be integers")
    if type(recipe.number_of_points) is not int or type(recipe.number_of_sweeps) is not int:
        raise ValueError("Bias Spectroscopy point and sweep counts must be integers")
    if not isinstance(recipe.backward_sweep, bool) or not isinstance(recipe.save_all, bool):
        raise ValueError("Bias Spectroscopy backward_sweep and save_all must be booleans")
    timing_values = (
        recipe.timing.z_averaging_time_s,
        recipe.timing.initial_settling_time_s,
        recipe.timing.settling_time_s,
        recipe.timing.integration_time_s,
        recipe.timing.end_settling_time_s,
        recipe.timing.z_control_time_s,
    )
    if any(not math.isfinite(value) or value < 0.0 for value in timing_values):
        raise ValueError("Bias Spectroscopy timing values must be finite and non-negative")
    if not math.isfinite(recipe.timing.maximum_slew_rate_v_per_s) or recipe.timing.maximum_slew_rate_v_per_s <= 0:
        raise ValueError("Bias Spectroscopy maximum slew rate must be finite and positive")
