from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from pathlib import Path, PurePath
from typing import Any
import math


class ZControllerStatus(str, Enum):
    UNKNOWN = "unknown"
    OFF = "off"
    ON = "on"
    HOLD = "hold"
    SWITCHING_OFF = "switching_off"
    SAFE_TIP = "safe_tip"
    WITHDRAWING = "withdrawing"

    @classmethod
    def from_code(cls, code: int) -> "ZControllerStatus":
        return {
            1: cls.OFF,
            2: cls.ON,
            3: cls.HOLD,
            4: cls.SWITCHING_OFF,
            5: cls.SAFE_TIP,
            6: cls.WITHDRAWING,
        }.get(int(code), cls.UNKNOWN)


@dataclass(frozen=True)
class ZPositionLimits:
    high_limit_m: float
    low_limit_m: float


@dataclass(frozen=True)
class NanonisTipStateSnapshot:
    z_controller_status: ZControllerStatus
    z_controller_on: bool
    z_position_m: float
    z_limits_enabled: bool
    z_limits: ZPositionLimits
    tip_lift_m: float
    current_a: float


@dataclass(frozen=True)
class NanonisVersion:
    product_line: str
    version: str
    host_release: int | None
    rt_release: int | None
    available: bool = True
    error: str | None = None


@dataclass(frozen=True)
class ZHomeProperties:
    relative: bool
    position_m: float


@dataclass(frozen=True)
class ZHomeResult:
    properties: ZHomeProperties
    requested_target_z_m: float
    tip_state_before: NanonisTipStateSnapshot
    tip_state_after: NanonisTipStateSnapshot
    command_acknowledged: bool = True
    motion_completion: str = "awaiting_independent_postcheck"


@dataclass(frozen=True)
class ScanStatus:
    running: bool
    raw_code: int


@dataclass(frozen=True)
class ScanFrame:
    center_x_m: float
    center_y_m: float
    width_m: float
    height_m: float
    angle_deg: float


@dataclass(frozen=True)
class PiezoRange:
    x_m: float
    y_m: float
    z_m: float


@dataclass(frozen=True)
class DriftCompensationState:
    """Reported settings only, not measured drift or physical acceptance."""

    enabled: bool
    vx_m_per_s: float
    vy_m_per_s: float
    vz_m_per_s: float
    x_saturated: bool
    y_saturated: bool
    z_saturated: bool
    saturation_limit_percent: float | None

    @property
    def saturated(self) -> bool:
        return self.x_saturated or self.y_saturated or self.z_saturated


@dataclass(frozen=True)
class DriftCompensationResult:
    before: DriftCompensationState
    after: DriftCompensationState


@dataclass(frozen=True)
class LockInModulatorState:
    number: int
    enabled: bool
    signal: "SignalReference"
    amplitude: float
    phase_register: int
    phase_register_frequency_hz: float
    harmonic: int
    phase_deg: float


@dataclass(frozen=True)
class LockInFilterState:
    order: int  # 0 is off; no-change setter codes are not readback states.
    cutoff_frequency_hz: float


@dataclass(frozen=True)
class LockInDemodulatorState:
    number: int
    signal: "SignalReference"
    harmonic: int
    phase_register: int
    phase_register_frequency_hz: float
    phase_deg: float
    high_pass: LockInFilterState
    low_pass: LockInFilterState
    sync_filter_enabled: bool
    rt_signals: str  # X/Y or R/phi; signal names alone do not prove the mode.


@dataclass(frozen=True)
class ScanBuffer:
    channel_indexes: list[int]
    pixels: int
    lines: int


@dataclass(frozen=True)
class ScanSpeed:
    forward_m_per_s: float
    backward_m_per_s: float
    forward_line_time_s: float
    backward_line_time_s: float
    keep_parameter_constant: int
    speed_ratio: float


@dataclass(frozen=True)
class ScanStartResult:
    """Proof that only the current scan configuration was started."""

    direction: str
    status_before: ScanStatus
    status_after: ScanStatus
    configured_frame: ScanFrame
    configured_buffer: ScanBuffer
    configured_speed: ScanSpeed
    bounded_parameters: dict[str, float]


