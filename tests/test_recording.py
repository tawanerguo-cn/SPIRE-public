from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Event, Thread
import time
import unittest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_layer import (  # noqa: E402
    ArtifactKind,
    ArtifactLinkStatus,
    ExistingRunActionEventSink,
    RunStore,
    SessionArtifactObserver,
    TelemetrySample,
    estimate_recording_storage,
    inspect_experiment_artifact,
    inspect_nanonis_tab_file,
    latest_completed_action_id,
    reconcile_saved_points,
    recording_tree_size_bytes,
)
from analysis_layer import TipProximityPolicy  # noqa: E402
from instrument_layer import (  # noqa: E402
    ActionActor,
    ActionEventEmitter,
    ActionName,
    ActionPhase,
    FakeNanonisBackend,
    NanonisDevice,
)
from runtime_layer.recording_control import (  # noqa: E402
    load_recording_settings,
    set_recording_switch,
)
from runtime_layer import run_experiment_recording  # noqa: E402
from workflow_layer import TelemetryRecorder, TipRecordingGateMonitor  # noqa: E402


TEST_SIGNALS = ("Current (A)", "X (m)", "Y (m)", "Z (m)")
TEST_STORAGE_QUOTA_BYTES = 10 * 1024**2
TEST_STORAGE_WARNING_BYTES = 9 * 1024**2


