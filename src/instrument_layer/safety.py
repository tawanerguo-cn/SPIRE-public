from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import math
from typing import Any


@dataclass(frozen=True)
class SafetyViolation:
    action: str
    parameter: str | None
    code: str
    message: str
    value: Any = None
    minimum: float | None = None
    maximum: float | None = None
    unit: str | None = None


@dataclass(frozen=True)
class SafetyCheckResult:
    action: str
    ok: bool
    normalized_parameters: dict[str, float]
    violations: tuple[SafetyViolation, ...]

    def raise_for_violations(self) -> None:
        if not self.ok:
            raise SafetyGateError(self)


class SafetyGateError(ValueError):
    def __init__(self, result: SafetyCheckResult) -> None:
        self.result = result
        messages = "; ".join(violation.message for violation in result.violations)
        super().__init__(f"safety gate rejected {result.action!r}: {messages}")


@dataclass(frozen=True)
class NumericParameterSpec:
    name: str
    unit: str
    minimum: float | None = None
    maximum: float | None = None
    required: bool = True
    integer: bool = False
    description: str = ""

    def validate(self, action: str, value: Any) -> tuple[float | None, SafetyViolation | None]:
        if isinstance(value, bool):
            return None, self._violation(action, "not_numeric", value, "must be a number, not a boolean")
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None, self._violation(action, "not_numeric", value, "must be numeric")

        if not math.isfinite(numeric):
            return None, self._violation(action, "not_finite", value, "must be finite")
        if self.integer and not numeric.is_integer():
            return None, self._violation(action, "not_integer", value, "must be an integer")
        if self.minimum is not None and numeric < self.minimum:
            return None, self._violation(
                action,
                "below_minimum",
                value,
                f"must be >= {self.minimum:g} {self.unit}",
            )
        if self.maximum is not None and numeric > self.maximum:
            return None, self._violation(
                action,
                "above_maximum",
                value,
                f"must be <= {self.maximum:g} {self.unit}",
            )
        return numeric, None

    def _violation(self, action: str, code: str, value: Any, detail: str) -> SafetyViolation:
        return SafetyViolation(
            action=action,
            parameter=self.name,
            code=code,
            message=f"{self.name} {detail}",
            value=value,
            minimum=self.minimum,
            maximum=self.maximum,
            unit=self.unit,
        )


@dataclass(frozen=True)
class BoundedActionSpec:
    name: str
    description: str
    parameters: tuple[NumericParameterSpec, ...]
    live_enabled: bool = False

    def validate(
        self,
        parameters: Mapping[str, Any],
        *,
        require_live_enabled: bool = False,
    ) -> SafetyCheckResult:
        violations: list[SafetyViolation] = []
        normalized: dict[str, float] = {}
        specs = {parameter.name: parameter for parameter in self.parameters}

        if require_live_enabled and not self.live_enabled:
            violations.append(
                SafetyViolation(
                    action=self.name,
                    parameter=None,
                    code="action_not_live_enabled",
                    message=f"{self.name} is registered but not enabled for live execution",
                )
            )

        for name in parameters:
            if name not in specs:
                violations.append(
                    SafetyViolation(
                        action=self.name,
                        parameter=name,
                        code="unknown_parameter",
                        message=f"{name} is not an allowed parameter for {self.name}",
                        value=parameters[name],
                    )
                )

        for name, spec in specs.items():
            if name not in parameters:
                if spec.required:
                    violations.append(
                        SafetyViolation(
                            action=self.name,
                            parameter=name,
                            code="missing_required_parameter",
                            message=f"{name} is required for {self.name}",
                        )
                    )
                continue

            value, violation = spec.validate(self.name, parameters[name])
            if violation is not None:
                violations.append(violation)
            elif value is not None:
                normalized[name] = value

        return SafetyCheckResult(
            action=self.name,
            ok=not violations,
            normalized_parameters=normalized,
            violations=tuple(violations),
        )


