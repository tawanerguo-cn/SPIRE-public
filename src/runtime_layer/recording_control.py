from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

from knowledge_base import (
    DEFAULT_GUI_ACTION_BINDING_PROFILE_ID,
    DEFAULT_INSTRUMENT_PROFILE_ID,
    InstrumentProfile,
    load_instrument_profile,
)


DEFAULT_RECORDING_CONFIG = Path(__file__).resolve().parents[2] / "config" / "recording.toml"
DEFAULT_LOCAL_RECORDING_CONFIG = (
    Path(__file__).resolve().parents[2] / "config" / "recording.local.toml"
)


@dataclass(frozen=True)
class RecordingSettings:
    enabled: bool
    instrument_profile: InstrumentProfile
    root: Path
    storage_quota_gib: float
    storage_warning_gib: float
    gate_mode: str
    gate_poll_interval_s: float
    action_audit_start_mode: str
    observe_external_state: bool
    external_state_poll_interval_s: float
    observe_gui_actions: bool
    gui_action_binding_profile: str
    status_port: int | None
    data_logger_enabled: bool

    @property
    def storage_quota_bytes(self) -> int:
        return int(self.storage_quota_gib * 1024**3)

    @property
    def storage_warning_bytes(self) -> int:
        return int(self.storage_warning_gib * 1024**3)

    @property
    def signals(self) -> tuple[str, ...]:
        return self.instrument_profile.recording_signal_names

    @property
    def minimum_effective_current_a(self) -> float:
        return self.instrument_profile.minimum_effective_current_a

    @property
    def minimum_setpoint_fraction(self) -> float:
        return self.instrument_profile.minimum_setpoint_fraction

    @property
    def maximum_setpoint_fraction(self) -> float:
        return self.instrument_profile.maximum_setpoint_fraction

    @property
    def minimum_z_limit_clearance_fraction(self) -> float:
        return self.instrument_profile.minimum_z_limit_clearance_fraction

    @property
    def minimum_z_limit_clearance_m(self) -> float:
        return self.instrument_profile.minimum_z_limit_clearance_m

    @property
    def arrival_hold_s(self) -> float:
        return self.instrument_profile.arrival_hold_s

    @property
    def arrival_minimum_samples(self) -> int:
        return self.instrument_profile.arrival_minimum_samples

    @property
    def far_noise_current_ceiling_a(self) -> float:
        return self.instrument_profile.far_noise_current_ceiling_a

    @property
    def far_noise_window_s(self) -> float:
        return self.instrument_profile.far_noise_window_s

    @property
    def far_noise_minimum_samples(self) -> int:
        return self.instrument_profile.far_noise_minimum_samples

    @property
    def far_noise_hold_s(self) -> float:
        return self.instrument_profile.far_noise_hold_s

