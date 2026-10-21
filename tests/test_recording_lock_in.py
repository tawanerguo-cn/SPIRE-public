"""Quadrature provenance, grouped sampling, and historical database reads."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_layer import ExperimentMetadata, RunStore, TelemetrySample
from data_layer.recording_signals import iter_recording_signals
from instrument_layer import FakeNanonisBackend, NanonisDevice
from workflow_layer import TelemetryRecorder


SIGNALS = ("Current (A)", "X (m)", "Y (m)", "Z (m)", "LI Demod 1 X (A)", "LI Demod 1 Y (A)")
# Deliberately different from this station's observed 0/28/29/30/86/87.
INDEXES = (11, 3, 76, 20, 101, 64)
VALUES = (1e-10, 2e-9, -3e-9, -20e-9, -2e-12, 5e-12)


def backend_for_test():
    names = [f"Signal {i}" for i in range(128)]
    for index, name in zip(INDEXES, SIGNALS):
        names[index] = name
    return FakeNanonisBackend(signal_names=names, signal_values=dict(zip(INDEXES, VALUES)))


def recorder(backend, root, signals=SIGNALS):
    return TelemetryRecorder(NanonisDevice(backend=backend), root, signal_names=signals,
                             storage_quota_bytes=10*1024**2, storage_warning_bytes=9*1024**2)


class LockInRecordingTests(unittest.TestCase):
    def test_six_channels_share_a_single_grouped_read_and_timestamp(self):
        backend = backend_for_test()
        with tempfile.TemporaryDirectory() as tmp:
            result = recorder(backend, tmp).record(duration_s=.065)
            rows = list(iter_recording_signals(result.database_path, maximum_seq=result.samples-1, include_lock_in=True))
            self.assertGreaterEqual(len(rows), 2)
            self.assertTrue(all(tuple(row[4:]) == VALUES for row in rows))
            default_rows = list(iter_recording_signals(result.database_path, maximum_seq=0))
            self.assertEqual(len(default_rows[0]), 8)
            manifest = json.loads((result.run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["signal_catalog"], dict(zip(SIGNALS, INDEXES)))
            self.assertEqual(manifest["schema_version"], 5)
            with closing(sqlite3.connect(result.database_path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM signal_samples WHERE monotonic_ns != call_finished_monotonic_ns").fetchone()[0], 0)
        calls = [args for name, args in backend.command_log if name == "Signals_ValsGet"]
        self.assertEqual(len(calls), result.samples)
        self.assertTrue(all(args == (list(INDEXES), 0) for args in calls))
        self.assertFalse(any(name.endswith(("Set", "Start", "Withdraw")) for name, _ in backend.command_log))

    def test_missing_quadrature_prevents_start_without_instrument_writes(self):
        backend = backend_for_test()
        backend.signal_names[INDEXES[5]] = "Unavailable"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "required telemetry signals are unavailable"):
                recorder(backend, tmp).record(duration_s=.01)
            self.assertEqual(list(Path(tmp).rglob("run.sqlite")), [])
        self.assertFalse(any(name == "Signals_ValsGet" or name.endswith("Set") for name, _ in backend.command_log))

    def test_invalid_quadrature_is_evidence_not_zero_or_a_control_action(self):
        for invalid in (float("nan"), float("inf")):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as tmp:
                backend = backend_for_test()
                backend.signal_values[INDEXES[5]] = invalid
                result = recorder(backend, tmp).record(duration_s=.065)
                self.assertEqual(result.status, "recording_compromised")
                self.assertEqual(result.samples, 0)
                with closing(sqlite3.connect(result.database_path)) as db:
                    event = db.execute("SELECT payload_json FROM events WHERE event_type='invalid_telemetry_sample'").fetchone()
                self.assertEqual(json.loads(event[0])["signal_names"], list(SIGNALS))
                self.assertFalse(any(name.endswith(("Set", "Start", "Withdraw")) for name, _ in backend.command_log))

    def test_four_channel_run_has_absent_quadratures(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = recorder(backend_for_test(), tmp, SIGNALS[:4]).record(duration_s=.025)
            rows = list(iter_recording_signals(result.database_path, maximum_seq=0, include_lock_in=True))
            self.assertEqual(rows[0][-2:], (None, None))

    def test_old_database_read_does_not_migrate_or_invent_quadratures(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "old.sqlite"
            with closing(sqlite3.connect(database)) as db:
                db.executescript("""CREATE TABLE runs(status TEXT); INSERT INTO runs VALUES('complete');
                    CREATE TABLE signal_samples(seq INTEGER,segment_id INTEGER,monotonic_ns INTEGER,
                        utc_ns INTEGER,current_a REAL,x_m REAL,y_m REAL,z_m REAL);
                    INSERT INTO signal_samples VALUES(0,0,100,200,1e-10,1e-9,2e-9,3e-9);""")
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            rows = list(iter_recording_signals(database, maximum_seq=0, include_lock_in=True))
            self.assertEqual(rows[0][-2:], (None, None))
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)

    def test_old_archive_finalize_retains_its_actual_schema_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore.create(tmp, signal_catalog={}, requested_period_ns=0,
                experiment_metadata=ExperimentMetadata(tags={"record_kind": "agent_experiment_archive"}))
            database = store.database_path
            manifest_path = store.run_dir / "manifest.json"
            store.finalize("complete", stopped_utc_ns=100)
            store.close()
            with closing(sqlite3.connect(database)) as db:
                db.execute("ALTER TABLE signal_samples DROP COLUMN lock_in_x_a")
                db.execute("ALTER TABLE signal_samples DROP COLUMN lock_in_y_a")
                db.execute("UPDATE runs SET schema_version=4")
                db.commit()
            with RunStore.open_artifact_archive(database) as store:
                store.finalize("complete", stopped_utc_ns=200)
            self.assertEqual(json.loads(manifest_path.read_text())["schema_version"], 4)

    def test_unpaired_or_duplicate_channel_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for signals in (SIGNALS[:5], SIGNALS[:4] + (SIGNALS[4], SIGNALS[4])):
                with self.subTest(signals=signals), self.assertRaises(ValueError):
                    recorder(backend_for_test(), tmp, signals)
