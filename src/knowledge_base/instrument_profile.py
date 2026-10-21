from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import tomllib


DEFAULT_INSTRUMENT_PROFILE_DIRECTORY = Path(__file__).resolve().parent / "instrument_profiles"
DEFAULT_INSTRUMENT_PROFILE_ID = "spire_local_station"


@dataclass(frozen=True)
class InstrumentEvidence:
    kind: str
    observed_on: str
    source: str | None
    fact: str


@dataclass(frozen=True)
class InstrumentProfile:
    profile_id: str
    status: str
    reviewed_on: str
    station: str
    application: str
    active_session_path: Path
    lowest_experiment_setpoint_range_a: tuple[float, float]
    ccd_friendly_name: str
    ccd_interface: str
    ccd_windows_driver_service: str
    ccd_capture_backend: str
    ccd_vision_gate_status: str
    recording_signal_names: tuple[str, ...]
    recording_start_condition: str
    minimum_effective_current_a: float
    minimum_setpoint_fraction: float
    maximum_setpoint_fraction: float
    minimum_z_limit_clearance_fraction: float
    minimum_z_limit_clearance_m: float
    arrival_hold_s: float
    arrival_minimum_samples: int
    far_noise_current_ceiling_a: float
    far_noise_window_s: float
    far_noise_minimum_samples: int
    far_noise_hold_s: float
    zctrl_withdraw_closes_segment: bool
    follow_me_gui_state_reflection: str
    follow_me_live_status: str
    follow_me_commissioned_on: str
    scan_start_live_status: str
    scan_start_commissioned_on: str
    scan_direction_readback: str
    coarse_minus_z_live_permission: str
    motor_move_enforcement: str
    evidence: tuple[InstrumentEvidence, ...]


