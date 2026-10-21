from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
import tomllib


DEFAULT_GUI_ACTION_BINDING_DIRECTORY = (
    Path(__file__).resolve().parent / "gui_action_bindings"
)
DEFAULT_GUI_ACTION_BINDING_PROFILE_ID = "spire_local_nanonis"


@dataclass(frozen=True)
class NormalizedGuiRegion:
    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self) -> None:
        if not (
            0.0 <= self.left < self.right <= 1.0
            and 0.0 <= self.top < self.bottom <= 1.0
        ):
            raise ValueError(
                "normalized GUI region must be an ordered rectangle inside [0, 1]"
            )

    def contains(self, x: float, y: float) -> bool:
        return self.left <= x <= self.right and self.top <= y <= self.bottom


@dataclass(frozen=True)
class GuiActionBinding:
    binding_id: str
    application_executable: str
    window_title: str
    window_class: str
    action_name: str
    control_label: str
    region: NormalizedGuiRegion
    arguments: Mapping[str, str | int | float | bool]
    continuous_hold_threshold_ms: float | None


@dataclass(frozen=True)
class GuiActionBindingProfile:
    profile_id: str
    status: str
    reviewed_on: str
    coordinate_system: str
    bindings: tuple[GuiActionBinding, ...]


def load_gui_action_binding_profile(
    profile_id: str = DEFAULT_GUI_ACTION_BINDING_PROFILE_ID,
    *,
    profile_directory: str | Path = DEFAULT_GUI_ACTION_BINDING_DIRECTORY,
) -> GuiActionBindingProfile:
    _validate_profile_id(profile_id)
    path = Path(profile_directory) / f"{profile_id}.toml"
    with path.open("rb") as handle:
        payload = tomllib.load(handle)

    profile = payload.get("profile")
    if not isinstance(profile, dict):
        raise ValueError(f"{path} must contain a [profile] table")
    if str(profile.get("id")) != profile_id:
        raise ValueError(f"{path} profile.id does not match its requested id")
    status = str(profile.get("status"))
    if status != "reviewed":
        raise ValueError(f"GUI binding profile {profile_id!r} is not reviewed")
    coordinate_system = str(profile.get("coordinate_system"))
    if coordinate_system != "whole_window_normalized":
        raise ValueError("only whole_window_normalized GUI mappings are supported")

    raw_bindings = payload.get("binding")
    if not isinstance(raw_bindings, list) or not raw_bindings:
        raise ValueError("GUI binding profile must contain [[binding]] entries")
    bindings: list[GuiActionBinding] = []
    seen_ids: set[str] = set()
    for raw in raw_bindings:
        if not isinstance(raw, dict):
            raise ValueError("GUI binding entries must be tables")
        binding_id = str(raw.get("id"))
        if not binding_id or binding_id in seen_ids:
            raise ValueError("GUI binding ids must be non-empty and unique")
        seen_ids.add(binding_id)
        region_values = raw.get("normalized_region")
        if not isinstance(region_values, list) or len(region_values) != 4:
            raise ValueError(
                f"GUI binding {binding_id!r} normalized_region must have four values"
            )
        arguments = raw.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError(f"GUI binding {binding_id!r} arguments must be a table")
        threshold = raw.get("continuous_hold_threshold_ms")
        threshold_ms = None if threshold is None else float(threshold)
        if threshold_ms is not None and threshold_ms <= 0.0:
            raise ValueError("continuous_hold_threshold_ms must be positive")
        bindings.append(
            GuiActionBinding(
                binding_id=binding_id,
                application_executable=str(raw.get("application_executable")),
                window_title=str(raw.get("window_title")),
                window_class=str(raw.get("window_class")),
                action_name=str(raw.get("action_name")),
                control_label=str(raw.get("control_label")),
                region=NormalizedGuiRegion(
                    *(float(value) for value in region_values)
                ),
                arguments=MappingProxyType(dict(arguments)),
                continuous_hold_threshold_ms=threshold_ms,
            )
        )
    return GuiActionBindingProfile(
        profile_id=profile_id,
        status=status,
        reviewed_on=str(profile.get("reviewed_on")),
        coordinate_system=coordinate_system,
        bindings=tuple(bindings),
    )


def _validate_profile_id(profile_id: str) -> None:
    if not profile_id or len(profile_id) > 64:
        raise ValueError("GUI binding profile id must contain 1-64 characters")
    if any(
        character
        not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in profile_id
    ):
        raise ValueError("GUI binding profile id contains unsafe path characters")
