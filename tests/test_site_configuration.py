from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
import io
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from instrument_layer.config import load_connection_config
from knowledge_base.instrument_profile import load_instrument_profile
from runtime_layer.experiment_recording import run_experiment_recording
from runtime_layer.recording_control import load_recording_settings
from runtime_layer.recording_supervisor import supervise_recording
from runtime_layer import recorder_cli as experiment_recorder_cli


PROFILE = """[profile]
id = "{profile_id}"
status = "{status}"
reviewed_on = "2026-01-01"
station = "Example Nanonis station"
application = "Nanonis"
active_session_path = "sessions"

[z_controller]
lowest_experiment_setpoint_range_a = [2e-12, 3e-12]

[ccd]
friendly_name = "Example camera"
interface = "UVC"
windows_driver_service = "usbvideo"
capture_backend = "unavailable"
spire_vision_gate_status = "not_implemented"

[recording]
signal_names = ["Current (A)", "X (m)", "Y (m)", "Z (m)"]
start_condition = "z_controller_control_on_setpoint_window_and_z_inside_limits"
minimum_effective_current_a = 1.5e-12
minimum_setpoint_fraction = 0.7
maximum_setpoint_fraction = 1.5
minimum_z_limit_clearance_fraction = 1e-4
minimum_z_limit_clearance_m = 1e-12
arrival_hold_s = 0.01
arrival_minimum_samples = 1
far_noise_current_ceiling_a = 0.75e-12
far_noise_window_s = 0.1
far_noise_minimum_samples = 1
far_noise_hold_s = 0.0
zctrl_withdraw_closes_segment = false

[follow_me]
nanonis_gui_state_reflection = "unknown"
live_status = "not_commissioned"
commissioned_on = "unknown"

[scan_control]
start_live_status = "not_commissioned"
commissioned_on = "unknown"
direction_readback = "explicit"

[safety_policy_reference]
coarse_minus_z_live_permission = "forbidden"
motor_move_enforcement = "typed wrappers only"

[[evidence]]
kind = "synthetic"
observed_on = "2026-01-01"
fact = "Synthetic test profile; not approved for live acquisition."
"""


def write_profile(directory, profile_id="lab_site", status="draft", change=""):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{profile_id}.toml"
    path.write_text(PROFILE.format(profile_id=profile_id, status=status) + change, encoding="utf-8")
    return path


def write_recording(path, *, enabled=False, profile_id="lab_site",
                    profile_dir="profiles", root="data/runs"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[recording]\n"
        f"enabled = {'true' if enabled else 'false'}\n"
        f'instrument_profile = "{profile_id}"\n'
        f'instrument_profile_directory = "{profile_dir}"\n'
        f'root = "{root}"\n'
        "storage_quota_gib = 10\nstorage_warning_gib = 9\n"
        'gate_mode = "always"\ngate_poll_interval_s = 0.1\n'
        "observe_external_state = false\n",
        encoding="utf-8",
    )