@dataclass(frozen=True)
class XYPosition:
    x_m: float
    y_m: float


@dataclass(frozen=True)
class FollowMeSpeed:
    speed_m_per_s: float
    custom_speed: bool


@dataclass(frozen=True)
class AtomTrackingProperties:
    integral_gain: float
    frequency_hz: float
    amplitude_m: float
    phase_deg: float
    switch_off_delay_s: float


@dataclass(frozen=True)
class SignalReference:
    requested: str | int
    index: int | None
    name: str | None
    error: str | None = None


@dataclass(frozen=True)
class SignalValue:
    index: int
    name: str | None
    value: float | None
    error: str | None = None


@dataclass(frozen=True)
class DataLogChannel:
    """One Data Logger channel addressed by the full Nanonis RT signal index."""

    index: int
    name: str | None


@dataclass(frozen=True)
class DataLogChannels:
    channels: tuple[DataLogChannel, ...]

    @property
    def indexes(self) -> tuple[int, ...]:
        return tuple(channel.index for channel in self.channels)


@dataclass(frozen=True)
class DataLogProperties:
    timed: bool
    duration_s: float
    averaging: int
    basename: str
    comment: str


@dataclass(frozen=True)
class DataLogStatus:
    start_time: str
    elapsed_s: float
    stop_time: str
    saved_file_path: str | None
    saved_points: int

    @property
    def running(self) -> bool:
        return bool(self.start_time) and not bool(self.stop_time)


@dataclass(frozen=True)
class DataLogRecipe:
    """Lossless Data Logger setup; averaging is intentionally fixed to one."""

    signals: tuple[str | int, ...]
    basename: str
    comment: str = ""
    modules: tuple[str, ...] = ()
    timed: bool = False
    duration_s: float = 0.0
    averaging: int = 1


@dataclass(frozen=True)
class ScanFrameData:
    channel_index: int
    channel_name: str
    direction: str
    rows: int
    columns: int
    data: Any
    raw_scan_direction: int


@dataclass(frozen=True)
class NanonisSpectrumData:
    instance: int
    f0_hz: float
    df_hz: float
    density: tuple[float, ...]

    @property
    def frequencies_hz(self) -> tuple[float, ...]:
        return tuple(self.f0_hz + index * self.df_hz for index in range(len(self.density)))

    @property
    def last_frequency_hz(self) -> float:
        if not self.density:
            return self.f0_hz
        return self.f0_hz + (len(self.density) - 1) * self.df_hz


@dataclass(frozen=True)
class NanonisSpectrumSettings:
    instance: int
    channel_index: int
    frequency_range_index: int
    frequency_ranges_hz: tuple[float, ...]
    frequency_resolution_index: int
    frequency_resolutions_hz: tuple[float, ...]
    fft_window_index: int
    averaging_mode: int
    weighting_mode: int
    averaging_count: int
    ac_coupling: bool

    @property
    def frequency_range_hz(self) -> float | None:
        return _selected(self.frequency_ranges_hz, self.frequency_range_index)

    @property
    def frequency_resolution_hz(self) -> float | None:
        return _selected(self.frequency_resolutions_hz, self.frequency_resolution_index)


@dataclass(frozen=True)
class BiasSpectroscopyAdvancedSettings:
    reset_bias: bool
    z_controller_hold: bool
    record_final_z: bool
    lock_in_run: bool
    sweep_mode: str
    lock_in_per_segment: bool | None  # Not applicable in Linear mode.


@dataclass(frozen=True)
class BiasSpectroscopyProperties:
    save_all: bool
    number_of_sweeps: int
    backward_sweep: bool
    number_of_points: int
    parameter_names: tuple[str, ...]
    fixed_parameter_names: tuple[str, ...]
    autosave: bool
    show_save_dialog: bool


