from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import tempfile
from threading import Thread
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from instrument_layer import (  # noqa: E402
    ActionActor,
    ActionName,
    ActionPhase,
    DesktopMouseClick,
    DesktopWindow,
    FakeNanonisBackend,
    MouseClickAssembler,
    NanonisDevice,
)
from knowledge_base import load_gui_action_binding_profile  # noqa: E402
from workflow_layer import GuiActionObserver, TelemetryRecorder  # noqa: E402


class FakeClickSource:
    def __init__(self) -> None:
        self.callback = None
        self.started = False
        self.stopped = False
        self.failed = False
        self.error = None
        self.dropped_events = 0

    def start(self, callback) -> None:
        self.callback = callback
        self.started = True

    def stop(self, *, raise_on_failure: bool = True) -> None:
        self.stopped = True

    def emit(self, click: DesktopMouseClick) -> None:
        assert self.callback is not None
        self.callback(click)


class FailingClickSource(FakeClickSource):
    def start(self, callback) -> None:
        raise RuntimeError("synthetic hook failure")


class FakeWindowLocator:
    def __init__(self, window: DesktopWindow | None) -> None:
        self.window = window
        self.calls = 0

    def window_at(self, x_px: int, y_px: int) -> DesktopWindow | None:
        self.calls += 1
        return self.window


def motor_window() -> DesktopWindow:
    return DesktopWindow(
        handle=123,
        process_id=456,
        process_executable="Nanonis Mimea.exe",
        title="Motor Control",
        class_name="LVDChild",
        left_px=0,
        top_px=0,
        right_px=396,
        bottom_px=755,
    )


def click_at(
    x: int,
    y: int,
    *,
    duration_ms: float = 100.0,
) -> DesktopMouseClick:
    duration_ns = round(duration_ms * 1_000_000)
    return DesktopMouseClick(
        button="left",
        down_x_px=x,
        down_y_px=y,
        up_x_px=x,
        up_y_px=y,
        down_monotonic_ns=1_000_000_000,
        up_monotonic_ns=1_000_000_000 + duration_ns,
        up_utc_ns=2_000_000_000 + duration_ns,
    )


class MouseClickAssemblerTests(unittest.TestCase):
    def test_pairs_only_completed_physical_left_clicks(self) -> None:
        assembler = MouseClickAssembler()
        self.assertIsNone(
            assembler.feed(
                kind="left_down",
                x_px=10,
                y_px=20,
                monotonic_ns=100,
                utc_ns=1000,
                injected=False,
            )
        )
        click = assembler.feed(
            kind="left_up",
            x_px=11,
            y_px=21,
            monotonic_ns=500,
            utc_ns=1400,
            injected=False,
        )
        self.assertIsNotNone(click)
        self.assertEqual(click.down_x_px, 10)
        self.assertEqual(click.up_x_px, 11)

    def test_injected_input_is_dropped_and_cancels_pending_click(self) -> None:
        assembler = MouseClickAssembler()
        assembler.feed(
            kind="left_down",
            x_px=10,
            y_px=20,
            monotonic_ns=100,
            utc_ns=1000,
            injected=False,
        )
        self.assertIsNone(
            assembler.feed(
                kind="left_up",
                x_px=10,
                y_px=20,
                monotonic_ns=200,
                utc_ns=1100,
                injected=True,
            )
        )


