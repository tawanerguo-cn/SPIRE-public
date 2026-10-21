from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Event, Thread
import time
import unittest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analysis_layer import TipProximityPolicy  # noqa: E402
from data_layer import ExperimentMetadata  # noqa: E402
from instrument_layer import (  # noqa: E402
    ActionActor,
    ActionEventEmitter,
    ActionIntent,
    ActionName,
    ActionPhase,
    BiasSpectroscopyRecipe,
    BiasSpectroscopyTiming,
    FakeNanonisBackend,
    NanonisDevice,
    SafetyGateError,
    ZControllerStatus,
    ZPokeRecipe,
)
from runtime_layer import WorkflowPauseController  # noqa: E402
from workflow_layer import (  # noqa: E402
    ActionAuditStartMode,
    ExternalStateObserver,
    RecordingGateSnapshot,
    TelemetryRecorder,
    TipRecordingGateMonitor,
)


TEST_SIGNALS = ("Current (A)", "X (m)", "Y (m)", "Z (m)")


def telemetry_backend() -> FakeNanonisBackend:
    names = [f"Signal {index}" for index in range(31)]
    names[0], names[28], names[29], names[30] = TEST_SIGNALS
    return FakeNanonisBackend(
        signal_names=names,
        signal_values={
            0: -100e-12,
            28: 1e-9,
            29: 2e-9,
            30: 3e-9,
        },
    )


def wait_until(predicate, *, timeout_s: float = 1.0) -> None:
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("timed out waiting for test state")