def load_instrument_profile(
    profile_id: str = DEFAULT_INSTRUMENT_PROFILE_ID,
    *,
    profile_directory: str | Path | None = None,
) -> InstrumentProfile:
    _validate_profile_id(profile_id)
    directory = (
        DEFAULT_INSTRUMENT_PROFILE_DIRECTORY
        if profile_directory is None
        else Path(profile_directory).expanduser()
    )
    path = directory / f"{profile_id}.toml"
    with path.open("rb") as handle:
        payload = tomllib.load(handle)

    profile = _table(payload, "profile")
    z_controller = _table(payload, "z_controller")
    ccd = _table(payload, "ccd")
    recording = _table(payload, "recording")
    follow_me = _table(payload, "follow_me")
    scan_control = _table(payload, "scan_control")
    safety = _table(payload, "safety_policy_reference")

    if str(profile.get("id")) != profile_id:
        raise ValueError(f"{path} profile.id does not match its requested id")
    status = str(profile.get("status"))
    if status not in {"draft", "reviewed"}:
        raise ValueError(
            f"instrument profile {profile_id!r} status must be 'draft' or 'reviewed'"
        )

    raw_setpoint_range = z_controller.get("lowest_experiment_setpoint_range_a", ())
    if not isinstance(raw_setpoint_range, list):
        raise ValueError(
            "z_controller.lowest_experiment_setpoint_range_a must be a numeric pair"
        )
    low_setpoint_range = tuple(
        _finite_number(value, "z_controller.lowest_experiment_setpoint_range_a")
        for value in raw_setpoint_range
    )
    if (
        len(low_setpoint_range) != 2
        or not all(math.isfinite(value) for value in low_setpoint_range)
        or low_setpoint_range[0] <= 0.0
        or low_setpoint_range[1] < low_setpoint_range[0]
    ):
        raise ValueError(
            "z_controller.lowest_experiment_setpoint_range_a must be a "
            "positive ascending pair"
        )

    ccd_interface = str(ccd.get("interface"))
    ccd_driver = str(ccd.get("windows_driver_service"))
    ccd_capture_backend = str(ccd.get("capture_backend"))
    ccd_vision_gate_status = str(ccd.get("spire_vision_gate_status"))
    no_camera = (
        ccd_interface == "not_configured"
        and ccd_driver == "not_configured"
        and ccd_capture_backend == "unavailable"
        and ccd_vision_gate_status == "not_implemented"
    )
    if not no_camera:
        if ccd_interface != "UVC" or ccd_driver != "usbvideo":
            raise ValueError(
                "configured CCD profile must preserve the UVC/usbvideo interface; "
                "a site without a camera must use the complete not_configured state"
            )
        if ccd_capture_backend not in {
            "opencv_directshow_available",
            "unavailable",
        }:
            raise ValueError("unsupported CCD capture backend state")
        if ccd_vision_gate_status not in {
            "not_implemented",
            "commissioned",
        }:
            raise ValueError("unsupported CCD vision gate status")

    signal_names = tuple(str(value) for value in recording.get("signal_names", ()))
    if len(signal_names) not in (4, 6) or len(set(signal_names)) != len(signal_names) or not all(signal_names):
        raise ValueError("recording.signal_names must contain unique I, X, Y, Z names, optionally followed by lock-in X, Y (A)")
    start_condition = str(recording.get("start_condition"))
    if (
        start_condition
        != "z_controller_control_on_setpoint_window_and_z_inside_limits"
    ):
        raise ValueError(
            "only z_controller_control_on_setpoint_window_and_z_inside_limits "
            "is an approved recording start condition"
        )
    minimum_effective_current_a = _finite_number(
        recording.get("minimum_effective_current_a"),
        "recording.minimum_effective_current_a",
    )
    if minimum_effective_current_a <= 0.0:
        raise ValueError("recording.minimum_effective_current_a must be positive")
    minimum_setpoint_fraction = _finite_number(
        recording.get("minimum_setpoint_fraction"),
        "recording.minimum_setpoint_fraction",
    )
    if not 0.0 < minimum_setpoint_fraction <= 1.0:
        raise ValueError("recording.minimum_setpoint_fraction must be in (0, 1]")
    maximum_setpoint_fraction = _finite_number(
        recording.get("maximum_setpoint_fraction"),
        "recording.maximum_setpoint_fraction",
    )
    if maximum_setpoint_fraction < 1.0:
        raise ValueError("recording.maximum_setpoint_fraction must be at least 1")
    minimum_z_limit_clearance_fraction = _finite_number(
        recording.get("minimum_z_limit_clearance_fraction"),
        "recording.minimum_z_limit_clearance_fraction",
    )
    if not 0.0 <= minimum_z_limit_clearance_fraction < 0.5:
        raise ValueError(
            "recording.minimum_z_limit_clearance_fraction must be in [0, 0.5)"
        )
    minimum_z_limit_clearance_m = _finite_number(
        recording.get("minimum_z_limit_clearance_m"),
        "recording.minimum_z_limit_clearance_m",
    )
    if minimum_z_limit_clearance_m < 0.0:
        raise ValueError("recording.minimum_z_limit_clearance_m must be non-negative")
    arrival_hold_s = _finite_number(
        recording.get("arrival_hold_s"), "recording.arrival_hold_s"
    )
    if arrival_hold_s < 0.0:
        raise ValueError("recording.arrival_hold_s must be non-negative")
    arrival_minimum_samples = _positive_integer(
        recording.get("arrival_minimum_samples"),
        "recording.arrival_minimum_samples",
    )
    if arrival_minimum_samples < 1:
        raise ValueError("recording.arrival_minimum_samples must be positive")
    far_noise_current_ceiling_a = _finite_number(
        recording.get("far_noise_current_ceiling_a"),
        "recording.far_noise_current_ceiling_a",
    )
    if far_noise_current_ceiling_a <= 0.0:
        raise ValueError("recording.far_noise_current_ceiling_a must be positive")
    far_noise_window_s = _finite_number(
        recording.get("far_noise_window_s"), "recording.far_noise_window_s"
    )
    if far_noise_window_s <= 0.0:
        raise ValueError("recording.far_noise_window_s must be positive")
    far_noise_minimum_samples = _positive_integer(
        recording.get("far_noise_minimum_samples"),
        "recording.far_noise_minimum_samples",
    )
    if far_noise_minimum_samples < 1:
        raise ValueError("recording.far_noise_minimum_samples must be positive")
    far_noise_hold_s = _finite_number(
        recording.get("far_noise_hold_s"), "recording.far_noise_hold_s"
    )
    if far_noise_hold_s < 0.0:
        raise ValueError("recording.far_noise_hold_s must be non-negative")
    permission = str(safety.get("coarse_minus_z_live_permission"))
    if permission != "forbidden":
        raise ValueError("reviewed instrument profile must keep coarse -Z live permission forbidden")

    evidence = _evidence(
        payload.get("evidence"), allow_empty=status == "draft"
    )

    return InstrumentProfile(
        profile_id=profile_id,
        status=status,
        reviewed_on=str(profile.get("reviewed_on")),
        station=str(profile.get("station")),
        application=str(profile.get("application")),
        active_session_path=_resolve_profile_path(
            str(profile.get("active_session_path")), path.parent
        ),
        lowest_experiment_setpoint_range_a=(
            low_setpoint_range[0],
            low_setpoint_range[1],
        ),
        ccd_friendly_name=str(ccd.get("friendly_name")),
        ccd_interface=ccd_interface,
        ccd_windows_driver_service=ccd_driver,
        ccd_capture_backend=ccd_capture_backend,
        ccd_vision_gate_status=ccd_vision_gate_status,
        recording_signal_names=signal_names,
        recording_start_condition=start_condition,
        minimum_effective_current_a=minimum_effective_current_a,
        minimum_setpoint_fraction=minimum_setpoint_fraction,
        maximum_setpoint_fraction=maximum_setpoint_fraction,
        minimum_z_limit_clearance_fraction=minimum_z_limit_clearance_fraction,
        minimum_z_limit_clearance_m=minimum_z_limit_clearance_m,
        arrival_hold_s=arrival_hold_s,
        arrival_minimum_samples=arrival_minimum_samples,
        far_noise_current_ceiling_a=far_noise_current_ceiling_a,
        far_noise_window_s=far_noise_window_s,
        far_noise_minimum_samples=far_noise_minimum_samples,
        far_noise_hold_s=far_noise_hold_s,
        zctrl_withdraw_closes_segment=bool(
            recording.get("zctrl_withdraw_closes_segment")
        ),
        follow_me_gui_state_reflection=str(
            follow_me.get("nanonis_gui_state_reflection")
        ),
        follow_me_live_status=str(follow_me.get("live_status")),
        follow_me_commissioned_on=str(follow_me.get("commissioned_on")),
        scan_start_live_status=str(scan_control.get("start_live_status")),
        scan_start_commissioned_on=str(scan_control.get("commissioned_on")),
        scan_direction_readback=str(scan_control.get("direction_readback")),
        coarse_minus_z_live_permission=permission,
        motor_move_enforcement=str(safety.get("motor_move_enforcement")),
        evidence=evidence,
    )


def _table(payload: dict, key: str) -> dict:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"missing or invalid [{key}] table")
    return value


def _evidence(value, *, allow_empty: bool = False) -> tuple[InstrumentEvidence, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        requirement = "a list (and at least one entry when reviewed)"
        raise ValueError(f"instrument profile evidence must be {requirement}")
    evidence: list[InstrumentEvidence] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("instrument profile evidence entries must be tables")
        source = entry.get("source")
        evidence.append(
            InstrumentEvidence(
                kind=str(entry.get("kind")),
                observed_on=str(entry.get("observed_on")),
                source=None if source is None else str(source),
                fact=str(entry.get("fact")),
            )
        )
    return tuple(evidence)


def _validate_profile_id(profile_id: str) -> None:
    if not profile_id or len(profile_id) > 64:
        raise ValueError("instrument profile id must contain 1-64 characters")
    if any(
        character
        not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in profile_id
    ):
        raise ValueError("instrument profile id contains unsafe path characters")


def _finite_number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _positive_integer(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _resolve_profile_path(value: str, profile_directory: Path) -> Path:
    if not value.strip():
        raise ValueError("profile.active_session_path must be a nonempty path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = profile_directory / path
    return path.resolve(strict=False)