@dataclass(frozen=True)
class BiasSpectroscopyTiming:
    z_averaging_time_s: float
    z_offset_m: float
    initial_settling_time_s: float
    maximum_slew_rate_v_per_s: float
    settling_time_s: float
    integration_time_s: float
    end_settling_time_s: float
    z_control_time_s: float


@dataclass(frozen=True)
class BiasSpectroscopySettings:
    running: bool
    properties: BiasSpectroscopyProperties
    start_bias_v: float
    end_bias_v: float
    channel_indexes: tuple[int, ...]
    channel_names: tuple[str, ...]
    timing: BiasSpectroscopyTiming


@dataclass(frozen=True)
class BiasSpectroscopyRecipe:
    """Bounded core recipe; advanced lock-in/reset/feedback settings are inherited.

    This is not a complete live ESR acquisition protocol. Autosave and dialogs
    are disabled. Advanced setting control needs separate commissioning.
    """

    start_bias_v: float
    end_bias_v: float
    number_of_sweeps: int
    number_of_points: int
    channel_indexes: tuple[int, ...]
    timing: BiasSpectroscopyTiming
    backward_sweep: bool = False
    save_all: bool = False
    restoration_timeout_ms: int = 1000

    @classmethod
    def from_mapping(cls, values: dict) -> "BiasSpectroscopyRecipe":
        fields = dict(values)
        fields["timing"] = BiasSpectroscopyTiming(**fields["timing"])
        fields["channel_indexes"] = tuple(fields["channel_indexes"])
        return cls(**fields)

    @property
    def estimated_duration_s(self) -> float:
        directions = 2 if self.backward_sweep else 1
        per_point_s = self.timing.settling_time_s + self.timing.integration_time_s
        sweep_s = directions * self.number_of_points * per_point_s
        overhead_s = (
            self.timing.z_averaging_time_s
            + self.timing.initial_settling_time_s
            + self.timing.end_settling_time_s
            + self.timing.z_control_time_s
        )
        return self.number_of_sweeps * sweep_s + overhead_s


@dataclass(frozen=True)
class BiasSpectroscopyData:
    channel_names: tuple[str, ...]
    rows: int
    columns: int
    data: tuple[tuple[float, ...], ...]
    parameters: tuple[float, ...]


@dataclass(frozen=True)
class BiasSpectroscopyAcquisition:
    recipe: BiasSpectroscopyRecipe
    settings_before: BiasSpectroscopySettings
    data: BiasSpectroscopyData
    tip_state_before: NanonisTipStateSnapshot
    tip_state_after: NanonisTipStateSnapshot
    bounded_parameters: dict[str, float]
    settings_applied: BiasSpectroscopySettings | None = None
    advanced_settings_before: BiasSpectroscopyAdvancedSettings | None = None
    bias_before_v: float | None = None
    bias_after_v: float | None = None
    save_settings_restoration: dict[str, Any] | None = None


@dataclass(frozen=True)
class BiasPulseRecipe:
    target_bias_v: float
    duration_ms: float


@dataclass(frozen=True)
class ZPokeRecipe:
    delta_z_m: float
    duration_ms: float


@dataclass(frozen=True)
class FeedbackPokeRecipe:
    """One depth-controlled poke that preserves the current working bias.

    ``approach_depth_m`` is positive.  Nanonis Tip Shaper receives the
    station's toward-sample convention as a negative Tip Lift and returns by
    the same positive Lift Height. Bias is supplied from device readback when
    building the full Tip Shaper recipe, never as an operator-adjustable input.
    """

    approach_depth_m: float
    switch_off_delay_s: float = 0.05
    approach_time_s: float = 0.2
    dwell_time_s: float = 0.1
    retract_time_s: float = 0.2
    end_wait_time_s: float = 0.1

    def to_tip_shaper_recipe(self, *, current_bias_v: float) -> "TipShaperRecipe":
        return TipShaperRecipe(
            switch_off_delay_s=self.switch_off_delay_s,
            change_bias=False,
            bias_v=current_bias_v,
            tip_lift_m=-self.approach_depth_m,
            lift_time_1_s=self.approach_time_s,
            bias_lift_v=current_bias_v,
            bias_settling_time_s=self.dwell_time_s,
            lift_height_m=self.approach_depth_m,
            lift_time_2_s=self.retract_time_s,
            end_wait_time_s=self.end_wait_time_s,
            restore_feedback=True,
        )