class RecordingDataLayerTests(unittest.TestCase):
    def test_run_store_writes_durable_wide_samples_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore.create(
                tmp,
                signal_catalog={"Current (A)": 0, "X (m)": 28, "Y (m)": 29, "Z (m)": 30},
                requested_period_ns=20_000_000,
                run_id="test-run",
            )
            store.append_sample(
                TelemetrySample(
                    seq=0,
                    segment_id=0,
                    monotonic_ns=10,
                    utc_ns=20,
                    call_started_monotonic_ns=8,
                    call_finished_monotonic_ns=10,
                    current_a=-1e-12,
                    x_m=1e-9,
                    y_m=2e-9,
                    z_m=3e-9,
                )
            )
            store.append_event("bias_intent", {"target_v": -0.1}, monotonic_ns=11, utc_ns=21)
            store.finalize("complete", stopped_utc_ns=30)
            database = store.database_path
            manifest = store.run_dir / "manifest.json"
            store.close()

            connection = sqlite3.connect(database)
            try:
                sample = connection.execute(
                    "SELECT seq, current_a, x_m, y_m, z_m FROM signal_samples"
                ).fetchone()
                event = connection.execute("SELECT event_type FROM events").fetchone()
                run = connection.execute("SELECT status FROM runs").fetchone()
            finally:
                connection.close()

            self.assertEqual(sample[0], 0)
            self.assertEqual(sample[1:], (-1e-12, 1e-9, 2e-9, 3e-9))
            self.assertEqual(event[0], "bias_intent")
            self.assertEqual(run[0], "complete")
            self.assertTrue(manifest.exists())

    def test_existing_run_sink_appends_without_owning_or_finalizing_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore.create(
                tmp,
                signal_catalog={},
                requested_period_ns=20_000_000,
                run_id="active-run",
            )
            sink = ExistingRunActionEventSink(store.database_path)
            emitter = ActionEventEmitter(sink)

            emitter.record_instantaneous(
                ActionName.SCAN_START,
                arguments={"direction_up": False},
                result={"running": True},
                source="test.external_control",
            )

            connection = sqlite3.connect(store.database_path)
            try:
                rows = connection.execute(
                    "SELECT action_name, phase FROM action_events ORDER BY action_event_id"
                ).fetchall()
                status = connection.execute("SELECT status FROM runs").fetchone()[0]
            finally:
                connection.close()
                store.close()

        self.assertEqual(rows, [("scan_start", "before"), ("scan_start", "after")])
        self.assertEqual(status, "recording")

    def test_saved_artifact_can_resolve_the_preceding_typed_scan_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore.create(
                tmp,
                signal_catalog={},
                requested_period_ns=20_000_000,
                run_id="action-link-run",
            )
            emitter = ActionEventEmitter(ExistingRunActionEventSink(store.database_path))
            emitter.record_instantaneous(
                ActionName.SCAN_START,
                arguments={"direction_up": False},
                result={"running": True},
                source="test.external_control",
            )

            action_id = latest_completed_action_id(
                store.database_path,
                run_id=store.run_id,
                action_names=(ActionName.SCAN_START.value,),
                before_utc_ns=time.time_ns() + 1_000_000,
            )
            store.close()

        self.assertIsNotNone(action_id)

    def test_artifact_metadata_and_saved_event_are_committed_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scan.sxm"
            source.write_bytes(b":SCANIT_END:\nraw scan bytes")
            artifact = inspect_experiment_artifact(
                source,
                observed_monotonic_ns=12,
                observed_utc_ns=22,
                producer_action_id="scan-action-1",
                metadata={"channel": "Z"},
            )
            source_size = source.stat().st_size
            store = RunStore.create(
                root / "runs",
                signal_catalog={},
                requested_period_ns=20_000_000,
                run_id="artifact-run",
            )
            store.append_artifact(artifact)
            database = store.database_path
            store.finalize("complete", stopped_utc_ns=30)
            store.close()

            connection = sqlite3.connect(database)
            try:
                stored = connection.execute(
                    """
                    SELECT artifact_kind, link_status, producer_action_id,
                           size_bytes, sha256
                    FROM artifacts
                    """
                ).fetchone()
                event = connection.execute(
                    """
                    SELECT event_type, payload_json
                    FROM events WHERE event_type='artifact_saved'
                    """
                ).fetchone()
            finally:
                connection.close()

        self.assertIs(artifact.kind, ArtifactKind.SCAN_RAW)
        self.assertIs(artifact.link_status, ArtifactLinkStatus.LINKED)
        self.assertEqual(stored[:3], ("scan_raw", "linked", "scan-action-1"))
        self.assertEqual(stored[3], source_size)
        self.assertEqual(stored[4], artifact.sha256)
        self.assertEqual(event[0], "artifact_saved")
        self.assertEqual(json.loads(event[1])["artifact_id"], artifact.artifact_id)

    def test_session_artifact_observer_ignores_baseline_and_waits_for_stability(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "old.sxm").write_bytes(b"existing")
            observed = []
            observer = SessionArtifactObserver(
                session,
                observed.append,
                poll_interval_s=0.01,
                stable_polls=2,
            )
            observer.start()
            (session / "spectrum.dat").write_text(
                "Experiment\tBias Spectroscopy\n[DATA]\nBias\tCurrent\n",
                encoding="utf-8",
            )
            (session / "final.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            deadline = time.monotonic() + 1.0
            while len(observed) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            observer.stop()

        self.assertEqual(len(observed), 2)
        self.assertEqual(
            {artifact.kind for artifact in observed},
            {ArtifactKind.SPECTRUM_RAW, ArtifactKind.RENDERED_IMAGE},
        )
        self.assertTrue(
            all(
                artifact.link_status is ArtifactLinkStatus.UNRESOLVED
                for artifact in observed
            )
        )

    def test_nanonis_file_inspection_and_saved_points_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logger.dat"
            path.write_text(
                "Experiment\tData Logger\n"
                "[DATA]\n"
                "Time (s)\tCurrent (A)\tX (m)\tY (m)\tZ (m)\n"
                "0.00\t1\t2\t3\t4\n"
                "0.02\t5\t6\t7\t8\n",
                encoding="utf-8",
            )

            summary = inspect_nanonis_tab_file(path)
            complete = reconcile_saved_points(2, summary)
            mismatch = reconcile_saved_points(3, summary)

        self.assertEqual(summary.data_rows, 2)
        self.assertEqual(summary.malformed_rows, 0)
        self.assertEqual(summary.columns[1:], ("Current (A)", "X (m)", "Y (m)", "Z (m)"))
        self.assertTrue(complete.complete)
        self.assertFalse(mismatch.complete)
        self.assertEqual(mismatch.reason, "saved_points_mismatch")

    def test_storage_estimate_is_conservative_for_dual_recording(self) -> None:
        estimate = estimate_recording_storage(hours=24.0, samples_per_s=50.0)

        self.assertEqual(estimate.sample_count, 4_320_000)
        self.assertEqual(estimate.numeric_payload_bytes, 172_800_000)
        self.assertEqual(estimate.combined_bytes, 2_211_840_000)

    def test_recording_tree_size_counts_only_the_recording_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run").mkdir()
            (root / "run" / "a.bin").write_bytes(b"a" * 123)
            (root / "b.bin").write_bytes(b"b" * 45)

            self.assertEqual(recording_tree_size_bytes(root), 168)


class RecordingWorkflowTests(unittest.TestCase):
    def test_fake_telemetry_recording_uses_acquisition_period_and_all_four_signals(self) -> None:
        names = [f"Signal {index}" for index in range(31)]
        names[0], names[28], names[29], names[30] = "Current (A)", "X (m)", "Y (m)", "Z (m)"
        backend = FakeNanonisBackend(
            signal_names=names,
            signal_values={0: -1e-12, 28: 1e-9, 29: 2e-9, 30: 3e-9},
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = TelemetryRecorder(
                NanonisDevice(backend=backend),
                tmp,
                signal_names=TEST_SIGNALS,
                storage_quota_bytes=TEST_STORAGE_QUOTA_BYTES,
                storage_warning_bytes=TEST_STORAGE_WARNING_BYTES,
            ).record(duration_s=0.07, run_id="telemetry-test")
            connection = sqlite3.connect(result.database_path)
            try:
                row = connection.execute(
                    "SELECT current_a, x_m, y_m, z_m FROM signal_samples ORDER BY seq LIMIT 1"
                ).fetchone()
                period = connection.execute("SELECT requested_period_ns FROM runs").fetchone()[0]
            finally:
                connection.close()

        self.assertGreaterEqual(result.samples, 3)
        self.assertEqual(row, (-1e-12, 1e-9, 2e-9, 3e-9))
        self.assertEqual(period, 20_000_000)
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.segments, 1)

    def test_expired_deadlines_are_counted_instead_of_catch_up_reads(
        self,
    ) -> None:
        class SlowSignalBackend(FakeNanonisBackend):
            def Signals_ValsGet(
                self,
                Signals_indexes,
                Wait_for_newest_data,
            ):
                time.sleep(0.03)
                return super().Signals_ValsGet(
                    Signals_indexes,
                    Wait_for_newest_data,
                )

        names = [f"Signal {index}" for index in range(31)]
        names[0], names[28], names[29], names[30] = TEST_SIGNALS
        backend = SlowSignalBackend(
            signal_names=names,
            signal_values={0: -1e-12, 28: 1e-9, 29: 2e-9, 30: 3e-9},
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = TelemetryRecorder(
                NanonisDevice(backend=backend),
                tmp,
                signal_names=TEST_SIGNALS,
                storage_quota_bytes=TEST_STORAGE_QUOTA_BYTES,
                storage_warning_bytes=TEST_STORAGE_WARNING_BYTES,
            ).record(duration_s=0.16, run_id="deadline-test")
            connection = sqlite3.connect(result.database_path)
            try:
                scheduled = [
                    row[0]
                    for row in connection.execute(
                        "SELECT scheduled_monotonic_ns FROM "
                        "signal_samples ORDER BY seq"
                    )
                ]
            finally:
                connection.close()

        self.assertGreater(result.missed_deadlines, 0)
        self.assertGreater(result.gap_events, 0)
        self.assertEqual(result.status, "recording_compromised")
        self.assertTrue(result.duration_completed)
        self.assertEqual(set(result.component_failures), {"telemetry_cadence"})
        self.assertTrue(
            all(
                right - left >= 40_000_000
                for left, right in zip(scheduled, scheduled[1:])
            )
        )

    def test_explicit_stop_seals_samples_and_preserves_cadence_warning(self) -> None:
        ticks = [0]

        def advance(seconds):
            ticks[0] += round(seconds * 1_000_000_000)

        class DelayedSignalBackend(FakeNanonisBackend):
            reads = 0

            def Signals_ValsGet(self, Signals_indexes, Wait_for_newest_data):
                advance(0.03)  # Deterministically miss the unchanged 20 ms deadline.
                self.reads += 1
                return super().Signals_ValsGet(Signals_indexes, Wait_for_newest_data)

        backend = DelayedSignalBackend(
            signal_names=list(TEST_SIGNALS),
            signal_values={0: -1e-12, 1: 1e-9, 2: 2e-9, 3: 3e-9},
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = TelemetryRecorder(
                NanonisDevice(backend=backend), tmp, signal_names=TEST_SIGNALS,
                storage_quota_bytes=TEST_STORAGE_QUOTA_BYTES,
                storage_warning_bytes=TEST_STORAGE_WARNING_BYTES,
                clock_ns=lambda: ticks[0], sleep=advance,
            ).record(duration_s=10, stop_requested=lambda: backend.reads >= 2,
                     generate_review=False)
            connection = sqlite3.connect(result.database_path)
            try:
                status, stopped_ns = connection.execute(
                    "SELECT status, stopped_utc_ns FROM runs"
                ).fetchone()
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM signal_samples"
                ).fetchone()[0], 2)
                failure = json.loads(connection.execute(
                    "SELECT payload_json FROM events WHERE event_type='recording_component_failed'"
                ).fetchone()[0])
            finally:
                connection.close()
        self.assertEqual(result.status, "recording_compromised")
        self.assertEqual(status, result.status)
        self.assertIsNotNone(stopped_ns)
        self.assertFalse(result.duration_completed)
        self.assertEqual(set(result.component_failures), {"telemetry_cadence"})
        self.assertEqual(failure["component"], "telemetry_cadence")
        self.assertEqual(result.samples, 2)
        self.assertEqual(result.gap_events, 2)

    def test_external_observer_failure_degrades_but_does_not_stop_ixyz(
        self,
    ) -> None:
        class FailingStatusBackend(FakeNanonisBackend):
            def Bias_Get(self):
                raise OSError("truncated status response")

        names = [f"Signal {index}" for index in range(31)]
        names[0], names[28], names[29], names[30] = TEST_SIGNALS
        telemetry = FakeNanonisBackend(
            signal_names=names,
            signal_values={0: -1e-12, 28: 1e-9, 29: 2e-9, 30: 3e-9},
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = TelemetryRecorder(
                NanonisDevice(backend=telemetry),
                tmp,
                signal_names=TEST_SIGNALS,
                storage_quota_bytes=TEST_STORAGE_QUOTA_BYTES,
                storage_warning_bytes=TEST_STORAGE_WARNING_BYTES,
                external_state_device=NanonisDevice(
                    backend=FailingStatusBackend()
                ),
                external_state_poll_interval_s=0.01,
            ).record(duration_s=0.12, run_id="observer-failure")
            connection = sqlite3.connect(result.database_path)
            try:
                stored_status = connection.execute(
                    "SELECT status FROM runs"
                ).fetchone()[0]
                failure_payload = json.loads(
                    connection.execute(
                        "SELECT payload_json FROM events WHERE "
                        "event_type='recording_component_failed'"
                    ).fetchone()[0]
                )
            finally:
                connection.close()

        self.assertGreaterEqual(result.samples, 4)
        self.assertEqual(result.status, "recording_compromised")
        self.assertEqual(stored_status, "recording_compromised")
        self.assertEqual(
            failure_payload["component"],
            "external_state_observer",
        )
        self.assertTrue(failure_payload["telemetry_continued"])

    def test_telemetry_exception_still_seals_the_run(self) -> None:
        class FailingTelemetryBackend(FakeNanonisBackend):
            reads = 0

            def Signals_ValsGet(
                self,
                Signals_indexes,
                Wait_for_newest_data,
            ):
                self.reads += 1
                if self.reads >= 3:
                    raise OSError("telemetry socket closed")
                return super().Signals_ValsGet(
                    Signals_indexes,
                    Wait_for_newest_data,
                )

        names = [f"Signal {index}" for index in range(31)]
        names[0], names[28], names[29], names[30] = TEST_SIGNALS
        backend = FailingTelemetryBackend(
            signal_names=names,
            signal_values={0: -1e-12, 28: 1e-9, 29: 2e-9, 30: 3e-9},
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(OSError, "socket closed"):
                TelemetryRecorder(
                    NanonisDevice(backend=backend),
                    tmp,
                    signal_names=TEST_SIGNALS,
                    storage_quota_bytes=TEST_STORAGE_QUOTA_BYTES,
                    storage_warning_bytes=TEST_STORAGE_WARNING_BYTES,
                ).record(duration_s=0.2, run_id="sealed-failure")
            database = next(Path(tmp).rglob("run.sqlite"))
            connection = sqlite3.connect(database)
            try:
                stored_status = connection.execute(
                    "SELECT status FROM runs"
                ).fetchone()[0]
                failure_type = connection.execute(
                    "SELECT event_type FROM events WHERE "
                    "event_type='recorder_failed'"
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(stored_status, "failed")
        self.assertEqual(failure_type, "recorder_failed")

    def test_data_logger_segment_is_ingested_and_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "session"
            session.mkdir()

            class FileWritingDataLoggerBackend(FakeNanonisBackend):
                def DataLog_Stop(self):
                    path = session / f"{self.data_log_basename}.dat"
                    self.data_log_saved_file_path = str(path)
                    self.data_log_saved_points = 2
                    response = super().DataLog_Stop()
                    path.write_text(
                        "Experiment\tData Logger\n"
                        "[DATA]\n"
                        "Time (s)\tCurrent (A)\tX (m)\tY (m)\tZ (m)\n"
                        "0.00\t1\t2\t3\t4\n"
                        "0.02\t5\t6\t7\t8\n",
                        encoding="utf-8",
                    )
                    return response

            names = [f"Signal {index}" for index in range(31)]
            names[0], names[28], names[29], names[30] = TEST_SIGNALS
            backend = FileWritingDataLoggerBackend(
                signal_names=names,
                signal_values={
                    0: -1e-12,
                    28: 1e-9,
                    29: 2e-9,
                    30: 3e-9,
                },
                session_path=str(session),
            )
            device = NanonisDevice(backend=backend)
            result = TelemetryRecorder(
                device,
                root / "runs",
                signal_names=TEST_SIGNALS,
                storage_quota_bytes=TEST_STORAGE_QUOTA_BYTES,
                storage_warning_bytes=TEST_STORAGE_WARNING_BYTES,
                data_logger_device=device,
                artifact_session_path=session,
                artifact_poll_interval_s=0.01,
                artifact_stable_polls=2,
            ).record(duration_s=0.08, run_id="data-logger-run")
            connection = sqlite3.connect(result.database_path)
            try:
                reconciliation = json.loads(
                    connection.execute(
                        "SELECT payload_json FROM events WHERE "
                        "event_type='data_logger_reconciliation'"
                    ).fetchone()[0]
                )
                artifact_kind = connection.execute(
                    "SELECT artifact_kind FROM artifacts"
                ).fetchone()[0]
                component_failures = connection.execute(
                    "SELECT payload_json FROM events WHERE "
                    "event_type='recording_component_failed'"
                ).fetchall()
            finally:
                connection.close()

        self.assertEqual(result.status, "complete", component_failures)
        self.assertEqual(result.artifacts, 1)
        self.assertEqual(artifact_kind, "data_logger_raw")
        self.assertTrue(reconciliation["complete"])
        self.assertEqual(reconciliation["saved_points"], 2)
        self.assertEqual(reconciliation["ingested_rows"], 2)

    def test_recorder_links_scan_and_spectrum_files_to_observed_actions(
        self,
    ) -> None:
        names = [f"Signal {index}" for index in range(31)]
        names[0], names[28], names[29], names[30] = TEST_SIGNALS
        backend = FakeNanonisBackend(
            signal_names=names,
            signal_values={0: -1e-12, 28: 1e-9, 29: 2e-9, 30: 3e-9},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "session"
            recordings = root / "recordings"
            session.mkdir()
            result_box = {}
            observer_ready = Event()

            def record() -> None:
                result_box["result"] = TelemetryRecorder(
                    NanonisDevice(backend=backend),
                    recordings,
                    signal_names=TEST_SIGNALS,
                    storage_quota_bytes=TEST_STORAGE_QUOTA_BYTES,
                    storage_warning_bytes=TEST_STORAGE_WARNING_BYTES,
                    artifact_session_path=session,
                    artifact_poll_interval_s=0.01,
                    artifact_stable_polls=2,
                ).record(duration_s=0.6, run_id="artifact-observer-run",
                    progress_callback=lambda state: observer_ready.set() if state.get('phase') == 'recording' else None)

            thread = Thread(target=record)
            thread.start()
            self.assertTrue(observer_ready.wait(2.0), 'artifact observer must establish its baseline before new files appear')
            (session / "scan.sxm").write_bytes(b":SCANIT_END:\nscan")
            (session / "spectrum.dat").write_text(
                "Experiment\tBias Spectroscopy\n"
                "[DATA]\n"
                "Bias (V)\tCurrent (A)\n"
                "0.0\t1e-12\n",
                encoding="utf-8",
            )
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())
            result = result_box["result"]
            connection = sqlite3.connect(result.database_path)
            try:
                artifacts = connection.execute(
                    """
                    SELECT artifact_kind, link_status, producer_action_id
                    FROM artifacts ORDER BY artifact_kind
                    """
                ).fetchall()
                artifact_event_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM events
                    WHERE event_type='artifact_saved'
                    """
                ).fetchone()[0]
                action_rows = connection.execute(
                    """
                    SELECT action_name, phase, actor, action_id
                    FROM action_events
                    WHERE source='SessionArtifactObserver'
                    ORDER BY action_name
                    """
                ).fetchall()
            finally:
                connection.close()

        self.assertEqual(result.artifacts, 2)
        self.assertEqual(
            {(row[0], row[1]) for row in artifacts},
            {
                ("scan_raw", "linked"),
                ("spectrum_raw", "linked"),
            },
        )
        self.assertEqual(artifact_event_count, 2)
        self.assertEqual(
            {(row[0], row[1], row[2]) for row in action_rows},
            {
                (
                    ActionName.SCAN_ACQUIRE.value,
                    ActionPhase.OBSERVED.value,
                    ActionActor.UNKNOWN.value,
                ),
                (
                    ActionName.BIAS_SPECTROSCOPY_ACQUIRE.value,
                    ActionPhase.OBSERVED.value,
                    ActionActor.UNKNOWN.value,
                ),
            },
        )
        self.assertEqual(
            {row[2] for row in artifacts},
            {row[3] for row in action_rows},
        )

    def test_tip_gate_opens_at_arrival_and_closes_after_withdrawn_noise_window(self) -> None:
        names = [f"Signal {index}" for index in range(31)]
        names[0], names[28], names[29], names[30] = "Current (A)", "X (m)", "Y (m)", "Z (m)"
        telemetry_backend = FakeNanonisBackend(
            signal_names=names,
            signal_values={0: -1e-12, 28: 1e-9, 29: 2e-9, 30: 3e-9},
        )
        status_backend = FakeNanonisBackend(
            current_a=-0.06e-12,
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

        stage = 0

        def advance_after_observation() -> bool:
            nonlocal stage
            snapshot = gate.latest_snapshot()
            if snapshot is None:
                return False
            if stage == 0:
                self.assertFalse(gate.high_resolution_enabled)
                status_backend.z_controller_on = True
                status_backend.z_status_code = 2
                stage = 1
            elif stage == 1 and snapshot.z_controller_on:
                self.assertFalse(gate.high_resolution_enabled)
                status_backend.current_a = -100e-12
                stage = 2
            elif stage == 2 and snapshot.current_a == -100e-12:
                self.assertFalse(gate.high_resolution_enabled)
                status_backend.z_position_m = 0.0
                stage = 3
            elif stage == 3 and any(
                name == "Signals_ValsGet" for name, _ in telemetry_backend.command_log
            ):
                # Keep the near-sample state until acquisition actually happens;
                # database startup and thread scheduling must not consume it.
                self.assertTrue(gate.high_resolution_enabled)
                status_backend.current_a = 0.3e-12
                status_backend.z_controller_on = False
                status_backend.z_status_code = 1
                status_backend.z_position_m = 200e-9
                stage = 4
            elif stage == 4 and gate.high_resolution_status()[1] == "withdrawn_sustained_current_noise":
                self.assertFalse(gate.high_resolution_enabled)
                stage = 5
                # Let this recorder iteration persist the closed gate edge.
            elif stage == 5:
                return True
            return False

        with tempfile.TemporaryDirectory() as tmp:
            result = TelemetryRecorder(
                NanonisDevice(backend=telemetry_backend),
                tmp,
                signal_names=TEST_SIGNALS,
                storage_quota_bytes=TEST_STORAGE_QUOTA_BYTES,
                storage_warning_bytes=TEST_STORAGE_WARNING_BYTES,
                gate_monitor=gate,
            ).record(
                duration_s=5.0,  # Watchdog only; observed completion ends the test.
                run_id="gated-test",
                stop_requested=advance_after_observation,
            )
            connection = sqlite3.connect(result.database_path)
            try:
                event_types = [
                    row[0] for row in connection.execute("SELECT event_type FROM events ORDER BY event_id")
                ]
                stopped_payload = connection.execute(
                    "SELECT payload_json FROM events "
                    "WHERE event_type='high_resolution_segment_stopped'"
                ).fetchone()
                segment_ids = {
                    row[0] for row in connection.execute("SELECT DISTINCT segment_id FROM signal_samples")
                }
                sample_bounds = connection.execute(
                    "SELECT MIN(monotonic_ns),MAX(monotonic_ns) FROM signal_samples"
                ).fetchone()
                segment_edges = dict(connection.execute(
                    "SELECT event_type,monotonic_ns FROM events WHERE event_type IN "
                    "('high_resolution_segment_started','high_resolution_segment_stopped')"
                ))
            finally:
                connection.close()

        self.assertEqual(stage, 5, "all observed gate transitions must complete before the watchdog")
        self.assertGreater(result.samples, 0)
        self.assertEqual(result.segments, 1)
        self.assertEqual(segment_ids, {0})
        self.assertIn("high_resolution_segment_started", event_types)
        self.assertIn("high_resolution_segment_stopped", event_types)
        self.assertIn("withdrawn_sustained_current_noise", stopped_payload[0])
        self.assertGreaterEqual(sample_bounds[0], segment_edges["high_resolution_segment_started"])
        self.assertLessEqual(sample_bounds[1], segment_edges["high_resolution_segment_stopped"])

    def test_local_switch_overrides_checked_in_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "recording.toml"
            local = root / "recording.local.toml"
            config.write_text(
                "[recording]\n"
                "enabled=false\n"
                "instrument_profile='spire_local_station'\n"
                f"root='{root.as_posix()}'\n"
                "storage_quota_gib=100\n"
                "storage_warning_gib=95\n"
                "gate_mode='near_sample'\n"
                "gate_poll_interval_s=0.25\n",
                encoding="utf-8",
            )

            self.assertFalse(load_recording_settings(config, local).enabled)
            self.assertIsNone(
                load_recording_settings(config, local).status_port
            )
            self.assertFalse(
                load_recording_settings(config, local).data_logger_enabled
            )
            self.assertFalse(
                load_recording_settings(config, local).observe_gui_actions
            )
            set_recording_switch(True, local)
            self.assertTrue(load_recording_settings(config, local).enabled)
            self.assertFalse(
                load_recording_settings(config, local).observe_gui_actions
            )
            set_recording_switch(False, local)
            self.assertFalse(load_recording_settings(config, local).enabled)
            local.write_text(
                "[recording]\nobserve_gui_actions=true\n", encoding="utf-8"
            )
            self.assertTrue(
                load_recording_settings(config, local).observe_gui_actions
            )

    def test_live_data_logger_requires_a_distinct_status_port(self) -> None:
        settings = replace(
            load_recording_settings(),
            enabled=True,
            data_logger_enabled=True,
            status_port=None,
        )
        with self.assertRaisesRegex(
            ValueError,
            "requires a commissioned status_port",
        ):
            run_experiment_recording(
                settings,
                live=True,
                duration_s=0.01,
                run_id="must-not-connect",
            )

    def test_runtime_assembles_fake_recording_from_reviewed_profile(self) -> None:
        settings = load_recording_settings()
        with tempfile.TemporaryDirectory() as tmp:
            enabled = replace(settings, enabled=True, root=Path(tmp))
            result = run_experiment_recording(
                enabled,
                live=False,
                duration_s=1.3,
                run_id="runtime-assembly",
            )

        self.assertEqual(result.status, "complete")
        self.assertGreater(result.samples, 0)

    def test_quota_warning_is_persisted_and_reported_without_dropping_samples(self) -> None:
        names = [f"Signal {index}" for index in range(31)]
        names[0], names[28], names[29], names[30] = "Current (A)", "X (m)", "Y (m)", "Z (m)"
        backend = FakeNanonisBackend(
            signal_names=names,
            signal_values={0: -1e-12, 28: 1e-9, 29: 2e-9, 30: 3e-9},
        )
        warnings: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "existing.bin").write_bytes(b"x" * 1024)
            result = TelemetryRecorder(
                NanonisDevice(backend=backend),
                tmp,
                signal_names=TEST_SIGNALS,
                storage_warning_bytes=1,
                storage_quota_bytes=10 * 1024**2,
                quota_check_interval_s=0.01,
                quota_warning_callback=warnings.append,
            ).record(duration_s=0.05, run_id="quota-warning")
            connection = sqlite3.connect(result.database_path)
            try:
                event_types = {
                    row[0] for row in connection.execute("SELECT event_type FROM events")
                }
            finally:
                connection.close()

        self.assertEqual(result.status, "complete")
        self.assertGreater(result.samples, 0)
        self.assertEqual(len(warnings), 1)
        self.assertIn("storage_quota_warning", event_types)

    def test_recorder_refuses_to_start_at_quota(self) -> None:
        names = [f"Signal {index}" for index in range(31)]
        names[0], names[28], names[29], names[30] = "Current (A)", "X (m)", "Y (m)", "Z (m)"
        backend = FakeNanonisBackend(signal_names=names)
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "existing.bin").write_bytes(b"x" * 1024)
            recorder = TelemetryRecorder(
                NanonisDevice(backend=backend),
                tmp,
                signal_names=TEST_SIGNALS,
                storage_warning_bytes=512,
                storage_quota_bytes=1024,
            )
            with self.assertRaises(OSError):
                recorder.record(duration_s=0.01, run_id="quota-full")

    def test_recorder_flushes_and_stops_when_quota_is_reached_after_start(self) -> None:
        names = [f"Signal {index}" for index in range(31)]
        names[0], names[28], names[29], names[30] = "Current (A)", "X (m)", "Y (m)", "Z (m)"
        backend = FakeNanonisBackend(signal_names=names)
        with tempfile.TemporaryDirectory() as tmp:
            result = TelemetryRecorder(
                NanonisDevice(backend=backend),
                tmp,
                signal_names=TEST_SIGNALS,
                storage_warning_bytes=1,
                storage_quota_bytes=2,
                quota_check_interval_s=0.01,
            ).record(duration_s=0.1, run_id="quota-reached")
            connection = sqlite3.connect(result.database_path)
            try:
                event_types = {
                    row[0] for row in connection.execute("SELECT event_type FROM events")
                }
                stored_status = connection.execute("SELECT status FROM runs").fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(result.status, "storage_quota_reached")
        self.assertEqual(stored_status, "storage_quota_reached")
        self.assertIn("storage_quota_reached", event_types)


if __name__ == "__main__":
    unittest.main()
