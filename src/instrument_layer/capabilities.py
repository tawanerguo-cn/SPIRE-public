from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
import inspect
from typing import Any

from .backends import OfficialNanonisBackend
from .safety import SafetyGate, build_default_safety_gate


class CapabilityImplementation(str, Enum):
    IMPLEMENTED = "implemented"
    NOT_IMPLEMENTED = "not_implemented"
    INTERNAL_HELPER = "internal_helper"


class CapabilityAccess(str, Enum):
    READ_ONLY = "read_only"
    INSTRUMENT_CONTROL = "instrument_control"
    DATA_WRITE = "data_write"
    UI_CONTROL = "ui_control"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


class CapabilitySafety(str, Enum):
    READ_ONLY = "read_only_no_hardware_change"
    PROTECTIVE_STOP = "protective_stop_only"
    BOUNDED_ACTION = "bounded_action_registered"
    NOT_EXPOSED = "not_exposed"
    NOT_EXPOSED_DATA_WRITE = "not_exposed_data_write"
    INTERNAL = "internal_helper_not_for_agent"
    UNKNOWN = "unknown_requires_review"


@dataclass(frozen=True)
class CapabilityOverlay:
    wrapper_methods: tuple[str, ...]
    access: CapabilityAccess
    safety: CapabilitySafety
    bounded_action: str | None = None
    notes: str = ""
    wrapper_bounded_actions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class NanonisCapability:
    nanonis_method: str
    group: str
    implementation: CapabilityImplementation
    access: CapabilityAccess
    safety: CapabilitySafety
    wrapper_methods: tuple[str, ...] = ()
    bounded_action: str | None = None
    notes: str = ""
    wrapper_bounded_actions: dict[str, str] = field(default_factory=dict)

    @property
    def is_implemented(self) -> bool:
        return self.implementation == CapabilityImplementation.IMPLEMENTED

    @property
    def is_read_only(self) -> bool:
        return self.access == CapabilityAccess.READ_ONLY

    @property
    def changes_instrument_state(self) -> bool:
        return self.access in {CapabilityAccess.INSTRUMENT_CONTROL, CapabilityAccess.UI_CONTROL}

    @property
    def writes_data(self) -> bool:
        return self.access == CapabilityAccess.DATA_WRITE


@dataclass(frozen=True)
class CapabilitySummary:
    total: int
    by_implementation: dict[str, int]
    by_access: dict[str, int]
    by_safety: dict[str, int]