class TypedActionEventTests(unittest.TestCase):
    def test_device_actions_and_pause_emit_correlated_before_after(self) -> None:
        events = []
        backend = FakeNanonisBackend(scan_running=False)
        device = NanonisDevice(
            backend=backend,
            action_event_sink=events.append,
        )
        pause = WorkflowPauseController(
            ActionEventEmitter(events.append)
        )
        center_x, center_y = backend.scan_frame[:2]

        device.set_bias_v(-1.0)
        device.set_z_setpoint_a(3e-12)
        device.move_tip_xy(center_x, center_y)
        device.acquire_scan(0, timeout_ms=1000)
        device.acquire_bias_spectrum_transaction(
            BiasSpectroscopyRecipe(
                start_bias_v=-0.1,
                end_bias_v=0.1,
                number_of_sweeps=1,
                number_of_points=5,
                channel_indexes=(0, 1),
                timing=BiasSpectroscopyTiming(
                    0.01,
                    0.0,
                    0.01,
                    10.0,
                    0.001,
                    0.002,
                    0.01,
                    0.01,
                ),
            )
        )
        backend.z_controller_on = False
        device.run_z_poke(
            ZPokeRecipe(delta_z_m=-0.5e-9, duration_ms=0.1)
        )
        pause.pause("operator inspection")
        pause.resume("inspection complete")
        device.withdraw_tip(wait=True, timeout_ms=1234)

        by_name = defaultdict(list)
        for event in events:
            by_name[event.action_name].append(event)
        expected = {
            ActionName.BIAS_SET,
            ActionName.Z_SETPOINT_SET,
            ActionName.FOLLOW_ME_MOVE,
            ActionName.SCAN_ACQUIRE,
            ActionName.BIAS_SPECTROSCOPY_ACQUIRE,
            ActionName.Z_POKE,
            ActionName.WORKFLOW_PAUSE,
            ActionName.WORKFLOW_RESUME,
            ActionName.WITHDRAW,
        }
        self.assertTrue(expected <= set(by_name))
        for action_name in expected:
            phases = [event.phase for event in by_name[action_name]]
            self.assertEqual(phases, [ActionPhase.BEFORE, ActionPhase.AFTER])
            self.assertEqual(
                len({event.action_id for event in by_name[action_name]}),
                1,
            )
        self.assertEqual(
            by_name[ActionName.WITHDRAW][0].intent,
            ActionIntent.PROTECTIVE,
        )
        self.assertFalse(
            by_name[ActionName.SCAN_ACQUIRE][1].result.truncated
        )
        self.assertEqual(backend.bias_v, -1.0)
        self.assertEqual(backend.z_setpoint_a, 3e-12)

    def test_failed_action_is_correlated_and_pre_event_is_fail_closed(
        self,
    ) -> None:
        events = []
        backend = FakeNanonisBackend(scan_running=False)
        device = NanonisDevice(
            backend=backend,
            action_event_sink=events.append,
        )
        center_x, center_y = backend.scan_frame[:2]

        with self.assertRaises(SafetyGateError):
            device.move_tip_xy(center_x + 100e-9, center_y)

        self.assertEqual(
            [event.phase for event in events],
            [ActionPhase.BEFORE, ActionPhase.FAILED],
        )
        self.assertEqual(events[0].action_id, events[1].action_id)
        self.assertEqual(events[1].error_type, "SafetyGateError")
        self.assertNotIn(
            "FolMe_XYPosSet",
            [name for name, _arguments in backend.command_log],
        )

        operation_called = False

        def unavailable_sink(_event) -> None:
            raise OSError("audit storage unavailable")

        def operation() -> None:
            nonlocal operation_called
            operation_called = True

        with self.assertRaises(OSError):
            ActionEventEmitter(unavailable_sink).execute(
                ActionName.BIAS_SET,
                arguments={"target_bias_v": -1.0},
                operation=operation,
                source="test",
            )
        self.assertFalse(operation_called)

        protective_backend = FakeNanonisBackend()
        protective_device = NanonisDevice(
            backend=protective_backend,
            action_event_sink=unavailable_sink,
        )
        protective_device.withdraw_tip()
        self.assertIn(
            "ZCtrl_Withdraw",
            [name for name, _arguments in protective_backend.command_log],
        )

    def test_external_observer_never_claims_actor_or_intent(self) -> None:
        events = []
        snapshot_ready = Event()

        def capture(event):
            events.append(event)
            if event.action_name is ActionName.INSTRUMENT_STATE_SNAPSHOT:
                snapshot_ready.set()

        backend = FakeNanonisBackend(scan_running=False)
        observer = ExternalStateObserver(
            NanonisDevice(backend=backend),
            ActionEventEmitter(capture),
            poll_interval_s=0.01,
        )

        def wait_for_poll():
            snapshot_ready.clear()
            observer.request_snapshot()
            self.assertTrue(snapshot_ready.wait(2), "observer did not finish a requested poll")
            observer.raise_if_failed()

        observer.start()
        try:
            wait_for_poll()
            backend.bias_v = -1.25
            backend.z_setpoint_a = 5e-12
            backend.follow_me_position = (10e-9, -10e-9)
            # Observe the idle move before scanning takes ownership of XY.
            wait_for_poll()
            backend.follow_me_position = (20e-9, -20e-9)
            backend.scan_running = True
            backend.bias_spectroscopy_running = True
            backend.auto_approach_running = True
            wait_for_poll()
        finally:
            observer.stop()

        names = {event.action_name for event in events}
        self.assertTrue(
            {
                ActionName.BIAS_SET,
                ActionName.Z_SETPOINT_SET,
                ActionName.FOLLOW_ME_MOVE,
                ActionName.SCAN_STATE_CHANGE,
                ActionName.BIAS_SPECTROSCOPY_STATE_CHANGE,
                ActionName.AUTO_APPROACH_STATE_CHANGE,
            }
            <= names
        )
        self.assertTrue(
            all(event.phase is ActionPhase.OBSERVED for event in events)
        )
        self.assertTrue(
            all(event.actor is ActionActor.UNKNOWN for event in events)
        )
        self.assertTrue(
            all(event.intent is ActionIntent.UNKNOWN for event in events)
        )
        self.assertEqual(
            sum(
                event.action_name is ActionName.FOLLOW_ME_MOVE
                for event in events
            ),
            1,
        )

        snapshot = observer.emit_snapshot()
        self.assertEqual(snapshot.bias_v, backend.bias_v)
        snapshot_event = events[-1]
        self.assertIs(
            snapshot_event.action_name,
            ActionName.INSTRUMENT_STATE_SNAPSHOT,
        )
        self.assertEqual(
            snapshot_event.arguments.value["after"]["bias_v"],
            backend.bias_v,
        )

    def test_external_observer_reuses_gate_snapshot_for_overlapping_state(
        self,
    ) -> None:
        events = []
        backend = FakeNanonisBackend(scan_running=False)
        shared = RecordingGateSnapshot(
            monotonic_ns=time.perf_counter_ns(),
            current_a=-3e-12,
            current_setpoint_a=3e-12,
            z_position_m=10e-9,
            z_low_limit_m=-200e-9,
            z_high_limit_m=200e-9,
            z_controller_on=True,
            z_controller_status=ZControllerStatus.ON,
            auto_approach_running=False,
        )
        observer = ExternalStateObserver(
            NanonisDevice(backend=backend),
            ActionEventEmitter(events.append),
            poll_interval_s=0.01,
            gate_snapshot_provider=lambda: shared,
        )
        observer.start()
        time.sleep(0.04)
        observer.stop()

        names = [name for name, _arguments in backend.command_log]
        self.assertNotIn("ZCtrl_SetpntGet", names)
        self.assertNotIn("ZCtrl_ZPosGet", names)
        self.assertNotIn("ZCtrl_OnOffGet", names)
        self.assertNotIn("ZCtrl_StatusGet", names)
        self.assertNotIn("AutoApproach_OnOffGet", names)
        self.assertIn("FolMe_XYPosGet", names)

    def test_scan_xy_readback_is_not_reported_as_follow_me_actions(
        self,
    ) -> None:
        events = []
        backend = FakeNanonisBackend(scan_running=False)
        observer = ExternalStateObserver(
            NanonisDevice(backend=backend),
            ActionEventEmitter(events.append),
            poll_interval_s=0.01,
            position_settle_s=0.02,
        )
        observer.start()
        time.sleep(0.03)
        backend.scan_running = True
        for index in range(5):
            backend.follow_me_position = (
                index * 10e-9,
                -index * 10e-9,
            )
            time.sleep(0.015)
        backend.scan_running = False
        time.sleep(0.04)
        observer.stop()

        self.assertEqual(
            sum(
                event.action_name is ActionName.SCAN_STATE_CHANGE
                for event in events
            ),
            2,
        )
        self.assertFalse(
            any(
                event.action_name is ActionName.FOLLOW_ME_MOVE
                for event in events
            )
        )