class GuiActionObserverTests(unittest.TestCase):
    def build_observer(self, window: DesktopWindow | None = None):
        source = FakeClickSource()
        locator = FakeWindowLocator(motor_window() if window is None else window)
        events = []
        observer = GuiActionObserver(
            source,
            locator,
            load_gui_action_binding_profile(),
        )
        observer.action_events.set_sink(events.append)
        observer.start()
        return observer, source, locator, events

    def test_physical_x_positive_click_records_one_correlated_operator_action(self) -> None:
        observer, source, locator, events = self.build_observer()

        source.emit(click_at(236, 337))
        observer.stop()

        self.assertEqual(locator.calls, 1)
        self.assertEqual(observer.matched_clicks, 1)
        self.assertEqual(observer.binding_counts, {"pmd4_x_positive": 1})
        self.assertEqual(len(events), 2)
        self.assertEqual(
            [event.phase for event in events],
            [ActionPhase.BEFORE, ActionPhase.AFTER],
        )
        self.assertEqual({event.action_id for event in events}, {events[0].action_id})
        self.assertEqual(events[0].action_name, ActionName.COARSE_XY_MOVE)
        self.assertEqual(events[0].actor, ActionActor.OPERATOR)
        arguments = events[0].arguments.value
        self.assertEqual(arguments["axis"], "x")
        self.assertEqual(arguments["direction"], "+")
        self.assertEqual(arguments["interaction_mode"], "click")
        self.assertNotIn("steps", arguments)
        self.assertFalse(events[1].result.value["physical_motion_verified"])
        self.assertFalse(
            events[1].result.value["position_or_step_reconstruction_attempted"]
        )

    def test_long_y_negative_press_is_labeled_continuous_without_step_claim(self) -> None:
        observer, source, _, events = self.build_observer()

        source.emit(click_at(215, 358, duration_ms=700.0))
        observer.stop()

        arguments = events[0].arguments.value
        self.assertEqual(arguments["axis"], "y")
        self.assertEqual(arguments["direction"], "-")
        self.assertEqual(arguments["interaction_mode"], "continuous_hold")
        self.assertNotIn("steps", arguments)

    def test_click_outside_mapped_controls_is_ignored(self) -> None:
        observer, source, locator, events = self.build_observer()

        source.emit(click_at(100, 100))
        observer.stop()

        self.assertEqual(locator.calls, 1)
        self.assertEqual(observer.ignored_clicks, 1)
        self.assertEqual(events, [])

    def test_drag_out_of_control_is_ignored(self) -> None:
        observer, source, _, events = self.build_observer()
        click = click_at(236, 337)
        source.emit(
            DesktopMouseClick(
                **{
                    **click.__dict__,
                    "up_x_px": 100,
                    "up_y_px": 100,
                }
            )
        )
        observer.stop()

        self.assertEqual(observer.ignored_clicks, 1)
        self.assertEqual(events, [])

    def test_other_application_with_same_window_shape_is_ignored(self) -> None:
        other = motor_window()
        other = DesktopWindow(
            **{
                **other.__dict__,
                "process_executable": "Other.exe",
            }
        )
        observer, source, _, events = self.build_observer(other)

        source.emit(click_at(236, 337))
        observer.stop()

        self.assertEqual(observer.ignored_clicks, 1)
        self.assertEqual(events, [])

    def test_recorder_binds_observer_to_same_durable_run_store(self) -> None:
        names = [f"Signal {index}" for index in range(31)]
        names[0], names[28], names[29], names[30] = (
            "Current (A)",
            "X (m)",
            "Y (m)",
            "Z (m)",
        )
        device = NanonisDevice(
            backend=FakeNanonisBackend(
                signal_names=names,
                signal_values={
                    0: -48e-12,
                    28: -481e-9,
                    29: -442e-9,
                    30: 128e-9,
                },
            )
        )
        source = FakeClickSource()
        observer = GuiActionObserver(
            source,
            FakeWindowLocator(motor_window()),
            load_gui_action_binding_profile(),
        )
        result_box = {}
        with tempfile.TemporaryDirectory() as tmp:
            recorder = TelemetryRecorder(
                device,
                tmp,
                signal_names=(
                    "Current (A)",
                    "X (m)",
                    "Y (m)",
                    "Z (m)",
                ),
                storage_quota_bytes=10 * 1024**2,
                storage_warning_bytes=9 * 1024**2,
                gui_action_observer=observer,
            )
            runner = Thread(
                target=lambda: result_box.setdefault(
                    "result",
                    recorder.record(duration_s=0.4, run_id="gui-audit"),
                )
            )
            runner.start()
            deadline = time.monotonic() + 1.0
            while source.callback is None and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertIsNotNone(source.callback)
            source.emit(click_at(236, 337))
            runner.join(timeout=2.0)
            self.assertFalse(runner.is_alive())
            result = result_box["result"]
            connection = sqlite3.connect(result.database_path)
            try:
                rows = connection.execute(
                    """
                    SELECT action_name, phase, actor, source
                    FROM action_events
                    WHERE action_name = 'coarse_xy_move'
                    ORDER BY action_event_id
                    """
                ).fetchall()
                summary = connection.execute(
                    """
                    SELECT payload_json FROM events
                    WHERE event_type = 'gui_action_observer_summary'
                    """
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(
            rows,
            [
                (
                    "coarse_xy_move",
                    "before",
                    "operator",
                    "GuiActionObserver:pmd4_x_positive",
                ),
                (
                    "coarse_xy_move",
                    "after",
                    "operator",
                    "GuiActionObserver:pmd4_x_positive",
                ),
            ],
        )
        self.assertIsNotNone(summary)
        self.assertTrue(source.stopped)

    def test_gui_observer_start_failure_does_not_stop_ixyz_recording(self) -> None:
        names = [f"Signal {index}" for index in range(31)]
        names[0], names[28], names[29], names[30] = (
            "Current (A)",
            "X (m)",
            "Y (m)",
            "Z (m)",
        )
        source = FailingClickSource()
        observer = GuiActionObserver(
            source,
            FakeWindowLocator(motor_window()),
            load_gui_action_binding_profile(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = TelemetryRecorder(
                NanonisDevice(
                    backend=FakeNanonisBackend(
                        signal_names=names,
                        signal_values={
                            0: -48e-12,
                            28: -481e-9,
                            29: -442e-9,
                            30: 128e-9,
                        },
                    )
                ),
                tmp,
                signal_names=(
                    "Current (A)",
                    "X (m)",
                    "Y (m)",
                    "Z (m)",
                ),
                storage_quota_bytes=10 * 1024**2,
                storage_warning_bytes=9 * 1024**2,
                gui_action_observer=observer,
            ).record(duration_s=0.1, run_id="gui-failure")
            connection = sqlite3.connect(result.database_path)
            try:
                failed = connection.execute(
                    """
                    SELECT payload_json FROM events
                    WHERE event_type = 'recording_component_failed'
                    """
                ).fetchall()
            finally:
                connection.close()

        self.assertGreater(result.samples, 0)
        self.assertEqual(result.status, "recording_compromised")
        self.assertTrue(
            any("gui_action_observer" in payload for (payload,) in failed)
        )
        self.assertTrue(source.stopped)


if __name__ == "__main__":
    unittest.main()
