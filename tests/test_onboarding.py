from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtime_layer.onboarding import main
from runtime_layer.recording_control import load_recording_settings


class OnboardingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_root = Path(
            r"D:\project\esr_stm\.tmp\.tmp_spire_opensource\onboarding_audit"
        )
        cls.temp_root.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="case-", dir=self.temp_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def call_cli(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(arguments))
        return code, out.getvalue(), err.getvalue()

    def make_site(self):
        site = self.root / "新建目录" / "实验站 A"
        code, _, err = self.call_cli("init", "--directory", str(site), "--profile-id", "lab_one")
        self.assertEqual((code, err), (0, ""))
        return site

    def test_init_generates_private_draft_with_resolved_custom_paths(self):
        site = self.make_site()
        self.assertEqual(
            (site / ".gitignore").read_text(encoding="utf-8"),
            "# Private Nanonis site configuration\n*\n",
        )
        connection = tomllib.loads((site / "connection.toml").read_text(encoding="utf-8"))
        self.assertEqual(connection["connection"]["backend"], "fake")
        profile_doc = tomllib.loads((site / "profiles" / "lab_one.toml").read_text(encoding="utf-8"))
        self.assertEqual(profile_doc["profile"]["status"], "draft")
        self.assertEqual(profile_doc["evidence"], [])
        self.assertEqual(profile_doc["profile"]["active_session_path"], "../data/session")
        self.assertEqual(profile_doc["follow_me"]["nanonis_gui_state_reflection"], "not_commissioned")
        self.assertEqual(profile_doc["follow_me"]["commissioned_on"], "")
        self.assertEqual(profile_doc["scan_control"]["commissioned_on"], "")
        self.assertEqual(profile_doc["ccd"]["interface"], "not_configured")
        self.assertNotIn("motor_control", profile_doc)
        self.assertNotIn("feedback_poke", profile_doc)
        self.assertIn("Example only", (site / "profiles" / "lab_one.toml").read_text(encoding="utf-8"))
        settings = load_recording_settings(site / "recording.toml")
        self.assertEqual(settings.instrument_profile.status, "draft")
        self.assertEqual(settings.instrument_profile.profile_id, "lab_one")
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.observe_gui_actions)
        self.assertFalse(settings.data_logger_enabled)
        self.assertEqual(settings.root, (site / "data" / "recordings").resolve())

    def test_init_refuses_existing_target_without_changing_it(self):
        site = self.root / "existing"
        site.mkdir()
        marker = site / "marker"
        marker.write_text("keep", encoding="utf-8")
        code, _, err = self.call_cli("init", "--directory", str(site))
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_doctor_detects_invalid_site_without_printing_paths(self):
        site = self.make_site()
        (site / "connection.toml").write_text('[connection]\nhost = ""\nport = 70000\n', encoding="utf-8")
        with patch.dict("os.environ", {"NANONIS_BACKEND": "live"}), \
                patch.object(socket, "socket", side_effect=AssertionError("socket used")), \
                patch.object(socket, "create_connection", side_effect=AssertionError("network used")):
            code, output, _ = self.call_cli("doctor", "--site", str(site), "--json")
        report = json.loads(output)
        self.assertEqual(code, 1)
        self.assertFalse(report["offline_ready"])
        self.assertFalse(report["hardware_contacted"])
        self.assertEqual(report["network_requests"], 0)
        self.assertNotIn(str(site), output)

    def test_doctor_draft_is_offline_ready_and_never_contacts_hardware(self):
        site = self.make_site()
        with patch.dict("os.environ", {"NANONIS_BACKEND": "live"}), \
                patch.object(socket, "socket", side_effect=AssertionError("socket used")), \
                patch.object(socket, "create_connection", side_effect=AssertionError("network used")):
            code, output, _ = self.call_cli("doctor", "--site", str(site), "--json")
        report = json.loads(output)
        self.assertEqual(code, 0)
        self.assertTrue(report["offline_ready"])
        self.assertIn("commissioning", report["live_readiness"])
        self.assertFalse(report["hardware_contacted"])
        self.assertIn("draft", {item["status"] for item in report["checks"]})

    def test_doctor_without_site_marks_bundled_profile_reference_only(self):
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(socket, "socket", side_effect=AssertionError("socket used")), \
                patch.object(socket, "create_connection", side_effect=AssertionError("network used")):
            code, output, _ = self.call_cli("doctor", "--json")
        report = json.loads(output)
        profiles = [item for item in report["checks"] if item["check"] == "site_profile"]
        self.assertEqual(code, 0)
        self.assertTrue(report["offline_ready"])
        self.assertEqual(profiles[0]["status"], "reference_only")
        self.assertIn("reference", profiles[0]["detail"])
        self.assertFalse(report["hardware_contacted"])

    def test_demo_persists_synthetic_audit_and_refuses_overwrite(self):
        output = self.root / "demo"
        with patch.dict("os.environ", {"NANONIS_BACKEND": "live"}), \
                patch.object(socket, "socket", side_effect=AssertionError("socket used")), \
                patch.object(socket, "create_connection", side_effect=AssertionError("network used")):
            code, stdout, _ = self.call_cli("demo", "--output", str(output))
        self.assertEqual(code, 0, stdout)
        summary = json.loads((output / "demo-summary.json").read_text(encoding="utf-8"))
        self.assertTrue(summary["synthetic"])
        self.assertEqual(summary["mode"], "fake_only")
        self.assertEqual(summary["tip_quality"], "not_assessed")
        self.assertGreater(len(summary["persisted_events"]["events"]), 0)
        previous = (output / "demo-summary.json").read_bytes()
        code, _, _ = self.call_cli("demo", "--output", str(output))
        self.assertEqual(code, 2)
        self.assertEqual((output / "demo-summary.json").read_bytes(), previous)

    def test_mcp_config_is_valid_toml_and_preserves_unicode_paths(self):
        read_root = self.root / "原始数据"
        read_root.mkdir()
        output_root = self.root / "本地结果"
        site = self.make_site()
        code, snippet, err = self.call_cli(
            "mcp-config", "--read-root", str(read_root), "--output-root",
            str(output_root), "--site", str(site),
        )
        self.assertEqual((code, err), (0, ""))
        parsed = tomllib.loads(snippet)
        server = parsed["mcp_servers"]["spire"]
        self.assertIn("-m", server["args"])
        self.assertIn("runtime_layer.agent_cli", server["args"])
        self.assertIn(str(read_root.resolve()), server["args"])
        self.assertEqual(server["env"]["NANONIS_BACKEND"], "fake")
        self.assertEqual(server["env"]["RUN_LIVE_NANONIS"], "0")
        self.assertEqual(server["env"]["SPIRE_CONNECTION_CONFIG"], str((site / "connection.toml").resolve()))
        self.assertIn("原始数据", snippet)


if __name__ == "__main__":
    unittest.main()