class SafetyGate:
    def __init__(self, actions: Sequence[BoundedActionSpec]) -> None:
        self._actions = {action.name: action for action in actions}

    def list_actions(self) -> list[BoundedActionSpec]:
        return list(self._actions.values())

    def require_scan_frame_envelope(self, frame, piezo_range):
        """Veto rotated corners outside the read-back full XY piezo range.

        The caller must also validate the bounded action and live authorization.
        Station commissioning must verify the centered scanner coordinate convention.
        """
        if not all(math.isfinite(v) and v > 0 for v in (piezo_range.x_m, piezo_range.y_m)):
            raise ValueError("Piezo XY range is unavailable or invalid")
        angle = math.radians(frame.angle_deg)
        x_extent = abs(math.cos(angle)) * frame.width_m/2 + abs(math.sin(angle)) * frame.height_m/2
        y_extent = abs(math.sin(angle)) * frame.width_m/2 + abs(math.cos(angle)) * frame.height_m/2
        violations = []
        for axis, center, extent, full_range in (("x", frame.center_x_m, x_extent, piezo_range.x_m),
                                               ("y", frame.center_y_m, y_extent, piezo_range.y_m)):
            if abs(center) + extent > full_range/2:
                violations.append(SafetyViolation("scan_frame_set", axis, "outside_piezo_range",
                    f"Rotated scan frame exceeds read-back {axis.upper()} piezo range"))
        SafetyCheckResult("scan_frame_set", not violations, {}, tuple(violations)).raise_for_violations()

    def get_action(self, name: str) -> BoundedActionSpec | None:
        return self._actions.get(name)

    def validate(
        self,
        action: str,
        parameters: Mapping[str, Any],
        *,
        require_live_enabled: bool = False,
    ) -> SafetyCheckResult:
        spec = self.get_action(action)
        if spec is None:
            return SafetyCheckResult(
                action=action,
                ok=False,
                normalized_parameters={},
                violations=(
                    SafetyViolation(
                        action=action,
                        parameter=None,
                        code="unknown_action",
                        message=f"{action} is not a registered bounded action",
                    ),
                ),
            )
        return spec.validate(parameters, require_live_enabled=require_live_enabled)

    def require(
        self,
        action: str,
        parameters: Mapping[str, Any],
        *,
        require_live_enabled: bool = False,
    ) -> dict[str, float]:
        result = self.validate(action, parameters, require_live_enabled=require_live_enabled)
        result.raise_for_violations()
        return result.normalized_parameters


def build_control_safety_gate(profile: str | None = None) -> SafetyGate:
    """Use shared pulse bounds; an explicit mode may change mechanical bounds."""
    gate = build_default_safety_gate()
    if profile is None:
        return gate
    if profile != "stm_deep_contact":
        raise ValueError("Unknown control safety profile; omit safety_profile for ordinary "
                         "STM conditioning or use stm_deep_contact for the mechanical mode")
    actions = []
    for action in gate.list_actions():
        if action.name == "tip_shaper":
            # Operator-confirmed 50 nm commanded-travel envelope, not measured
            # penetration or a recipe. Preserve bias and restore feedback.
            bounds = {"tip_lift_m": (-50e-9, 0.), "lift_height_m": (0., 50e-9),
                      "change_bias": (0., 0.), "restore_feedback": (1., 1.)}
            action = replace(action, parameters=tuple(
                replace(p, minimum=bounds[p.name][0], maximum=bounds[p.name][1])
                if p.name in bounds else p for p in action.parameters))
        actions.append(action)
    return SafetyGate(actions)


