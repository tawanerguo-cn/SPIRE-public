from dataclasses import replace
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import tomllib
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from instrument_layer import FakeNanonisBackend, NanonisDevice
from runtime_layer import run_experiment_recording
from runtime_layer.recording_control import load_recording_settings, set_recording_switch
from workflow_layer import TelemetryRecorder
from data_layer import DurableRunWriter


class RecordingPortabilityTests(unittest.TestCase):
    def test_switch_preserves_station_overrides_and_other_tables(self):
        original = (
            "# local station\n[unrelated]\nenabled = false\n"
            "[recording]\nenabled = false # start permit only\n"
            "status_port = 6502\nroot = 'D:\\Data\\station-runs'\n"
            "observe_gui_actions = false\ndata_logger_enabled = false\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "recording.local.toml"
            local.write_text(original, encoding="utf-8")
            set_recording_switch(True, local)
            updated = local.read_text(encoding="utf-8")
            self.assertEqual(updated, original.replace(
                "enabled = false # start permit only", "enabled = true # start permit only"
            ))
            set_recording_switch(False, local)
            self.assertEqual(local.read_text(encoding="utf-8"), original)

    def test_switch_adds_missing_permit_without_losing_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "recording.local.toml"
            local.write_text("[recording]\nstatus_port = 6502\n", encoding="utf-8")
            set_recording_switch(True, local)
            self.assertEqual(tomllib.loads(local.read_text(encoding="utf-8")), {
                "recording": {"enabled": True, "status_port": 6502}
            })

    def test_switch_refuses_invalid_config_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "recording.local.toml"
            original = "[recording]\nstatus_port = broken\n"
            local.write_text(original, encoding="utf-8")
            with self.assertRaises(ValueError):
                set_recording_switch(True, local)
            self.assertEqual(local.read_text(encoding="utf-8"), original)

    def test_slow_startup_is_recorded_before_sampling_deadlines_begin(self):
        names = ("Current (A)", "X (m)", "Y (m)", "Z (m)")
        backend = FakeNanonisBackend(signal_names=list(names), signal_values={0:1e-12,1:1e-9,2:2e-9,3:3e-9})
        def slow_writer(*args, **kwargs):
            time.sleep(.08)
            return DurableRunWriter(*args, **kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("workflow_layer.telemetry_recording.DurableRunWriter", side_effect=slow_writer):
                result = TelemetryRecorder(NanonisDevice(backend=backend), tmp, signal_names=names,
                                           storage_quota_bytes=1024**3, storage_warning_bytes=512*1024**2).record(duration_s=.15)
            with closing(sqlite3.connect(result.database_path)) as db:
                ready = db.execute("SELECT monotonic_ns FROM events WHERE event_type='telemetry_segment_ready'").fetchone()[0]
                started = db.execute("SELECT monotonic_ns FROM events WHERE event_type='recorder_started'").fetchone()[0]
                first_due = db.execute("SELECT scheduled_monotonic_ns FROM signal_samples ORDER BY seq LIMIT 1").fetchone()[0]
            self.assertGreaterEqual(ready-started, 80_000_000)
            self.assertEqual(first_due, ready)
            self.assertEqual(result.missed_deadlines, 0)
            self.assertEqual(result.status, "complete")

    def test_live_assembly_uses_reported_session_not_copied_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "current-session"
            session.mkdir()
            settings = replace(load_recording_settings(), enabled=True, root=root / "runs",
                               gate_mode="always", observe_external_state=False)
            settings = replace(settings, instrument_profile=replace(
                settings.instrument_profile, active_session_path=str(root / "missing-old-session")
            ))
            names = list(settings.signals)
            backend = FakeNanonisBackend(session_path=str(session), signal_names=names,
                                        signal_values={0:1e-12,1:1e-9,2:2e-9,3:3e-9,4:2e-12,5:-3e-12})
            device = NanonisDevice(backend=backend)
            with patch("runtime_layer.experiment_recording.NanonisDevice", return_value=device):
                result = run_experiment_recording(settings, live=True, duration_s=.06, run_id="portable")
            self.assertEqual(result.status, "complete")
            self.assertGreater(result.samples, 0)

    def test_recording_events_share_high_resolution_clock_domain(self):
        names = ("Current (A)", "X (m)", "Y (m)", "Z (m)")
        backend = FakeNanonisBackend(signal_names=list(names), signal_values={0:1e-12,1:1e-9,2:2e-9,3:3e-9})
        with tempfile.TemporaryDirectory() as tmp:
            lower = time.perf_counter_ns()
            result = TelemetryRecorder(NanonisDevice(backend=backend), tmp, signal_names=names,
                                       storage_quota_bytes=1024**3, storage_warning_bytes=512*1024**2).record(duration_s=.07)
            upper = time.perf_counter_ns()
            with closing(sqlite3.connect(result.database_path)) as db:
                times = db.execute("SELECT monotonic_ns FROM events UNION ALL SELECT monotonic_ns FROM signal_samples").fetchall()
            self.assertTrue(times)
            self.assertTrue(all(lower <= tick <= upper for (tick,) in times))
            self.assertEqual(result.missed_deadlines, 0)
            self.assertEqual(result.status, "complete")
