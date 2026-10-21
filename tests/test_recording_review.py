from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analysis_layer.recording_review import summarize_recording
from data_layer import RunStore, inspect_experiment_artifact, TelemetrySample
from data_layer.recording import read_recording_snapshot, read_run_event_page
from instrument_layer import ActionEventEmitter, ActionName, ActionActor, FakeNanonisBackend, NanonisDevice
from runtime_layer.spm_tools import SPMTools
from workflow_layer.recording_review import publish_recording_review
from workflow_layer import TelemetryRecorder


def make_recording(root, *, close=True):
    store = RunStore.create(root / "runs", signal_catalog={}, requested_period_ns=20_000_000)
    ticks = iter(range(1_000_000_000, 100_000_000_000, 100_000_000))
    emitter = ActionEventEmitter(store.append_action_event, monotonic_clock_ns=lambda: next(ticks), utc_clock_ns=time.time_ns)
    def state(name, **after):
        emitter.observe(name, before={}, after=after, changed_fields=tuple(after), source="test_external_observer")
    state(ActionName.AUTO_APPROACH_STATE_CHANGE, auto_approach_running=True)
    for _ in range(3):
        state(ActionName.WITHDRAW, z_controller_status="withdrawing")
    for _ in range(2):
        state(ActionName.WITHDRAW, z_controller_status="safe_tip")
    emitter.record_instantaneous(ActionName.WITHDRAW, arguments={}, result={}, source="explicit_operator",
                                 actor=ActionActor.OPERATOR)
    try:
        emitter.execute(ActionName.Z_POSITION_CHANGE, arguments={}, source="test_failure",
                        operation=lambda: (_ for _ in ()).throw(OSError("simulated failure")))
    except OSError:
        pass
    state(ActionName.AUTO_APPROACH_STATE_CHANGE, auto_approach_running=False)
    for _ in range(2):
        state(ActionName.WITHDRAW, z_controller_status="withdrawing")
    store.append_event("telemetry_gap", {"estimated_missed_deadlines": 2}, monotonic_ns=10, utc_ns=time.time_ns())
    for seq, tick in enumerate((20_000_000, 40_000_000, 100_000_000)):
        store.append_sample(TelemetrySample(seq, 0, tick, time.time_ns(), tick, tick, 1e-12, 0, 0, 0))
    data = root / "partial.dat"
    data.write_text("Experiment\tbias spectroscopy\n[DATA]\nBias calc (V)\tCurrent (A)\tLI Demod 1 X (A)\n0\t1\t2\n1\tnan\tnan\n", encoding="utf-8")
    store.append_artifact(inspect_experiment_artifact(data, observed_monotonic_ns=11, observed_utc_ns=time.time_ns()))
    if close:
        store.finalize("recording_compromised", stopped_utc_ns=time.time_ns())
        store.close()
    return store