def build_default_safety_gate() -> SafetyGate:
    gate = SafetyGate(
        [
            BoundedActionSpec(
                name="z_home",
                description="Move using current Home settings within read-back Z limits; fake-first, live-disabled.",
                parameters=(
                    NumericParameterSpec("maximum_displacement_m", "m", minimum=1e-12, maximum=1e-6),
                    NumericParameterSpec("delta_z_m", "m", minimum=-1e-6, maximum=1e-6, required=False),
                    NumericParameterSpec("target_z_m", "m", minimum=-1e-6, maximum=1e-6, required=False),
                ),
            ),
            BoundedActionSpec(
                name="drift_compensation_set",
                description=("Draft fake-first XYZ drift configuration envelope, not a measured safe "
                             "velocity or drift estimate. Persistent motion; station commissioning required."),
                parameters=(
                    NumericParameterSpec("enabled", "bool", minimum=0, maximum=1, integer=True),
                    NumericParameterSpec("vx_m_per_s", "m/s", minimum=-1e-9, maximum=1e-9),
                    NumericParameterSpec("vy_m_per_s", "m/s", minimum=-1e-9, maximum=1e-9),
                    NumericParameterSpec("vz_m_per_s", "m/s", minimum=-1e-10, maximum=1e-10),
                    NumericParameterSpec("saturation_limit_percent", "%", minimum=0.1, maximum=10),
                ),
            ),
            BoundedActionSpec(
                name="drift_compensation_stop",
                description=("Disable drift and zero all three velocities with readback. "
                             "Does not reset offsets; default live-disabled pending commissioning."),
                parameters=(
                    NumericParameterSpec("saturation_limit_percent", "%", minimum=0.1, maximum=10, required=False),
                ),
            ),
            BoundedActionSpec(
                name="scan_frame_set",
                description="Configure an idle scan frame inside read-back XY piezo range; commissioning required.",
                parameters=(
                    NumericParameterSpec("center_x_m", "m"),
                    NumericParameterSpec("center_y_m", "m"),
                    NumericParameterSpec("width_m", "m", minimum=1e-10, maximum=1e-3),
                    NumericParameterSpec("height_m", "m", minimum=1e-10, maximum=1e-3),
                    NumericParameterSpec("angle_deg", "deg", minimum=-360, maximum=360),
                ),
            ),
            BoundedActionSpec(
                name="scan_speed_set",
                description="Configure idle scan line times with readback; default live-disabled.",
                parameters=(
                    NumericParameterSpec("forward_line_time_s", "s", minimum=.01, maximum=100),
                    NumericParameterSpec("backward_line_time_s", "s", minimum=.01, maximum=100),
                    NumericParameterSpec("forward_m_per_s", "m/s", minimum=1e-12, maximum=1e-4),
                    NumericParameterSpec("backward_m_per_s", "m/s", minimum=1e-12, maximum=1e-4),
                ),
            ),
            BoundedActionSpec(
                name="feedback_observation",
                description="Finite STM feedback observation policy, not a permission to condition the tip.",
                parameters=(
                    NumericParameterSpec("duration_s", "s", minimum=0.1, maximum=600),
                    NumericParameterSpec("interval_s", "s", minimum=0.005, maximum=1),
                    NumericParameterSpec("maximum_sample_gap_s", "s", minimum=0.05, maximum=2),
                    NumericParameterSpec("maximum_abs_current_a", "A", minimum=1e-12, maximum=1e-6),
                    NumericParameterSpec("z_margin_m", "m", minimum=1e-12, maximum=1e-8),
                    NumericParameterSpec("settling_s", "s", minimum=0, maximum=30, required=False),
                    NumericParameterSpec("overcurrent_hold_s", "s", minimum=0, maximum=10, required=False),
                    NumericParameterSpec("withdraw_on_overcurrent", "bool", minimum=0, maximum=1, integer=True, required=False),
                ),
            ),
            BoundedActionSpec(
                name="bias_set",
                description="Set the sample bias with explicit read-back; live-disabled until commissioning.",
                parameters=(
                    NumericParameterSpec("target_bias_v", "V", minimum=-5.0, maximum=5.0),
                    NumericParameterSpec(
                        "readback_tolerance_v",
                        "V",
                        minimum=1e-9,
                        maximum=0.1,
                    ),
                ),
            ),
            BoundedActionSpec(
                name="z_setpoint_set",
                description=(
                    "Set the Z-controller current setpoint with explicit read-back; "
                    "live-disabled until commissioning."
                ),
                parameters=(
                    NumericParameterSpec(
                        "target_setpoint_a",
                        "A",
                        minimum=-1e-6,
                        maximum=1e-6,
                    ),
                    NumericParameterSpec(
                        "readback_tolerance_a",
                        "A",
                        minimum=1e-18,
                        maximum=1e-9,
                    ),
                ),
            ),
            BoundedActionSpec(
                name="z_controller_set",
                description=(
                    "Set feedback control on/off with read-back; live-disabled until commissioning."
                ),
                parameters=(
                    NumericParameterSpec(
                        "enabled", "bool", minimum=0.0, maximum=1.0, integer=True
                    ),
                    NumericParameterSpec(
                        "timeout_ms", "ms", minimum=10.0, maximum=1000.0,
                        integer=True, required=False,
                    ),
                ),
            ),
            BoundedActionSpec(
                name="coarse_xy_move",
                description=(
                    "Draft fake-first bound for PMD4 X/Y steps only; Nanonis Z direction codes "
                    "are intentionally outside the registered range."
                ),
                parameters=(
                    NumericParameterSpec(
                        "direction_code", "Nanonis code", minimum=0.0, maximum=3.0, integer=True
                    ),
                    NumericParameterSpec(
                        "number_of_steps", "steps", minimum=1.0, maximum=1000.0, integer=True
                    ),
                    NumericParameterSpec(
                        "group", "1-based group", minimum=1.0, maximum=6.0, integer=True
                    ),
                ),
            ),
            BoundedActionSpec(
                name="coarse_z_retract",
                description=(
                    "Draft withdraw-only Z+ action. The 10-step rehearsal ceiling is not "
                    "a calibrated clearance guarantee; live commissioning is still required."
                ),
                parameters=(
                    NumericParameterSpec(
                        "direction_code", "Nanonis code", minimum=4.0, maximum=4.0, integer=True
                    ),
                    NumericParameterSpec(
                        "number_of_steps", "steps", minimum=1.0, maximum=10.0, integer=True
                    ),
                    NumericParameterSpec(
                        "group", "1-based group", minimum=1.0, maximum=6.0, integer=True
                    ),
                ),
            ),
            BoundedActionSpec(
                name="auto_approach",
                description=(
                    "Start and await one Auto Approach cycle; fake-first and live-disabled."
                ),
                parameters=(
                    NumericParameterSpec(
                        "timeout_ms", "ms", minimum=1000.0, maximum=600000.0, integer=True
                    ),
                    NumericParameterSpec(
                        "poll_interval_ms", "ms", minimum=10.0, maximum=1000.0, integer=True
                    ),
                ),
            ),
            BoundedActionSpec(
                name="bias_pulse",
                description="Shared STM pulse envelope; live execution requires session authorization.",
                parameters=(
                    NumericParameterSpec("target_bias_v", "V", minimum=-10.0, maximum=10.0),
                    NumericParameterSpec("duration_ms", "ms", minimum=0.1, maximum=200.0),
                ),
            ),
            BoundedActionSpec(
                name="z_poke",
                description="Draft bounds for one absolute Z move expressed as a delta from the read-back position.",
                parameters=(
                    NumericParameterSpec("delta_z_m", "m", minimum=-2e-9, maximum=2e-9),
                    NumericParameterSpec("duration_ms", "ms", minimum=0.1, maximum=1000.0),
                ),
            ),
            BoundedActionSpec(
                name="tip_shaper",
                description="Draft bounds for a future bounded tip shaper workflow.",
                parameters=(
                    NumericParameterSpec("switch_off_delay_s", "s", minimum=0.0, maximum=5.0),
                    NumericParameterSpec("change_bias", "bool", minimum=0.0, maximum=1.0, integer=True),
                    NumericParameterSpec("bias_v", "V", minimum=-5.0, maximum=5.0),
                    NumericParameterSpec("tip_lift_m", "m", minimum=-2e-9, maximum=2e-9),
                    NumericParameterSpec("lift_time_1_s", "s", minimum=0.001, maximum=5.0),
                    NumericParameterSpec("bias_lift_v", "V", minimum=-5.0, maximum=5.0),
                    NumericParameterSpec("bias_settling_time_s", "s", minimum=0.0, maximum=5.0),
                    NumericParameterSpec("lift_height_m", "m", minimum=-2e-9, maximum=2e-9),
                    NumericParameterSpec("lift_time_2_s", "s", minimum=0.001, maximum=5.0),
                    NumericParameterSpec("end_wait_time_s", "s", minimum=0.0, maximum=5.0),
                    NumericParameterSpec("restore_feedback", "bool", minimum=0.0, maximum=1.0, integer=True),
                    NumericParameterSpec("estimated_duration_s", "s", minimum=0.002, maximum=20.0),
                ),
            ),
            BoundedActionSpec(
                name="follow_me_move",
                description=(
                    "Commissioned live move to a point inside the current scan frame with read-back."
                ),
                parameters=(
                    NumericParameterSpec("frame_x_fraction", "frame", minimum=-1.0, maximum=1.0),
                    NumericParameterSpec("frame_y_fraction", "frame", minimum=-1.0, maximum=1.0),
                    NumericParameterSpec("position_tolerance_fraction", "frame", minimum=0.0, maximum=0.1),
                ),
                live_enabled=True,
            ),
            BoundedActionSpec(
                name="bias_spectroscopy",
                description="Draft fake-first bounds for one in-memory bias spectroscopy acquisition.",
                parameters=(
                    NumericParameterSpec("start_bias_v", "V", minimum=-5.0, maximum=5.0),
                    NumericParameterSpec("end_bias_v", "V", minimum=-5.0, maximum=5.0),
                    NumericParameterSpec("z_offset_m", "m", minimum=-2e-9, maximum=2e-9),
                    NumericParameterSpec("number_of_sweeps", "sweeps", minimum=1.0, maximum=20.0, integer=True),
                    NumericParameterSpec("number_of_points", "points", minimum=2.0, maximum=4096.0, integer=True),
                    NumericParameterSpec("channel_count", "channels", minimum=1.0, maximum=24.0, integer=True),
                    NumericParameterSpec("minimum_channel_index", "RT index", minimum=0.0, maximum=127.0, integer=True),
                    NumericParameterSpec("maximum_channel_index", "RT index", minimum=0.0, maximum=127.0, integer=True),
                    NumericParameterSpec("maximum_slew_rate_v_per_s", "V/s", minimum=1e-6, maximum=1e6),
                    NumericParameterSpec("integration_time_s", "s", minimum=1e-6, maximum=10.0),
                    NumericParameterSpec("restoration_timeout_ms", "ms", minimum=10, maximum=5000,
                                         integer=True, required=False),
                    NumericParameterSpec("estimated_duration_s", "s", minimum=1e-6, maximum=300.0),
                ),
            ),
            BoundedActionSpec(
                name="bias_spectroscopy_lock_in_configure",
                description="Set only the idle Linear spectroscopy Lock-in Run flag with readback; no modulator start/stop.",
                parameters=(NumericParameterSpec("enabled", "bool", minimum=0, maximum=1, integer=True),),
            ),
            BoundedActionSpec(
                name="scan_pause",
                description="Pause an existing raster with status confirmation; live commissioning required.",
                parameters=(NumericParameterSpec("timeout_ms", "ms", minimum=100, maximum=5000, integer=True),),
            ),
            BoundedActionSpec(
                name="scan_resume",
                description="Resume a scan paused by this connection with unchanged frame/buffer/speed.",
                parameters=(),
            ),
            BoundedActionSpec(
                name="scan_start_current_frame",
                description=(
                    "Commissioned live start of the operator's current scan frame, speed, "
                    "buffer, and save settings; only the scan direction is selected."
                ),
                parameters=(
                    NumericParameterSpec("direction_up", "bool", minimum=0.0, maximum=1.0, integer=True),
                    NumericParameterSpec("timeout_ms", "ms", minimum=10.0, maximum=30000.0,
                                         integer=True, required=False),
                ),
                live_enabled=True,
            ),
            BoundedActionSpec(
                name="scan_acquisition",
                description="Acquire the currently configured scan frame without changing frame or speed.",
                parameters=(
                    NumericParameterSpec("direction_up", "bool", minimum=0.0, maximum=1.0, integer=True),
                    NumericParameterSpec("timeout_ms", "ms", minimum=100.0, maximum=300000.0, integer=True),
                ),
            ),
            BoundedActionSpec(
                name="local_scan_acquisition",
                description=(
                    "Temporarily scan a subframe entirely inside the current rotated frame, then restore it."
                ),
                parameters=(
                    NumericParameterSpec("center_x_fraction", "frame", minimum=-1.0, maximum=1.0),
                    NumericParameterSpec("center_y_fraction", "frame", minimum=-1.0, maximum=1.0),
                    NumericParameterSpec("width_fraction", "frame", minimum=0.01, maximum=1.0),
                    NumericParameterSpec("height_fraction", "frame", minimum=0.01, maximum=1.0),
                    NumericParameterSpec("direction_up", "bool", minimum=0.0, maximum=1.0, integer=True),
                    NumericParameterSpec("timeout_ms", "ms", minimum=100.0, maximum=300000.0, integer=True),
                ),
            ),
            BoundedActionSpec(
                name="data_log_configure",
                description=(
                    "Configure Nanonis Data Logger with full RT signal indexes and lossless averaging."
                ),
                parameters=(
                    NumericParameterSpec("timed", "bool", minimum=0.0, maximum=1.0, integer=True),
                    NumericParameterSpec("duration_s", "s", minimum=0.0, maximum=86400.0),
                    NumericParameterSpec("averaging", "samples", minimum=1.0, maximum=1.0, integer=True),
                    NumericParameterSpec("channel_count", "channels", minimum=1.0, maximum=128.0, integer=True),
                    NumericParameterSpec("minimum_channel_index", "RT index", minimum=0.0, maximum=127.0, integer=True),
                    NumericParameterSpec("maximum_channel_index", "RT index", minimum=0.0, maximum=127.0, integer=True),
                ),
            ),
            BoundedActionSpec(
                name="data_log_start",
                description="Start a preconfigured Nanonis Data Logger acquisition.",
                parameters=(
                    NumericParameterSpec("averaging", "samples", minimum=1.0, maximum=1.0, integer=True),
                    NumericParameterSpec("channel_count", "channels", minimum=1.0, maximum=128.0, integer=True),
                ),
            ),
            BoundedActionSpec(
                name="recipe_execution",
                description="Draft bounds for a future bounded recipe execution workflow.",
                parameters=(
                    NumericParameterSpec("max_steps", "steps", minimum=1.0, maximum=20.0, integer=True),
                    NumericParameterSpec("max_duration_s", "s", minimum=0.1, maximum=300.0),
                ),
            ),
        ]
    )
    # Reuse the finite observer bounds. Spectroscopy cannot exempt an initial
    # settling window from protection or defer sustained overcurrent to an agent.
    observation = gate.get_action("feedback_observation")
    spectroscopy = replace(observation, name="spectroscopy_observation",
        description="Finite direct current guard during STM spectroscopy HOLD; not a hardware interlock.",
        parameters=tuple(replace(p, minimum=0, maximum=0) if p.name == "settling_s"
            else replace(p, minimum=1, maximum=1) if p.name == "withdraw_on_overcurrent"
            else p for p in observation.parameters))
    return SafetyGate([*gate.list_actions(), spectroscopy])
