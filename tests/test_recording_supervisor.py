from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runtime_layer import load_recording_settings, run_experiment_recording
from runtime_layer.recording_supervisor import (recording_lease, service_directory,
    supervise_recording, request_service_stop, read_health, write_health, _finish_child)


class RecordingSupervisorTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows delete-sharing behavior")
    def test_health_publish_recovers_after_real_windows_reader_closes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "worker.json"
            write_health(path, {"phase": "previous"})
            held_reader = path.open("rb")
            def release_reader(delay):
                self.assertEqual(json.loads(held_reader.read())["phase"], "previous")
                held_reader.close()
            try:
                with patch("runtime_layer.recording_supervisor.time.sleep", side_effect=release_reader) as sleep:
                    with self.assertLogs(level="WARNING") as logs:
                        write_health(path, {"phase": "recording", "samples": 123})
                self.assertEqual(sleep.call_count, 1)
                self.assertIn("health_publish_retry", logs.output[0])
            finally:
                held_reader.close()
            self.assertEqual(read_health(path)["samples"], 123)
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_persistent_health_permission_error_is_bounded_and_preserves_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "worker.json"
            write_health(path, {"phase": "previous"})
            error = PermissionError("persistent Windows sharing denial")
            error.winerror = 5
            with patch.object(Path, "replace", side_effect=error) as replace_file:
                with patch("runtime_layer.recording_supervisor.time.sleep") as sleep:
                    with self.assertLogs(level="WARNING"), self.assertRaises(PermissionError):
                        write_health(path, {"phase": "recording"})
            self.assertEqual(replace_file.call_count, 4)
            self.assertEqual(sum(call.args[0] for call in sleep.call_args_list), .035)
            self.assertEqual(read_health(path)["phase"], "previous")

    def test_other_health_write_errors_are_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "worker.json"
            for error in (PermissionError("real permission failure"), OSError("disk failure")):
                with self.subTest(error=error), patch.object(Path, "replace", side_effect=error) as replace_file:
                    with patch("runtime_layer.recording_supervisor.time.sleep") as sleep:
                        with self.assertRaises(type(error)):
                            write_health(path, {"phase": "recording"})
                    self.assertEqual(replace_file.call_count, 1)
                    sleep.assert_not_called()

    def configuration(self, root, gate="always"):
        config = root / "config.toml"
        config.write_text("[recording]\nenabled=true\n" +
            f"root='{(root / 'runs').as_posix()}'\ngate_mode='{gate}'\n" +
            "observe_external_state=false\nobserve_gui_actions=false\n" +
            "data_logger_enabled=false\n", encoding="utf-8")
        return config, root / "local.toml"

    def test_separate_workers_rotate_and_seal_raw_data_without_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, local = self.configuration(root)
            supervise_recording(segment_seconds=.25, maximum_runs=2,
                                config_path=config, local_path=local)
            databases = list((root / "runs").rglob("run.sqlite"))
            self.assertEqual(len(databases), 2)
            final = read_health(service_directory(root / "runs") / "worker.json")
            self.assertTrue(final["duration_completed"])
            self.assertEqual(final["component_failures"], {})
            for database in databases:
                with closing(sqlite3.connect(database)) as db:
                    self.assertEqual(db.execute("SELECT status FROM runs").fetchone()[0], "complete")
                    self.assertGreater(db.execute("SELECT COUNT(*) FROM signal_samples").fetchone()[0], 0)
                pending = json.loads((database.parent / "recording_review_status.json").read_text())
                self.assertEqual(pending["status"], "deferred")
                self.assertFalse((database.parent / "recording_review").exists())
                wal = database.with_name("run.sqlite-wal")
                self.assertFalse(wal.exists() and wal.stat().st_size)

    def test_idle_gate_has_health_but_no_high_frequency_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(load_recording_settings(), enabled=True, root=Path(tmp),
                gate_mode="near_sample", observe_external_state=False, data_logger_enabled=False)
            progress = []
            result = run_experiment_recording(settings, live=False, duration_s=.15,
                run_id=None, progress_callback=progress.append, generate_review=False)
            self.assertEqual(result.samples, 0)
            self.assertEqual(progress[0]["phase"], "recording")
            self.assertFalse(progress[0]["gate_open"])
            self.assertEqual(progress[-1]["phase"], "finalizing")

    def test_foreground_and_supervisor_locks_exclude_duplicates_and_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("recorder", "supervisor"):
                with recording_lease(tmp, name):
                    with self.assertRaisesRegex(RuntimeError, "Another"):
                        with recording_lease(tmp, name):
                            self.fail("duplicate accepted")
                with recording_lease(tmp, name):
                    pass

    def test_duplicate_supervisor_does_not_overwrite_owner_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, local = self.configuration(root)
            with recording_lease(root / "runs", "supervisor"):
                path = service_directory(root / "runs") / "supervisor.json"
                path.write_text('{"owner":true}', encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    supervise_recording(config_path=config, local_path=local, maximum_runs=1)
                self.assertEqual(path.read_text(), '{"owner":true}')

    def test_stop_marker_prevents_restart_and_preserves_existing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, local = self.configuration(root)
            request_service_stop(root / "runs")
            with patch("runtime_layer.recording_supervisor.subprocess.Popen") as start:
                supervise_recording(config_path=config, local_path=local)
            start.assert_not_called()
            self.assertTrue((service_directory(root / "runs") / "stop").exists())

    def test_worker_uses_module_entrypoint_and_selected_config_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, local = self.configuration(root)
            child = Mock(pid=123)
            child.poll.return_value = 0
            child.wait.return_value = 0
            with patch("runtime_layer.recording_supervisor.subprocess.Popen", return_value=child) as start, \
                 patch("runtime_layer.recording_supervisor.read_health", return_value={}):
                supervise_recording(config_path=config, local_path=local, maximum_runs=1)

        command = start.call_args.args[0]
        executable = Path(sys.executable)
        if executable.name.lower() == "pythonw.exe":
            executable = executable.with_name("python.exe")
        self.assertEqual(command[:4], [str(executable), "-B", "-m", "runtime_layer.recorder_cli"])
        self.assertEqual(command[command.index("--config") + 1], str(config))
        self.assertEqual(command[command.index("--local-config") + 1], str(local))
        self.assertIn("record", command)
        self.assertNotIn("experiment_recorder.py", " ".join(command))

    def test_disk_reserve_stops_cleanly_without_starting_high_frequency_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(load_recording_settings(), enabled=True, root=Path(tmp),
                gate_mode="always", observe_external_state=False, data_logger_enabled=False)
            with patch("workflow_layer.telemetry_recording.recording_root_free_bytes", return_value=10):
                result = run_experiment_recording(settings, live=False, duration_s=.5,
                    run_id=None, generate_review=False)
            self.assertEqual(result.status, "storage_quota_reached")
            self.assertFalse(result.duration_completed)
            self.assertEqual(result.samples, 0)
            with closing(sqlite3.connect(result.database_path)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE event_type='storage_free_space_low'").fetchone()[0], 1)

    def test_watchdog_waits_for_own_worker_before_returning(self):
        with tempfile.TemporaryDirectory() as tmp:
            child = Mock(pid=123)
            child.poll.return_value = None
            child.wait.side_effect = [subprocess.TimeoutExpired("worker", .01), 1]
            stop = Path(tmp) / "stop"
            _finish_child(child, stop, Mock(), grace_s=.01)
            self.assertTrue(stop.exists())
            child.terminate.assert_called_once()
            self.assertEqual(child.wait.call_count, 2)

    def test_cadence_quality_warning_keeps_responsive_worker_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, local = self.configuration(root)
            child = Mock(pid=123)
            child.poll.side_effect = [None, None, None, 0]
            child.wait.return_value = 3  # Quality remains compromised at sealing.
            health = {"pid": 123, "phase": "recording", "utc_ns": time.time_ns(),
                "monotonic_s": time.monotonic(),
                "component_failures": {"telemetry_cadence": "late sample"}}
            with patch("runtime_layer.recording_supervisor.subprocess.Popen", return_value=child), \
                 patch("runtime_layer.recording_supervisor.read_health", return_value=health), \
                 patch("runtime_layer.recording_supervisor.time.sleep"), \
                 patch("runtime_layer.recording_supervisor._finish_child") as finish:
                supervise_recording(config_path=config, local_path=local, maximum_runs=1)
            finish.assert_not_called()
            self.assertEqual(child.poll.call_count, 4)
            self.assertFalse((service_directory(root / "runs") / "worker.stop").exists())

    def test_cadence_warning_does_not_mask_watchdog_failures(self):
        failures = [
            {"component_failures": {"telemetry_cadence": "late sample", "external_state_observer": "failed"}},
            {"writer_failed": True},
            {"monotonic_s": time.monotonic() - 61},
        ]
        for failure in failures:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config, local = self.configuration(root)
                child = Mock(pid=123)
                child.poll.return_value = None
                child.wait.return_value = 3
                health = {"pid": 123, "phase": "recording", "utc_ns": time.time_ns(),
                    "monotonic_s": time.monotonic(),
                    "component_failures": {"telemetry_cadence": "late sample"}} | failure
                with patch("runtime_layer.recording_supervisor.subprocess.Popen", return_value=child), \
                     patch("runtime_layer.recording_supervisor.read_health", return_value=health), \
                     patch("runtime_layer.recording_supervisor._finish_child") as finish:
                    supervise_recording(config_path=config, local_path=local, maximum_runs=1)
                finish.assert_called_once()

    def test_only_completed_cadence_warning_rotates_without_failure_backoff(self):
        cases = [
            ({}, False),
            ({"duration_completed": False}, True),
            ({"duration_completed": None}, True),  # Older health lacks completion evidence.
            ({"pid": 999}, True),
            ({"phase": "failed"}, True),
            ({"status": "failed"}, True),
            ({"review_error": "failed to publish"}, True),
            ({"component_failures": {}}, True),
            ({"component_failures": {"telemetry_cadence": "late",
                                      "durable_writer": "failed"}}, True),
        ]
        for override, needs_backoff in cases:
            with self.subTest(override=override), tempfile.TemporaryDirectory() as tmp:
                config, local = self.configuration(Path(tmp))
                child = Mock(pid=123)
                child.poll.return_value = 3
                child.wait.return_value = 3
                health = {"pid": 123, "phase": "finished", "utc_ns": time.time_ns(),
                    "status": "recording_compromised", "duration_completed": True,
                    "component_failures": {"telemetry_cadence": "late"},
                    "review_error": None} | override
                ticks = [0.0]
                def advance(seconds):
                    ticks[0] += seconds
                with patch("runtime_layer.recording_supervisor.subprocess.Popen", return_value=child) as start, \
                     patch("runtime_layer.recording_supervisor.read_health", return_value=health), \
                     patch("runtime_layer.recording_supervisor.time.monotonic", side_effect=lambda: ticks[0]), \
                     patch("runtime_layer.recording_supervisor.time.sleep", side_effect=advance), \
                     patch("runtime_layer.recording_supervisor.write_health") as publish:
                    supervise_recording(config_path=config, local_path=local, maximum_runs=3)
                self.assertEqual(start.call_count, 3)
                self.assertEqual(ticks[0], 15.0 if needs_backoff else 0.0)
                waits = [c.args[1] for c in publish.call_args_list
                         if c.args[1].get("phase") == "retry_wait"]
                self.assertEqual(bool(waits), needs_backoff)

    def test_capacity_wait_never_creates_workers_or_deletes_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, local = self.configuration(root)
            data = root / "runs" / "preserve.bin"
            data.parent.mkdir()
            data.write_bytes(b"original")
            with patch("runtime_layer.recording_supervisor.recording_root_free_bytes", return_value=0), \
                 patch("runtime_layer.recording_supervisor.subprocess.Popen") as start, \
                 patch("runtime_layer.recording_supervisor.time.sleep", side_effect=lambda _: request_service_stop(data.parent)):
                supervise_recording(config_path=config, local_path=local)
            start.assert_not_called()
            self.assertEqual(data.read_bytes(), b"original")

    def test_unattended_mode_refuses_instrument_writes_and_gui_hooks_before_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, local = self.configuration(root)
            for key in ("data_logger_enabled", "observe_gui_actions"):
                local.write_text(f"[recording]\n{key}=true\n", encoding="utf-8")
                with patch("runtime_layer.recording_supervisor.subprocess.Popen") as start:
                    with self.assertRaises(ValueError):
                        supervise_recording(live=True, config_path=config, local_path=local)
                start.assert_not_called()

    def test_crashed_worker_is_restarted_and_explicit_stop_seals_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, local = self.configuration(root)
            source_root = Path(__file__).resolve().parents[1] / "src"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(source_root) + os.pathsep + env.get("PYTHONPATH", "")
            process = subprocess.Popen([sys.executable, "-B", "-m", "runtime_layer.recorder_cli",
                "--config", str(config), "--local-config", str(local), "supervise",
                "--segment-seconds", "60"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
            directory = service_directory(root / "runs")
            def wait_worker(previous=None):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    health = read_health(directory / "worker.json")
                    if health.get("phase") == "recording" and health.get("pid") != previous:
                        return health
                    if process.poll() is not None:
                        self.fail(process.communicate()[0].decode(errors="replace"))
                    time.sleep(.1)
                self.fail("worker did not become ready")
            try:
                first = wait_worker()
                os.kill(first["pid"], signal.SIGTERM)
                second = wait_worker(first["pid"])
                self.assertNotEqual(first["database_path"], second["database_path"])
                request_service_stop(root / "runs")
                output, _ = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, output.decode(errors="replace"))
                final = read_health(directory / "worker.json")
                self.assertEqual(final["pid"], second["pid"])
                self.assertEqual(final["phase"], "finished")
                self.assertFalse(final["duration_completed"])
                # A sealed, explicitly stopped run still retains any cadence warning.
                self.assertIn(final["status"], {"stopped", "recording_compromised"})
                expected_failures = (
                    {"telemetry_cadence"}
                    if final["status"] == "recording_compromised" else set()
                )
                self.assertEqual(set(final["component_failures"]), expected_failures)
                with closing(sqlite3.connect(second["database_path"])) as db:
                    status, stopped_ns = db.execute(
                        "SELECT status, stopped_utc_ns FROM runs"
                    ).fetchone()
                    self.assertEqual(status, final["status"])
                    self.assertIsNotNone(stopped_ns)
                    stopped = json.loads(db.execute(
                        "SELECT payload_json FROM events WHERE event_type='recorder_stopped'"
                    ).fetchone()[0])
                    self.assertEqual(stopped["status"], status)
                    self.assertEqual(stopped["samples"], db.execute(
                        "SELECT COUNT(*) FROM signal_samples"
                    ).fetchone()[0])
                    failures = [json.loads(row[0])["component"] for row in db.execute(
                        "SELECT payload_json FROM events WHERE event_type='recording_component_failed'"
                    )]
                    self.assertEqual(set(failures), expected_failures)
                    if expected_failures:
                        self.assertGreater(stopped["gap_events"], 0)
                        self.assertGreater(db.execute(
                            "SELECT COUNT(*) FROM events WHERE event_type='telemetry_gap'"
                        ).fetchone()[0], 0)
                self.assertTrue(Path(first["database_path"]).exists())
            finally:
                request_service_stop(root / "runs")
                if process.poll() is None:
                    try:
                        process.communicate(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