class IntegratedRunActionAuditTests(unittest.TestCase):
    def test_action_log_starts_with_first_ixyz_segment_and_outlives_it(
        self,
    ) -> None:
        status_backend = FakeNanonisBackend(
            current_a=0.1e-12,
            z_setpoint_a=100e-12,
            z_position_m=-200e-9,
            z_low_limit_m=-200e-9,
            z_high_limit_m=200e-9,
            z_controller_on=False,
            z_status_code=1,
        )
        gate = TipRecordingGateMonitor(
            NanonisDevice(backend=status_backend),
            policy=TipProximityPolicy(
                minimum_effective_current_a=1.5e-12,
                minimum_setpoint_fraction=0.7,
                maximum_setpoint_fraction=1.5,
                minimum_z_limit_clearance_fraction=1e-4,
                minimum_z_limit_clearance_m=1e-12,
                arrival_hold_s=0.0,
                arrival_minimum_samples=1,
                far_noise_current_ceiling_a=0.75e-12,
                far_noise_window_s=0.01,
                far_noise_minimum_samples=1,
                far_noise_hold_s=0.0,
            ),
            poll_interval_s=0.01,
        )
        action_backend = FakeNanonisBackend(scan_running=False)
        action_device = NanonisDevice(backend=action_backend)
        external_backend = FakeNanonisBackend(scan_running=False)
        pause = WorkflowPauseController()
        result_box = {}

        with tempfile.TemporaryDirectory() as tmp:
            recorder = TelemetryRecorder(
                NanonisDevice(backend=telemetry_backend()),
                tmp,
                signal_names=TEST_SIGNALS,
                storage_quota_bytes=10 * 1024**2,
                storage_warning_bytes=9 * 1024**2,
                gate_monitor=gate,
                experiment_metadata=ExperimentMetadata(
                    sample_id="sample-001",
                    description="MgO test surface",
                    operator="lab-user",
                    tags={"material": "MgO"},
                ),
                action_devices=(action_device,),
                workflow_action_emitters=(pause.action_events,),
                external_state_device=NanonisDevice(
                    backend=external_backend
                ),
                external_state_poll_interval_s=0.01,
                action_audit_start_mode=(
                    ActionAuditStartMode.TIP_NEAR_SAMPLE
                ),
            )

            runner = Thread(
                target=lambda: result_box.setdefault(
                    "result",
                    recorder.record(
                        duration_s=0.65,
                        run_id="action-audit-run",
                    ),
                )
            )
            runner.start()
            wait_until(lambda: action_device.action_events.sink is not None)

            action_device.set_bias_v(-0.5)

            status_backend.z_controller_on = True
            status_backend.z_status_code = 2
            status_backend.current_a = -100e-12
            status_backend.z_position_m = 0.0
            wait_until(lambda: gate.high_resolution_enabled)

            action_device.set_bias_v(-1.0)
            pause.pause("inspect scan")
            external_backend.bias_v = -2.0
            time.sleep(0.04)

            status_backend.current_a = 0.3e-12
            status_backend.z_controller_on = False
            status_backend.z_status_code = 1
            status_backend.z_position_m = 200e-9
            wait_until(lambda: not gate.high_resolution_enabled)
            time.sleep(0.06)

            action_device.set_z_setpoint_a(5e-12)
            pause.resume("continue after withdraw")

            time.sleep(0.08)
            action_device.set_bias_v(-3.0)

            runner.join(timeout=2.0)
            self.assertFalse(runner.is_alive())
            result = result_box["result"]
            connection = sqlite3.connect(result.database_path)
            try:
                action_rows = connection.execute(
                    """
                    SELECT action_name, phase, actor, intent,
                           arguments_json, monotonic_ns
                    FROM action_events
                    ORDER BY action_event_id
                    """
                ).fetchall()
                ixyz_stopped_ns = connection.execute(
                    """
                    SELECT monotonic_ns FROM events
                    WHERE event_type='high_resolution_segment_stopped'
                    """
                ).fetchone()[0]
                metadata_json = connection.execute(
                    "SELECT experiment_metadata_json FROM runs"
                ).fetchone()[0]
            finally:
                connection.close()

        decoded = [
            (
                name,
                phase,
                actor,
                intent,
                json.loads(arguments_json),
                monotonic_ns,
            )
            for (
                name,
                phase,
                actor,
                intent,
                arguments_json,
                monotonic_ns,
            ) in action_rows
        ]
        inline_values = [
            row[4]["value"]
            for row in decoded
            if isinstance(row[4]["value"], dict)
        ]
        self.assertFalse(
            any(value.get("target_bias_v") == -0.5 for value in inline_values)
        )
        self.assertTrue(
            any(value.get("target_bias_v") == -1.0 for value in inline_values)
        )
        self.assertTrue(
            any(value.get("target_bias_v") == -3.0 for value in inline_values)
        )
        z_setpoint_before = next(
            row
            for row in decoded
            if row[0] == ActionName.Z_SETPOINT_SET.value
            and row[1] == ActionPhase.BEFORE.value
        )
        self.assertGreater(z_setpoint_before[5], ixyz_stopped_ns)
        self.assertTrue(
            any(
                row[0] == ActionName.BIAS_SET.value
                and row[1] == ActionPhase.OBSERVED.value
                and row[2] == ActionActor.UNKNOWN.value
                and row[3] == ActionIntent.UNKNOWN.value
                and row[4]["value"]["after"]["bias_v"] == -2.0
                for row in decoded
            )
        )
        self.assertFalse(
            any(
                row[0] == ActionName.COARSE_RETRACT_SESSION_END.value
                for row in decoded
            )
        )
        self.assertTrue(result.action_audit_started)
        self.assertGreaterEqual(result.action_events_dropped_before_start, 2)
        self.assertEqual(result.action_events_dropped_after_close, 0)
        self.assertEqual(result.action_audit_closed_reason, "recorder_stopped")
        self.assertEqual(result.segments, 1)
        metadata = json.loads(metadata_json)
        self.assertEqual(metadata["sample_id"], "sample-001")
        self.assertEqual(metadata["tags"], {"material": "MgO"})


if __name__ == "__main__":
    unittest.main()