IMPLEMENTED_NANONIS_OVERLAYS: dict[str, CapabilityOverlay] = {
    "Util_VersionGet": CapabilityOverlay(
        ("get_version",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "Util_AcqPeriodGet": CapabilityOverlay(
        ("get_acquisition_period_s",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "Util_SessionPathGet": CapabilityOverlay(
        ("get_session_path", "list_recent_data_files"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
        notes="list_recent_data_files also reads the local Nanonis session directory.",
    ),
    "Bias_Get": CapabilityOverlay(
        ("get_bias_v",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "Bias_Set": CapabilityOverlay(
        ("set_bias_v",),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="bias_set",
        notes="Typed read-back wrapper; live execution remains disabled by default.",
    ),
    "Current_Get": CapabilityOverlay(
        ("get_current_a",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "ZCtrl_ZPosGet": CapabilityOverlay(
        ("get_z_position_m",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "ZCtrl_StatusGet": CapabilityOverlay(
        ("get_z_controller_status",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "ZCtrl_OnOffGet": CapabilityOverlay(
        ("get_z_controller_on", "get_tip_state_snapshot"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "ZCtrl_OnOffSet": CapabilityOverlay(
        ("set_z_controller_on",),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="z_controller_set",
        notes="Typed read-back wrapper; live execution remains disabled by default.",
    ),
    "ZCtrl_LimitsEnabledGet": CapabilityOverlay(
        ("get_z_limits_enabled", "get_tip_state_snapshot"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "ZCtrl_HomePropsGet": CapabilityOverlay(
        ("get_z_home_properties",), CapabilityAccess.READ_ONLY, CapabilitySafety.READ_ONLY),
    "ZCtrl_Home": CapabilityOverlay(
        ("home_z_controller",), CapabilityAccess.INSTRUMENT_CONTROL, CapabilitySafety.BOUNDED_ACTION,
        bounded_action="z_home", notes="Use existing absolute/relative Home settings, bound derived travel and audit one command. Default live-disabled; return current state for independent postcheck."),
    "ZCtrl_LimitsGet": CapabilityOverlay(
        ("get_z_limits", "get_tip_state_snapshot"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "ZCtrl_TipLiftGet": CapabilityOverlay(
        ("get_tip_lift_m", "get_tip_state_snapshot"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "ZCtrl_SetpntGet": CapabilityOverlay(
        ("get_z_setpoint_a",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "ZCtrl_SetpntSet": CapabilityOverlay(
        ("set_z_setpoint_a",),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="z_setpoint_set",
        notes="Typed read-back wrapper; live execution remains disabled by default.",
    ),
    "AutoApproach_OnOffGet": CapabilityOverlay(
        ("get_auto_approach_running",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "AutoApproach_OnOffSet": CapabilityOverlay(
        ("run_auto_approach", "stop_auto_approach"),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.PROTECTIVE_STOP,
        bounded_action="auto_approach",
        notes=(
            "Stop is protective; start is exposed only through a bounded wait/timeout transaction "
            "and remains live-disabled."
        ),
    ),
    "Scan_StatusGet": CapabilityOverlay(
        ("get_scan_status",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "Scan_FrameGet": CapabilityOverlay(
        ("get_scan_frame", "validate_tip_xy"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "Scan_FrameSet": CapabilityOverlay(
        ("acquire_local_scan", "set_scan_frame"),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="local_scan_acquisition",
        wrapper_bounded_actions={"set_scan_frame": "scan_frame_set", "acquire_local_scan": "local_scan_acquisition"},
        notes="Contained temporary subframes or an explicit idle frame configuration within read-back piezo range; default live-disabled.",
    ),
    "Scan_SpeedSet": CapabilityOverlay(
        ("set_scan_line_times",), CapabilityAccess.INSTRUMENT_CONTROL, CapabilitySafety.BOUNDED_ACTION,
        bounded_action="scan_speed_set", notes="Idle configuration with readback; default live-disabled."),
    "Piezo_RangeGet": CapabilityOverlay(
        ("get_piezo_range",), CapabilityAccess.READ_ONLY, CapabilitySafety.READ_ONLY),
    "Piezo_DriftCompGet": CapabilityOverlay(
        ("get_drift_compensation",), CapabilityAccess.READ_ONLY, CapabilitySafety.READ_ONLY),
    "Piezo_DriftCompSet": CapabilityOverlay(
        ("set_drift_compensation", "stop_drift_compensation"),
        CapabilityAccess.INSTRUMENT_CONTROL, CapabilitySafety.BOUNDED_ACTION,
        wrapper_bounded_actions={"set_drift_compensation": "drift_compensation_set",
                                 "stop_drift_compensation": "drift_compensation_stop"},
        notes="XYZ velocities and saturation limit, strict readback and audit; both actions default live-disabled. No offset reset or automatic rearm."),
    "LockIn_ModOnOffGet": CapabilityOverlay(
        ("get_lock_in_modulator", "get_lock_in_modulator_enabled"), CapabilityAccess.READ_ONLY, CapabilitySafety.READ_ONLY),
    "LockIn_ModSignalGet": CapabilityOverlay(
        ("get_lock_in_modulator",), CapabilityAccess.READ_ONLY, CapabilitySafety.READ_ONLY),
    "LockIn_ModAmpGet": CapabilityOverlay(
        ("get_lock_in_modulator",), CapabilityAccess.READ_ONLY, CapabilitySafety.READ_ONLY),
    "LockIn_ModPhasRegGet": CapabilityOverlay(
        ("get_lock_in_modulator",), CapabilityAccess.READ_ONLY, CapabilitySafety.READ_ONLY),
    "LockIn_ModPhasFreqGet": CapabilityOverlay(
        ("get_lock_in_modulator", "get_lock_in_demodulator", "get_lock_in_phase_frequency_hz"),
        CapabilityAccess.READ_ONLY, CapabilitySafety.READ_ONLY,
        notes="Explicit protocol register 1-8 only; never infer a binding for observed register 0."),
    "LockIn_ModHarmonicGet": CapabilityOverlay(
        ("get_lock_in_modulator",), CapabilityAccess.READ_ONLY, CapabilitySafety.READ_ONLY),
    "LockIn_ModPhasGet": CapabilityOverlay(
        ("get_lock_in_modulator",), CapabilityAccess.READ_ONLY, CapabilitySafety.READ_ONLY),
    **{name: CapabilityOverlay(("get_lock_in_demodulator",),
            CapabilityAccess.READ_ONLY, CapabilitySafety.READ_ONLY,
            notes="Protocol/fake validated; station commissioning pending. No setters exposed.")
       for name in ("LockIn_DemodSignalGet", "LockIn_DemodHarmonicGet", "LockIn_DemodPhasRegGet",
                    "LockIn_DemodPhasGet", "LockIn_DemodHPFilterGet", "LockIn_DemodLPFilterGet",
                    "LockIn_DemodSyncFilterGet", "LockIn_DemodRTSignalsGet")},
    "Scan_BufferGet": CapabilityOverlay(
        ("get_scan_buffer",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "Scan_SpeedGet": CapabilityOverlay(
        ("get_scan_speed",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "Scan_XYPosGet": CapabilityOverlay(
        ("get_scan_xy_position",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "FolMe_XYPosGet": CapabilityOverlay(
        ("get_follow_me_position",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "FolMe_SpeedGet": CapabilityOverlay(
        ("get_follow_me_speed",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "Signals_NamesGet": CapabilityOverlay(
        ("get_signal_catalog", "resolve_signals"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "Signals_ValsGet": CapabilityOverlay(
        ("read_resolved_signals", "read_signals", "sample_signal_chart"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "DataLog_ChsGet": CapabilityOverlay(
        ("get_data_log_channels",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
        notes="Indexes are the full 0..127 RT signal indexes in the current protocol.",
    ),
    "DataLog_PropsGet": CapabilityOverlay(
        ("get_data_log_properties",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "DataLog_StatusGet": CapabilityOverlay(
        ("get_data_log_status",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "DataLog_ChsSet": CapabilityOverlay(
        ("configure_data_log",),
        CapabilityAccess.DATA_WRITE,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="data_log_configure",
    ),
    "DataLog_PropsSet": CapabilityOverlay(
        ("configure_data_log",),
        CapabilityAccess.DATA_WRITE,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="data_log_configure",
    ),
    "DataLog_Start": CapabilityOverlay(
        ("start_data_log",),
        CapabilityAccess.DATA_WRITE,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="data_log_start",
        notes="Not exposed to agents and live-disabled until explicit commissioning.",
    ),
    "DataLog_Stop": CapabilityOverlay(
        ("stop_data_log",),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.PROTECTIVE_STOP,
    ),
    "Scan_FrameDataGrab": CapabilityOverlay(
        ("grab_scan_frame",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "SpectrumAnlzr_ChGet": CapabilityOverlay(
        ("get_spectrum_analyzer_channel", "get_spectrum_analyzer_settings"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "SpectrumAnlzr_FreqRangeGet": CapabilityOverlay(
        ("get_spectrum_analyzer_frequency_ranges", "get_spectrum_analyzer_settings"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "SpectrumAnlzr_FreqResGet": CapabilityOverlay(
        ("get_spectrum_analyzer_frequency_resolutions", "get_spectrum_analyzer_settings"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "SpectrumAnlzr_FFTWindowGet": CapabilityOverlay(
        ("get_spectrum_analyzer_fft_window", "get_spectrum_analyzer_settings"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "SpectrumAnlzr_AveragGet": CapabilityOverlay(
        ("get_spectrum_analyzer_averaging", "get_spectrum_analyzer_settings"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "SpectrumAnlzr_ACCouplingGet": CapabilityOverlay(
        ("get_spectrum_analyzer_ac_coupling", "get_spectrum_analyzer_settings"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "SpectrumAnlzr_DataGet": CapabilityOverlay(
        ("get_spectrum_analyzer_data",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "BiasSpectr_AdvPropsGet": CapabilityOverlay(
        ("get_bias_spectroscopy_advanced_settings",), CapabilityAccess.READ_ONLY, CapabilitySafety.READ_ONLY),
    "BiasSpectr_AdvPropsSet": CapabilityOverlay(
        ("set_bias_spectroscopy_lock_in_run",), CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.BOUNDED_ACTION, bounded_action="bias_spectroscopy_lock_in_configure",
        notes="Only Lock-in Run; other flags use no-change codes. Idle Linear/HOLD/reset only. Default live-disabled; OFF does not turn off an already running modulator."),
    "BiasSpectr_MLSModeGet": CapabilityOverlay(
        ("get_bias_spectroscopy_advanced_settings",), CapabilityAccess.READ_ONLY, CapabilitySafety.READ_ONLY),
    "BiasSpectr_MLSLockinPerSegGet": CapabilityOverlay(
        ("get_bias_spectroscopy_advanced_settings",), CapabilityAccess.READ_ONLY, CapabilitySafety.READ_ONLY),
    "BiasSpectr_StatusGet": CapabilityOverlay(
        ("get_bias_spectroscopy_status", "get_bias_spectroscopy_settings"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "BiasSpectr_PropsGet": CapabilityOverlay(
        ("get_bias_spectroscopy_properties", "get_bias_spectroscopy_settings"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "BiasSpectr_LimitsGet": CapabilityOverlay(
        ("get_bias_spectroscopy_limits", "get_bias_spectroscopy_settings"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "BiasSpectr_ChsGet": CapabilityOverlay(
        ("get_bias_spectroscopy_channels", "get_bias_spectroscopy_settings"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "BiasSpectr_TimingGet": CapabilityOverlay(
        ("get_bias_spectroscopy_timing", "get_bias_spectroscopy_settings"),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "TipShaper_PropsGet": CapabilityOverlay(
        ("get_tip_shaper_properties",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "AtomTrack_StatusGet": CapabilityOverlay(
        ("get_atom_tracking_status",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "AtomTrack_PropsGet": CapabilityOverlay(
        ("get_atom_tracking_properties",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
    ),
    "FolMe_XYPosSet": CapabilityOverlay(
        ("move_tip_xy",),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="follow_me_move",
        notes=(
            "Target must be inside the current scan frame; live API/UI/read-back commissioning "
            "passed on the local station on 2026-08-12."
        ),
    ),
    "BiasSpectr_PropsSet": CapabilityOverlay(
        ("acquire_bias_spectrum", "acquire_bias_spectrum_transaction"),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="bias_spectroscopy",
    ),
    "BiasSpectr_LimitsSet": CapabilityOverlay(
        ("acquire_bias_spectrum", "acquire_bias_spectrum_transaction"),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="bias_spectroscopy",
    ),
    "BiasSpectr_ChsSet": CapabilityOverlay(
        ("acquire_bias_spectrum", "acquire_bias_spectrum_transaction"),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="bias_spectroscopy",
    ),
    "BiasSpectr_TimingSet": CapabilityOverlay(
        ("acquire_bias_spectrum", "acquire_bias_spectrum_transaction"),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="bias_spectroscopy",
    ),
    "BiasSpectr_Start": CapabilityOverlay(
        ("acquire_bias_spectrum", "acquire_bias_spectrum_transaction"),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="bias_spectroscopy",
        notes="Wrapper acquires in memory with saving disabled, then verifies restoration of inherited save flags; takeover defers restoration.",
    ),
    "BiasSpectr_Stop": CapabilityOverlay(
        ("stop_bias_spectroscopy",),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.PROTECTIVE_STOP,
    ),
    "Bias_Pulse": CapabilityOverlay(
        ("apply_bias_pulse",),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="bias_pulse",
        notes="Absolute pulse with Z controller hold; live execution is disabled by default.",
    ),
    "TipShaper_PropsSet": CapabilityOverlay(
        ("run_tip_shaper", "run_feedback_poke"),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="tip_shaper",
    ),
    "TipShaper_Start": CapabilityOverlay(
        ("run_tip_shaper", "run_feedback_poke"),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="tip_shaper",
    ),
    "ZCtrl_ZPosSet": CapabilityOverlay(
        ("run_z_poke",),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="z_poke",
        notes="Atomic move/dwell/restore action; requires Z controller off and enabled Z limits.",
    ),
    "Scan_Action": CapabilityOverlay(
        ("stop_scan", "start_scan", "acquire_scan", "pause_scan", "resume_scan"),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="scan_start_current_frame",
        wrapper_bounded_actions={"start_scan": "scan_start_current_frame", "acquire_scan": "scan_acquisition",
                                 "pause_scan": "scan_pause", "resume_scan": "scan_resume"},
        notes=(
            "Stop is protective; start_scan starts the existing operator configuration and "
            "acquire_scan is the separate blocking acquisition transaction."
        ),
    ),
    "Scan_WaitEndOfScan": CapabilityOverlay(
        ("acquire_scan",),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.BOUNDED_ACTION,
        bounded_action="scan_acquisition",
    ),
    "FolMe_Stop": CapabilityOverlay(
        ("stop_follow_me",),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.PROTECTIVE_STOP,
    ),
    "AtomTrack_CtrlSet": CapabilityOverlay(
        ("stop_atom_tracking",),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.PROTECTIVE_STOP,
        notes="Only Status=0 is exposed; enabling Atom Tracking remains unavailable.",
    ),
    "Motor_StopMove": CapabilityOverlay(
        ("stop_motor",),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.PROTECTIVE_STOP,
    ),
    "Motor_PosGet": CapabilityOverlay(
        ("get_motor_position",),
        CapabilityAccess.READ_ONLY,
        CapabilitySafety.READ_ONLY,
        notes=(
            "Typed read-only probe; the local PMD4 currently returns Cannot access Motor Control Module."
        ),
    ),
    "Motor_StartMove": CapabilityOverlay(
        (
            "move_coarse_x_plus",
            "move_coarse_x_minus",
            "move_coarse_y_plus",
            "move_coarse_y_minus",
            "retract_coarse_z",
        ),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.BOUNDED_ACTION,
        wrapper_bounded_actions={
            "move_coarse_x_plus": "coarse_xy_move",
            "move_coarse_x_minus": "coarse_xy_move",
            "move_coarse_y_plus": "coarse_xy_move",
            "move_coarse_y_minus": "coarse_xy_move",
            "retract_coarse_z": "coarse_z_retract",
        },
        notes=(
            "XY uses codes 0..3; withdraw-only Z+ uses code 4. Z- (5) has no wrapper; "
            "live execution remains disabled."
        ),
    ),
    "ZCtrl_Withdraw": CapabilityOverlay(
        ("withdraw_tip", "stop_all_motion"),
        CapabilityAccess.INSTRUMENT_CONTROL,
        CapabilitySafety.PROTECTIVE_STOP,
        notes="Exposed as a protective withdraw, not as a general Z-control workflow.",
    ),
}


BOUNDED_ACTION_OVERLAYS: dict[str, str] = {
    "Bias_Pulse": "bias_pulse",
    "MProbeBias_Pulse": "bias_pulse",
    "TipShaper_Start": "tip_shaper",
    "TipShaper_PropsSet": "tip_shaper",
    "ZCtrl_ZPosSet": "z_poke",
    "MProbeZCtrl_ZPosSet": "z_poke",
    "Motor_StartMove": "coarse_xy_move",
}


def discover_nanonis_methods(nanonis_class: Any | None = None) -> list[str]:
    if nanonis_class is None:
        return OfficialNanonisBackend.discover_client_methods()
    return sorted(
        name
        for name, value in inspect.getmembers(nanonis_class)
        if callable(value) and not name.startswith("_")
    )


def build_nanonis_capability_registry(
    method_names: Sequence[str] | None = None,
    *,
    include_helpers: bool = False,
    safety_gate: SafetyGate | None = None,
) -> list[NanonisCapability]:
    if method_names is None:
        method_names = discover_nanonis_methods()
    gate = safety_gate or build_default_safety_gate()
    registered_actions = {action.name for action in gate.list_actions()}

    capabilities = [
        _build_capability(method, registered_actions)
        for method in sorted(method_names)
        if include_helpers or _is_nanonis_command(method)
    ]
    return capabilities


def summarize_capabilities(capabilities: Iterable[NanonisCapability]) -> CapabilitySummary:
    items = list(capabilities)
    return CapabilitySummary(
        total=len(items),
        by_implementation=dict(Counter(item.implementation.value for item in items)),
        by_access=dict(Counter(item.access.value for item in items)),
        by_safety=dict(Counter(item.safety.value for item in items)),
    )


def find_capability(capabilities: Iterable[NanonisCapability], method: str) -> NanonisCapability | None:
    for capability in capabilities:
        if capability.nanonis_method == method:
            return capability
    return None


def _build_capability(method: str, registered_actions: set[str]) -> NanonisCapability:
    group = _method_group(method)

    if not _is_nanonis_command(method):
        return NanonisCapability(
            nanonis_method=method,
            group=group,
            implementation=CapabilityImplementation.INTERNAL_HELPER,
            access=CapabilityAccess.INTERNAL,
            safety=CapabilitySafety.INTERNAL,
            notes="Public nanonis_spm helper, not a Nanonis command for agent use.",
        )

    overlay = IMPLEMENTED_NANONIS_OVERLAYS.get(method)
    if overlay is not None:
        for wrapper, action in overlay.wrapper_bounded_actions.items():
            if wrapper not in overlay.wrapper_methods or action not in registered_actions:
                raise ValueError(f"{method}.{wrapper} refers to an unregistered wrapper or action: {action}")
        return NanonisCapability(
            nanonis_method=method,
            group=group,
            implementation=CapabilityImplementation.IMPLEMENTED,
            access=overlay.access,
            safety=overlay.safety,
            wrapper_methods=overlay.wrapper_methods,
            bounded_action=overlay.bounded_action,
            notes=overlay.notes,
            wrapper_bounded_actions=dict(overlay.wrapper_bounded_actions),
        )

    bounded_action = BOUNDED_ACTION_OVERLAYS.get(method)
    if bounded_action in registered_actions:
        return NanonisCapability(
            nanonis_method=method,
            group=group,
            implementation=CapabilityImplementation.NOT_IMPLEMENTED,
            access=CapabilityAccess.INSTRUMENT_CONTROL,
            safety=CapabilitySafety.BOUNDED_ACTION,
            bounded_action=bounded_action,
            notes="Safety bounds are registered, but live execution is not implemented.",
        )

    access = _classify_access(method)
    safety = _default_safety_for_access(access)
    return NanonisCapability(
        nanonis_method=method,
        group=group,
        implementation=CapabilityImplementation.NOT_IMPLEMENTED,
        access=access,
        safety=safety,
    )


def _is_nanonis_command(method: str) -> bool:
    head, separator, tail = method.partition("_")
    return bool(separator and head[:1].isupper() and tail[:1].isupper())


def _method_group(method: str) -> str:
    if "_" not in method:
        return "internal"
    return method.split("_", 1)[0]


def _classify_access(method: str) -> CapabilityAccess:
    if not _is_nanonis_command(method):
        return CapabilityAccess.INTERNAL

    if method.endswith("Get") or method.endswith("DataGet") or method.endswith("NamesGet"):
        return CapabilityAccess.READ_ONLY
    if method.endswith("ListGet") or method.endswith("StatusGet") or method.endswith("ValGet"):
        return CapabilityAccess.READ_ONLY
    if method.endswith("Grab") or method.endswith("WaitEndOfScan"):
        return CapabilityAccess.READ_ONLY
    if method.endswith("Open") or method.endswith("Close") or method.endswith("Show"):
        return CapabilityAccess.UI_CONTROL

    data_write_tokens = (
        "Save",
        "Autosave",
        "DataLog_Start",
        "TCPLog_Start",
    )
    if any(token in method for token in data_write_tokens):
        return CapabilityAccess.DATA_WRITE

    control_suffixes = (
        "Set",
        "Start",
        "Stop",
        "Run",
        "Pulse",
        "Move",
        "Withdraw",
        "Home",
        "Action",
        "Activate",
        "Clear",
        "Erase",
        "Draw",
        "Paste",
        "Delete",
        "Reset",
        "Rearm",
        "Restart",
        "AutoOffset",
        "DriftComp",
        "QuickCompStart",
        "StartClosedLoop",
        "ScannerSwitch",
        "FreqShiftAutoCenter",
        "TrigAuto",
        "NullDefl",
        "SingleStateUpdate",
        "SingleTempUpdate",
        "Undeploy",
        "Deploy",
        "Load",
        "Pause",
        "Quit",
        "Lock",
        "UnLock",
    )
    if method.endswith(control_suffixes):
        return CapabilityAccess.INSTRUMENT_CONTROL

    return CapabilityAccess.UNKNOWN


def _default_safety_for_access(access: CapabilityAccess) -> CapabilitySafety:
    if access == CapabilityAccess.READ_ONLY:
        return CapabilitySafety.READ_ONLY
    if access == CapabilityAccess.DATA_WRITE:
        return CapabilitySafety.NOT_EXPOSED_DATA_WRITE
    if access in {CapabilityAccess.INSTRUMENT_CONTROL, CapabilityAccess.UI_CONTROL}:
        return CapabilitySafety.NOT_EXPOSED
    if access == CapabilityAccess.INTERNAL:
        return CapabilitySafety.INTERNAL
    return CapabilitySafety.UNKNOWN