class RecordingReviewTests(unittest.TestCase):
    def test_contextual_annotations_preserve_safetip_failures_and_explicit_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_recording(Path(tmp))
            snapshot = read_recording_snapshot(store.database_path)
            review = summarize_recording(snapshot)
            by_id = {a["action_event_id"]: a for a in review["action_annotations"]}
            self.assertEqual(review["observed_process_counts"], {"auto_approach": 1})
            self.assertEqual(review["background_groups"][0]["count"], 3)
            for row in snapshot["actions"]:
                if row["phase"] == "failed" or row["actor"] == "operator":
                    self.assertNotEqual(by_id[row["action_event_id"]]["priority"], "background")
                if (row["arguments"].get("value") or {}).get("after", {}).get("z_controller_status") == "safe_tip":
                    self.assertEqual(by_id[row["action_event_id"]]["priority"], "attention")
            self.assertEqual(review["annotation_counts"]["background"], 3)
            self.assertEqual(review["telemetry"]["estimated_missed_deadlines"], 2)
            self.assertEqual(review["telemetry"]["maximum_interval_ns"], 60_000_000)

    def test_report_links_raw_ids_and_checks_partial_files_without_altering_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = make_recording(root)
            before = hashlib.sha256(store.database_path.read_bytes()).hexdigest()
            review = publish_recording_review(store.database_path, root / "review", read_roots=[root])
            self.assertFalse(review["artifacts"][0]["quality"]["all_channels_finite"])
            self.assertEqual(review["artifacts"][0]["quality"]["channels"]["Current (A)"]["finite_count"], 1)
            annotated = json.loads(Path(review["annotation_path"]).read_text())
            self.assertEqual(len(annotated["annotations"]), review["counts"]["action_events"])
            self.assertEqual(before, hashlib.sha256(store.database_path.read_bytes()).hexdigest())
            with self.assertRaises(FileExistsError):
                publish_recording_review(store.database_path, root / "review", read_roots=[root])

    def test_source_scope_and_source_changes_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = make_recording(root)
            tools = SPMTools(read_roots=[root / "runs"], output_root=root / "out")
            result = tools.inspect_recording(str(store.database_path)).data
            self.assertEqual(result["artifacts"][0]["check"], "outside_read_roots")
            with self.assertRaises(ValueError):
                tools.inspect_recording(str(root / "partial.dat"))
            with (root / "partial.dat").open("a") as stream:
                stream.write("2\t3\t4\n")
            result = publish_recording_review(store.database_path, root / "changed", read_roots=[root])
            self.assertEqual(result["artifacts"][0]["check"], "source_changed")
            self.assertNotIn("quality", result["artifacts"][0])

    def test_live_snapshot_is_not_reported_as_a_stopped_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = make_recording(root, close=False)
            try:
                result = summarize_recording(read_recording_snapshot(store.database_path))
                self.assertFalse(result["finalized"])
                self.assertIn("not_checked", result["process_running"])
                with patch("data_layer.recording.sqlite3.connect", side_effect=sqlite3.OperationalError("unable to open")):
                    with self.assertRaisesRegex(ValueError, "live/unfinalized"):
                        read_recording_snapshot(store.database_path)
            finally:
                store.close()

    def test_idle_between_segments_is_not_a_sampling_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_recording(Path(tmp), close=False)
            for seq, tick in ((3, 10_000_000_000), (4, 10_020_000_000)):
                store.append_sample(TelemetrySample(seq, 1, tick, time.time_ns(), tick, tick, 0, 0, 0, 0))
            store.finalize("complete", stopped_utc_ns=time.time_ns())
            store.close()
            stats = summarize_recording(read_recording_snapshot(store.database_path))["telemetry"]
            self.assertEqual(stats["maximum_interval_ns"], 60_000_000)
            self.assertAlmostEqual(stats["average_hz"], 30)
            self.assertAlmostEqual(stats["duration_s"], 0.1)
            self.assertAlmostEqual(stats["wall_span_s"], 10)

    def test_sealed_readonly_fallback_requires_matching_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = make_recording(root)
            connect = sqlite3.connect
            def readonly_directory(database, **kwargs):
                if "immutable=1" not in database:
                    raise sqlite3.OperationalError("unable to open database file")
                return connect(database, **kwargs)
            with patch("data_layer.recording.sqlite3.connect", side_effect=readonly_directory):
                self.assertTrue(summarize_recording(read_recording_snapshot(store.database_path))["finalized"])
                self.assertEqual(read_run_event_page(store.database_path)["status"], "recording_compromised")
                manifest = store.run_dir / "manifest.json"
                data = json.loads(manifest.read_text())
                data["run_id"] = "different"
                manifest.write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, "does not match"):
                    read_recording_snapshot(store.database_path)

    def test_recorder_automatically_publishes_and_exposes_review_failure(self):
        def record(root):
            names = ["Current (A)", "X (m)", "Y (m)", "Z (m)"]
            backend = FakeNanonisBackend(signal_names=names, signal_values={i: 0.0 for i in range(4)})
            return TelemetryRecorder(NanonisDevice(backend=backend), root, signal_names=tuple(names),
                       storage_quota_bytes=10_000_000, storage_warning_bytes=9_000_000).record(duration_s=0.03)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = record(root)
            self.assertIsNone(result.review_error)
            self.assertTrue(Path(result.review_path).is_file())
            with patch("workflow_layer.recording_review.publish_recording_review", side_effect=OSError("disk full")):
                result = record(root)
                self.assertIsNone(result.review_path)
                self.assertIn("disk full", result.review_error)
                self.assertIn("disk full", json.loads((result.run_dir / "recording_review_status.json").read_text())["error"])

    def test_cached_old_scan_is_not_called_a_new_complete_acquisition(self):
        from test_tip_reference_verification import _write_sxm
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scan = root / "cached.sxm"
            _write_sxm(scan)
            store = RunStore.create(root / "runs", signal_catalog={}, requested_period_ns=20_000_000)
            store.append_artifact(inspect_experiment_artifact(scan, observed_monotonic_ns=1, observed_utc_ns=time.time_ns()))
            store.finalize("complete", stopped_utc_ns=time.time_ns())
            store.close()
            review = publish_recording_review(store.database_path, root / "review", read_roots=[root])
            self.assertTrue(review["artifacts"][0]["quality"]["acquisition_predates_run"])
            self.assertEqual(review["attention"][0]["kind"], "artifact_quality")


if __name__ == "__main__":
    unittest.main()