@dataclass(frozen=True)
class TipShaperRecipe:
    switch_off_delay_s: float
    change_bias: bool
    bias_v: float
    tip_lift_m: float
    lift_time_1_s: float
    bias_lift_v: float
    bias_settling_time_s: float
    lift_height_m: float
    lift_time_2_s: float
    end_wait_time_s: float
    restore_feedback: bool = True

    @property
    def estimated_duration_s(self) -> float:
        return (
            self.switch_off_delay_s
            + self.lift_time_1_s
            + self.bias_settling_time_s
            + self.lift_time_2_s
            + self.end_wait_time_s
        )


TipShaperProperties = TipShaperRecipe


@dataclass(frozen=True)
class ConditioningActionResult:
    action: str
    tip_state_before: NanonisTipStateSnapshot
    tip_state_after: NanonisTipStateSnapshot
    bounded_parameters: dict[str, float]


class CoarseXYDirection(str, Enum):
    X_PLUS = "x_plus"
    X_MINUS = "x_minus"
    Y_PLUS = "y_plus"
    Y_MINUS = "y_minus"

    @property
    def nanonis_code(self) -> int:
        return tuple(type(self)).index(self)


@dataclass(frozen=True)
class CoarseMoveResult:
    """Acknowledged motor command; neither steps nor state checks prove clearance."""

    direction: str
    nanonis_direction_code: int
    number_of_steps: int
    group: int
    waited_until_finished: bool
    completion_evidence: str
    bounded_parameters: dict[str, float]
    tip_state_before: NanonisTipStateSnapshot
    tip_state_after: NanonisTipStateSnapshot


@dataclass(frozen=True)
class MotorPosition:
    """Motor Control position read-back for one 1-based Nanonis group."""

    group: int
    x_m: float
    y_m: float
    z_m: float


@dataclass(frozen=True)
class FollowMeMoveResult:
    requested_frame_x_fraction: float
    requested_frame_y_fraction: float
    target: XYPosition
    position_before: XYPosition
    position_after: XYPosition
    bounded_parameters: dict[str, float]

    @property
    def x_m(self) -> float:
        return self.position_after.x_m

    @property
    def y_m(self) -> float:
        return self.position_after.y_m


@dataclass(frozen=True)
class ScanAcquisitionResult:
    timed_out: bool
    file_path: str | None
    frame_data: ScanFrameData
    bounded_parameters: dict[str, float]
    configured_frame: ScanFrame | None = None
    restored_frame: ScanFrame | None = None


@dataclass(frozen=True)
class AutoApproachResult:
    elapsed_s: float
    tip_state_before: NanonisTipStateSnapshot
    tip_state_after: NanonisTipStateSnapshot
    bounded_parameters: dict[str, float]
    completion_mode: str = "engaged"


@dataclass(frozen=True)
class RecentDataFile:
    path: Path
    modified_time_s: float
    size_bytes: int


@dataclass(frozen=True)
class NanonisStatusSnapshot:
    version: NanonisVersion
    session_path: str | None
    bias_v: float
    current_a: float
    z_position_m: float
    z_controller_status: ZControllerStatus
    z_setpoint_a: float
    scan_status: ScanStatus
    scan_frame: ScanFrame
    scan_buffer: ScanBuffer
    signals: list[SignalValue] = field(default_factory=list)
    recent_files: list[RecentDataFile] = field(default_factory=list)


def to_jsonable(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        # Preserve non-finite readbacks explicitly without emitting invalid JSON.
        return {"nonfinite_float": "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, PurePath):
        return str(value)
    if is_dataclass(value):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value


def _selected(values: tuple[float, ...], index: int) -> float | None:
    if 0 <= int(index) < len(values):
        return float(values[int(index)])
    return None