def load_recording_settings(
    config_path: str | Path | None = None,
    local_path: str | Path | None = None,
) -> RecordingSettings:
    base = DEFAULT_RECORDING_CONFIG if config_path is None else Path(config_path).expanduser()
    values = _load_recording_table(base)
    value_sources = {key: base for key in values}
    local = (
        base.with_name("recording.local.toml")
        if local_path is None
        else Path(local_path).expanduser()
    )
    if local.exists():
        local_values = _load_recording_table(local)
        values.update(local_values)
        value_sources.update({key: local for key in local_values})
    profile_id = str(
        values.get("instrument_profile", DEFAULT_INSTRUMENT_PROFILE_ID)
    )
    profile_directory_value = values.get("instrument_profile_directory")
    profile_directory = (
        None
        if profile_directory_value is None
        else _resolve_config_relative_path(
            str(profile_directory_value),
            value_sources["instrument_profile_directory"].parent,
        )
    )
    instrument_profile = load_instrument_profile(
        profile_id, profile_directory=profile_directory
    )
    root_value = Path(str(values["root"])).expanduser()
    root = (
        root_value
        if root_value.is_absolute()
        else (value_sources["root"].parent / root_value).resolve(strict=False)
    )
    storage_quota_gib = float(values.get("storage_quota_gib", 100.0))
    storage_warning_gib = float(values.get("storage_warning_gib", 95.0))
    if storage_quota_gib < 1.0:
        raise ValueError("storage_quota_gib must be at least 1")
    if not 0.0 < storage_warning_gib < storage_quota_gib:
        raise ValueError("storage_warning_gib must be positive and below storage_quota_gib")
    gate_mode = str(values.get("gate_mode", "near_sample"))
    if gate_mode not in {"near_sample", "always"}:
        raise ValueError("gate_mode must be 'near_sample' or 'always'")
    gate_poll_interval_s = float(values.get("gate_poll_interval_s", 0.25))
    if gate_poll_interval_s <= 0.0:
        raise ValueError("gate_poll_interval_s must be positive")
    action_audit_start_mode = str(
        values.get("action_audit_start_mode", "tip_near_sample")
    )
    if action_audit_start_mode not in {
        "approach_evidence",
        "tip_near_sample",
        "recorder_start",
    }:
        raise ValueError(
            "action_audit_start_mode must be 'tip_near_sample', "
            "'approach_evidence', or 'recorder_start'"
        )
    external_state_poll_interval_s = float(
        values.get("external_state_poll_interval_s", 0.1)
    )
    if external_state_poll_interval_s <= 0.0:
        raise ValueError(
            "external_state_poll_interval_s must be positive"
        )
    raw_status_port = values.get("status_port")
    status_port = (
        None if raw_status_port is None else int(raw_status_port)
    )
    if status_port is not None and not 1 <= status_port <= 65535:
        raise ValueError("status_port must be between 1 and 65535")
    return RecordingSettings(
        enabled=bool(values.get("enabled", False)),
        instrument_profile=instrument_profile,
        root=root,
        storage_quota_gib=storage_quota_gib,
        storage_warning_gib=storage_warning_gib,
        gate_mode=gate_mode,
        gate_poll_interval_s=gate_poll_interval_s,
        action_audit_start_mode=action_audit_start_mode,
        observe_external_state=bool(
            values.get("observe_external_state", True)
        ),
        external_state_poll_interval_s=external_state_poll_interval_s,
        observe_gui_actions=bool(values.get("observe_gui_actions", False)),
        gui_action_binding_profile=str(
            values.get(
                "gui_action_binding_profile",
                DEFAULT_GUI_ACTION_BINDING_PROFILE_ID,
            )
        ),
        status_port=status_port,
        data_logger_enabled=bool(
            values.get("data_logger_enabled", False)
        ),
    )


def set_recording_switch(
    enabled: bool,
    local_path: str | Path = DEFAULT_LOCAL_RECORDING_CONFIG,
) -> Path:
    path = Path(local_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = path.read_text(encoding="utf-8") if path.exists() else "[recording]\n"
    payload = tomllib.loads(content)
    table = payload.get("recording")
    if not isinstance(table, dict):
        raise ValueError(f"{path} must contain a [recording] table")
    table["enabled"] = bool(enabled)
    value = "true" if enabled else "false"
    # Preserve per-PC ports, paths and comments. Validate the entire parsed
    # document before writing, so a matching line in another table or a
    # multiline string can never silently change unrelated settings.
    switch_pattern = r"(?m)^[ \t]*(?:enabled|\"enabled\"|'enabled')[ \t]*=[ \t]*(true|false)\b"
    candidates = [
        content[:match.start(1)] + value + content[match.end(1):]
        for match in re.finditer(switch_pattern, content)
    ]
    header_pattern = r"(?m)^[ \t]*\[[ \t]*(?:recording|\"recording\"|'recording')[ \t]*\][ \t]*(?:#[^\n]*)?(?:\n|$)"
    candidates.extend(
        content[:match.end()] + f"\nenabled = {value}\n" + content[match.end():]
        for match in re.finditer(header_pattern, content)
    )
    updated = None
    for candidate in candidates:
        try:
            if tomllib.loads(candidate) == payload:
                updated = candidate
                break
        except tomllib.TOMLDecodeError:
            continue
    if updated is None:
        raise ValueError(f"Cannot safely update enabled in {path}; use a [recording] table")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    temporary.replace(path)
    return path
def _load_recording_table(path: Path) -> dict:
    with path.open("rb") as handle:
        payload = tomllib.load(handle)
    table = payload.get("recording")
    if not isinstance(table, dict):
        raise ValueError(f"{path} must contain a [recording] table")
    return dict(table)


def _resolve_config_relative_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)
