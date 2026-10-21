from __future__ import annotations

from collections import defaultdict
from threading import Lock

from instrument_layer import (
    ActionActor,
    ActionEventEmitter,
    ActionIntent,
    ActionName,
    DesktopClickSource,
    DesktopMouseClick,
    DesktopWindow,
    DesktopWindowLocator,
)
from knowledge_base import GuiActionBinding, GuiActionBindingProfile


class GuiActionObserver:
    """Map completed physical GUI clicks to typed operator action events.

    The observer never injects input, screenshots a window, runs OCR, or polls a
    control tree. One window lookup and an indexed rectangle match occur only
    after a complete physical left click.
    """

    def __init__(
        self,
        click_source: DesktopClickSource,
        window_locator: DesktopWindowLocator,
        profile: GuiActionBindingProfile,
        *,
        action_events: ActionEventEmitter | None = None,
    ) -> None:
        self.click_source = click_source
        self.window_locator = window_locator
        self.profile = profile
        self.action_events = action_events or ActionEventEmitter()
        self._bindings_by_window = self._index_bindings(profile.bindings)
        self._started = False
        self._matched_clicks = 0
        self._binding_counts: dict[str, int] = defaultdict(int)
        self._ignored_clicks = 0
        self._ambiguous_clicks = 0
        self._state_lock = Lock()

    @property
    def failed(self) -> bool:
        return self.click_source.failed

    @property
    def error(self) -> BaseException | None:
        return self.click_source.error

    @property
    def matched_clicks(self) -> int:
        with self._state_lock:
            return self._matched_clicks

    @property
    def ignored_clicks(self) -> int:
        with self._state_lock:
            return self._ignored_clicks

    @property
    def binding_counts(self) -> dict[str, int]:
        with self._state_lock:
            return dict(self._binding_counts)

    @property
    def ambiguous_clicks(self) -> int:
        with self._state_lock:
            return self._ambiguous_clicks

    @property
    def dropped_input_events(self) -> int:
        return self.click_source.dropped_events

    def start(self) -> None:
        if self._started:
            raise RuntimeError("GUI action observer has already been started")
        try:
            self.click_source.start(self._observe_click)
        except Exception:
            try:
                self.click_source.stop(raise_on_failure=False)
            except Exception:
                pass
            raise
        self._started = True

    def stop(self, *, raise_on_failure: bool = True) -> None:
        if not self._started:
            return
        self.click_source.stop(raise_on_failure=raise_on_failure)
        self._started = False

    def _observe_click(self, click: DesktopMouseClick) -> None:
        # Resolve only at release. Down and up must both remain inside the same
        # mapped rectangle; this prevents drags across controls becoming clicks.
        window = self.window_locator.window_at(click.up_x_px, click.up_y_px)
        if window is None:
            self._increment("ignored")
            return
        bindings = self._bindings_by_window.get(self._window_key(window), ())
        if not bindings:
            self._increment("ignored")
            return
        down = window.normalized_point(click.down_x_px, click.down_y_px)
        up = window.normalized_point(click.up_x_px, click.up_y_px)
        matches = tuple(
            binding
            for binding in bindings
            if binding.region.contains(*down) and binding.region.contains(*up)
        )
        if len(matches) != 1:
            self._increment("ambiguous" if matches else "ignored")
            return

        binding = matches[0]
        hold_threshold = binding.continuous_hold_threshold_ms
        continuous = (
            hold_threshold is not None
            and click.duration_ms >= hold_threshold
        )
        arguments = {
            **dict(binding.arguments),
            "binding_profile_id": self.profile.profile_id,
            "binding_id": binding.binding_id,
            "control_label": binding.control_label,
            "interaction_mode": "continuous_hold" if continuous else "click",
            "press_duration_ms": click.duration_ms,
            "window": {
                "application_executable": window.process_executable,
                "title": window.title,
                "class_name": window.class_name,
            },
            "input_evidence": {
                "kind": "physical_left_mouse_click",
                "injected": False,
                "down_screen_px": [click.down_x_px, click.down_y_px],
                "up_screen_px": [click.up_x_px, click.up_y_px],
                "down_monotonic_ns": click.down_monotonic_ns,
                "up_monotonic_ns": click.up_monotonic_ns,
                "completed_utc_ns": click.up_utc_ns,
            },
        }
        self.action_events.record_instantaneous(
            ActionName(binding.action_name),
            arguments=arguments,
            result={
                "operator_input_observed": True,
                "physical_motion_verified": False,
                "position_or_step_reconstruction_attempted": False,
            },
            source=f"GuiActionObserver:{binding.binding_id}",
            actor=ActionActor.OPERATOR,
            intent=ActionIntent.EXECUTE,
        )
        with self._state_lock:
            self._matched_clicks += 1
            self._binding_counts[binding.binding_id] += 1

    @staticmethod
    def _index_bindings(bindings: tuple[GuiActionBinding, ...]):
        indexed: dict[tuple[str, str, str], list[GuiActionBinding]] = defaultdict(list)
        for binding in bindings:
            # Fail at assembly, rather than after a live operator click, if the
            # reviewed map names an event outside the typed action vocabulary.
            ActionName(binding.action_name)
            key = (
                binding.application_executable.casefold(),
                binding.window_title,
                binding.window_class,
            )
            indexed[key].append(binding)
        return {key: tuple(value) for key, value in indexed.items()}

    @staticmethod
    def _window_key(window: DesktopWindow) -> tuple[str, str, str]:
        return (
            window.process_executable.casefold(),
            window.title,
            window.class_name,
        )

    def _increment(self, counter: str) -> None:
        with self._state_lock:
            if counter == "matched":
                self._matched_clicks += 1
            elif counter == "ambiguous":
                self._ambiguous_clicks += 1
            else:
                self._ignored_clicks += 1