class ConnectionConfigTests(unittest.TestCase):
    def test_path_precedence_and_field_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            explicit = root / "配置" / "explicit.toml"
            env_path = root / "environment.toml"
            explicit.parent.mkdir()
            explicit.write_text('[connection]\nhost="192.0.2.4"\nport=6502\nbackend="live"\n', encoding="utf-8")
            env_path.write_text('[connection]\nhost="192.0.2.8"\nport=6509\n', encoding="utf-8")
            env = {"SPIRE_CONNECTION_CONFIG": str(env_path), "NANONIS_PORT": "6503", "NANONIS_BACKEND": "fake"}
            with patch.dict(os.environ, env, clear=True):
                config = load_connection_config(explicit)
            self.assertEqual((config.host, config.port, config.backend), ("192.0.2.4", 6503, "fake"))

    def test_environment_path_and_missing_config_behavior(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nanonis.toml"
            path.write_text('[connection]\nhost="example.invalid"\nport=6502\n', encoding="utf-8")
            with patch.dict(os.environ, {"SPIRE_CONNECTION_CONFIG": str(path)}, clear=True):
                self.assertEqual(load_connection_config().host, "example.invalid")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(FileNotFoundError, "does not exist"):
                    load_connection_config(Path(temporary) / "missing.toml")
            with patch("instrument_layer.config.DEFAULT_CONFIG_PATH", path.parent / "missing.toml"), patch.dict(os.environ, {}, clear=True):
                config = load_connection_config()
            self.assertEqual((config.host, config.port, config.backend), ("127.0.0.1", 6501, "fake"))

    def test_malformed_config_and_fields_fail_with_actionable_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "connection.toml"
            path.write_text("[connection\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "invalid TOML"):
                load_connection_config(path)
            path.write_text("connection = []", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "must be a TOML table"):
                load_connection_config(path)
            path.write_text("other = 1", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "must define a \\[connection\\] table"):
                load_connection_config(path)
            for value, error in (
                ('host = "  "', "host"), ("port = 0", "port"), ("port = 65536", "port"),
                ("port = 6501.5", "port"), ("timeout_s = nan", "timeout_s"),
                ("timeout_s = 0", "timeout_s"), ('backend = "other"', "backend"),
            ):
                path.write_text(f"[connection]\n{value}\n", encoding="utf-8")
                with self.subTest(value=value), patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, error):
                    load_connection_config(path)

    def test_environment_values_are_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "connection.toml"
            path.write_text('[connection]\nhost = "127.0.0.1"\n', encoding="utf-8")
            for key, value, error in (
                ("NANONIS_HOST", " ", "host"), ("NANONIS_PORT", "70000", "port"),
                ("NANONIS_TIMEOUT_S", "nan", "timeout_s"),
            ):
                with self.subTest(key=key), patch.dict(os.environ, {key: value}, clear=True), self.assertRaisesRegex(ValueError, error):
                    load_connection_config(path)


class ExternalSiteConfigurationTests(unittest.TestCase):
    def test_relative_paths_and_profile_session_resolve_from_config_and_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            lab = Path(temporary) / "实验室 配置"
            write_profile(lab / "profiles")
            config = lab / "recording.toml"
            write_recording(config)
            expected_root = (lab / "data/runs").resolve()
            original_cwd = Path.cwd()
            with tempfile.TemporaryDirectory() as another_cwd:
                try:
                    os.chdir(another_cwd)
                    settings = load_recording_settings(config)
                finally:
                    os.chdir(original_cwd)
        self.assertEqual(settings.root, expected_root)
        self.assertEqual(settings.instrument_profile.status, "draft")
        self.assertEqual(settings.instrument_profile.active_session_path, (lab / "profiles/sessions").resolve())

    def test_implicit_sibling_and_explicit_local_override_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            lab = Path(temporary) / "lab"
            write_profile(lab / "base_profiles", "base")
            write_profile(lab / "local" / "profiles", "site_local")
            base = lab / "base" / "recording.toml"
            write_recording(base, profile_id="base", profile_dir="../base_profiles", root="../base_runs")
            local = lab / "base" / "recording.local.toml"
            write_recording(local, profile_id="site_local", profile_dir="../local/profiles", root="../local_runs")
            settings = load_recording_settings(base)
        self.assertEqual(settings.instrument_profile.profile_id, "site_local")
        self.assertEqual(settings.root, (lab / "local_runs").resolve())

    def test_custom_profile_lookup_and_schema_checks_apply_to_draft(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = write_profile(root)
            self.assertEqual(load_instrument_profile("lab_site", profile_directory=root).status, "draft")
            original = profile.read_text(encoding="utf-8")
            for old, new, error in (
                ('status = "draft"', 'status = "unknown"', "status must be"),
                ("maximum_setpoint_fraction = 1.5", "maximum_setpoint_fraction = nan", "maximum_setpoint_fraction"),
                ("minimum_effective_current_a = 1.5e-12", "minimum_effective_current_a = nan", "must be a finite number"),
                ("arrival_minimum_samples = 1", "arrival_minimum_samples = true", "must be a positive integer"),
                ('active_session_path = "sessions"', 'active_session_path = " "', "active_session_path must be a nonempty"),
                ('coarse_minus_z_live_permission = "forbidden"', 'coarse_minus_z_live_permission = "allowed"', "coarse -Z"),
            ):
                profile.write_text(original.replace(old, new), encoding="utf-8")
                with self.subTest(new=new), self.assertRaisesRegex(ValueError, error):
                    load_instrument_profile("lab_site", profile_directory=root)

    def test_site_without_camera_uses_explicit_unconfigured_state(self):
        camera_fields = (
            'interface = "UVC"',
            'windows_driver_service = "usbvideo"',
            'capture_backend = "unavailable"',
            'spire_vision_gate_status = "not_implemented"',
        )
        no_camera_fields = (
            'interface = "not_configured"',
            'windows_driver_service = "not_configured"',
            'capture_backend = "unavailable"',
            'spire_vision_gate_status = "not_implemented"',
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = write_profile(root)
            original = profile.read_text(encoding="utf-8")
            no_camera = original
            for old, new in zip(camera_fields, no_camera_fields):
                no_camera = no_camera.replace(old, new)

            for status in ("draft", "reviewed"):
                with self.subTest(status=status):
                    profile.write_text(
                        no_camera.replace('status = "draft"', f'status = "{status}"'),
                        encoding="utf-8",
                    )
                    loaded = load_instrument_profile("lab_site", profile_directory=root)
                    self.assertEqual(loaded.status, status)
                    self.assertEqual(loaded.ccd_interface, "not_configured")
                    self.assertEqual(loaded.ccd_capture_backend, "unavailable")

            for old, new in (
                ('windows_driver_service = "not_configured"', 'windows_driver_service = "usbvideo"'),
                ('capture_backend = "unavailable"', 'capture_backend = "opencv_directshow_available"'),
                ('spire_vision_gate_status = "not_implemented"', 'spire_vision_gate_status = "commissioned"'),
            ):
                profile.write_text(
                    no_camera.replace(old, new),
                    encoding="utf-8",
                )
                with self.subTest(inconsistent=new), self.assertRaisesRegex(
                    ValueError, "UVC/usbvideo|unsupported CCD"
                ):
                    load_instrument_profile("lab_site", profile_directory=root)

    def test_draft_can_start_without_evidence_but_reviewed_cannot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = write_profile(root)
            original = profile.read_text(encoding="utf-8")
            without_evidence = original[:original.index("[[evidence]]")].rstrip() + "\n"
            empty = "evidence = []\n" + without_evidence
            profile.write_text(empty, encoding="utf-8")
            self.assertEqual(load_instrument_profile("lab_site", profile_directory=root).evidence, ())
            profile.write_text(empty.replace('status = "draft"', 'status = "reviewed"'), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "at least one entry when reviewed"):
                load_instrument_profile("lab_site", profile_directory=root)
            profile.write_text(without_evidence, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "evidence must be a list"):
                load_instrument_profile("lab_site", profile_directory=root)

    def test_missing_custom_profile_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "recording.toml"
            (root / "profiles").mkdir()
            write_recording(config, profile_id="typo")
            with self.assertRaises(FileNotFoundError):
                load_recording_settings(config)

    def test_live_draft_rejected_before_connection_even_if_enabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_profile(root / "profiles")
            config = root / "recording.toml"
            write_recording(config, enabled=True)
            settings = load_recording_settings(config)
            with patch("runtime_layer.experiment_recording.NanonisDevice") as device, patch(
                "runtime_layer.experiment_recording.load_connection_config"
            ) as connection, self.assertRaisesRegex(RuntimeError, "requires a reviewed instrument profile"):
                run_experiment_recording(settings, live=True, duration_s=0.01, run_id="must-not-connect")
            device.assert_not_called()
            connection.assert_not_called()

    def test_draft_profile_still_runs_fake_recording(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_profile(root / "profiles")
            config = root / "recording.toml"
            write_recording(config, enabled=True, root="recordings")
            settings = load_recording_settings(config)
            result = run_experiment_recording(
                settings, live=False, duration_s=0.05, run_id="draft-fake",
                generate_review=False,
            )
        self.assertEqual(settings.instrument_profile.status, "draft")
        self.assertEqual(result.status, "complete")
        self.assertGreater(result.samples, 0)

    def test_recorder_cli_status_enable_disable_use_selected_config_sibling(self):
        with tempfile.TemporaryDirectory() as temporary:
            lab = Path(temporary) / "lab"
            write_profile(lab / "profiles")
            config = lab / "recording.toml"
            write_recording(config, enabled=False, root="runs")
            local = lab / "recording.local.toml"

            status = io.StringIO()
            with redirect_stdout(status):
                self.assertEqual(experiment_recorder_cli.main(["--config", str(config), "status"]), 0)
            self.assertIn(f"root={(lab / 'runs').resolve()}", status.getvalue())

            with redirect_stdout(io.StringIO()):
                self.assertEqual(experiment_recorder_cli.main(["--config", str(config), "enable"]), 0)
            self.assertTrue(local.is_file())
            self.assertTrue(load_recording_settings(config).enabled)

            with redirect_stdout(io.StringIO()):
                self.assertEqual(experiment_recorder_cli.main(["--config", str(config), "disable"]), 0)
            self.assertFalse(load_recording_settings(config).enabled)
            self.assertFalse(load_recording_settings(config, lab / "unused-local.toml").enabled)

    def test_supervisor_passes_custom_config_and_implicit_sibling_to_loader(self):
        with tempfile.TemporaryDirectory() as temporary:
            lab = Path(temporary) / "lab"
            write_profile(lab / "profiles")
            config = lab / "recording.toml"
            write_recording(config, profile_id="lab_site", root="runs")
            seen = {}

            @contextmanager
            def fake_lease(*_args):
                yield

            def fake_owned(settings, settings_loader, **kwargs):
                loaded_again = settings_loader()
                seen.update(
                    profile_id=loaded_again.instrument_profile.profile_id,
                    root=loaded_again.root,
                    kwargs=kwargs,
                )
                return "supervisor-fake"

            with patch("runtime_layer.recording_supervisor.recording_lease", fake_lease), patch(
                "runtime_layer.recording_supervisor._supervise_owned", side_effect=fake_owned
            ):
                result = supervise_recording(config_path=config, maximum_runs=0)

        self.assertEqual(result, "supervisor-fake")
        self.assertEqual(seen["profile_id"], "lab_site")
        self.assertEqual(seen["root"], (lab / "runs").resolve())
        self.assertIsNone(seen["kwargs"]["local_path"])


if __name__ == "__main__":
    unittest.main()
