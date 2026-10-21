from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest.mock import patch
import tomllib
import json
import copy
import unittest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from knowledge_base import (  # noqa: E402
    load_gui_action_binding_profile,
    load_instrument_profile,
    load_tip_quality_reference,
)
from instrument_layer.models import to_jsonable


class InstrumentProfileKnowledgeTests(unittest.TestCase):
    def test_reviewed_gui_action_map_exposes_only_pmd4_xy_controls(self) -> None:
        profile = load_gui_action_binding_profile()

        self.assertEqual(profile.status, "reviewed")
        self.assertEqual(len(profile.bindings), 4)
        self.assertEqual(
            {binding.control_label for binding in profile.bindings},
            {"+X", "-X", "+Y", "-Y"},
        )
        self.assertEqual(
            {binding.action_name for binding in profile.bindings},
            {"coarse_xy_move"},
        )
        self.assertFalse(
            any(
                binding.arguments.get("axis") == "z"
                for binding in profile.bindings
            )
        )

    def test_gui_action_profile_identifier_cannot_escape_directory(self) -> None:
        with self.assertRaises(ValueError):
            load_gui_action_binding_profile("../recording")

    def test_reviewed_station_profile_contains_current_lab_facts(self) -> None:
        profile = load_instrument_profile("spire_local_station")

        self.assertEqual(profile.status, "reviewed")
        self.assertGreaterEqual(len(profile.evidence), 4)
        self.assertEqual(profile.ccd_friendly_name, "UGREEN 25173")
        self.assertEqual(profile.ccd_interface, "UVC")
        self.assertEqual(
            profile.ccd_capture_backend,
            "opencv_directshow_available",
        )
        self.assertEqual(profile.ccd_vision_gate_status, "not_implemented")
        self.assertEqual(
            profile.lowest_experiment_setpoint_range_a,
            (2e-12, 3e-12),
        )

    def test_recording_protocol_is_loaded_from_reviewed_knowledge(self) -> None:
        profile = load_instrument_profile()

        self.assertEqual(
            profile.recording_signal_names,
            ("Current (A)", "X (m)", "Y (m)", "Z (m)", "LI Demod 1 X (A)", "LI Demod 1 Y (A)"),
        )
        self.assertEqual(
            profile.recording_start_condition,
            "z_controller_control_on_setpoint_window_and_z_inside_limits",
        )
        self.assertEqual(profile.minimum_effective_current_a, 1.5e-12)
        self.assertEqual(profile.minimum_setpoint_fraction, 0.7)
        self.assertEqual(profile.maximum_setpoint_fraction, 1.5)
        self.assertEqual(profile.minimum_z_limit_clearance_fraction, 1e-4)
        self.assertEqual(profile.minimum_z_limit_clearance_m, 1e-12)
        self.assertEqual(profile.arrival_hold_s, 1.0)
        self.assertEqual(profile.arrival_minimum_samples, 8)
        self.assertEqual(profile.far_noise_current_ceiling_a, 0.75e-12)
        self.assertEqual(profile.far_noise_window_s, 1.0)
        self.assertEqual(profile.far_noise_minimum_samples, 8)
        self.assertEqual(profile.far_noise_hold_s, 0.0)
        self.assertFalse(profile.zctrl_withdraw_closes_segment)

    def test_knowledge_does_not_claim_to_be_the_safety_enforcer(self) -> None:
        profile = load_instrument_profile()

        self.assertEqual(profile.coarse_minus_z_live_permission, "forbidden")
        self.assertIn("X+/X-/Y+/Y-", profile.motor_move_enforcement)
        self.assertIn("code 5 (Z-) has no wrapper", profile.motor_move_enforcement)

    def test_follow_me_live_commissioning_is_reviewed_station_knowledge(self) -> None:
        profile = load_instrument_profile()

        self.assertEqual(profile.follow_me_gui_state_reflection, "live_verified")
        self.assertEqual(profile.follow_me_live_status, "commissioned_bounded_action")
        self.assertEqual(profile.follow_me_commissioned_on, "2026-08-12")
        self.assertEqual(profile.scan_start_live_status, "commissioned_bounded_action")
        self.assertEqual(profile.scan_start_commissioned_on, "2026-08-12")
        self.assertIn("must be explicit", profile.scan_direction_readback)

    def test_profile_identifier_cannot_escape_the_knowledge_directory(self) -> None:
        with self.assertRaises(ValueError):
            load_instrument_profile("../recording")

    def test_reviewed_ag111_tip_reference_preserves_evidence_and_limits(self) -> None:
        reference = load_tip_quality_reference("ag111_short_scan_reference")

        self.assertEqual(reference.status, "reviewed")
        self.assertEqual(reference.surface, "Ag(111)")
        self.assertEqual(reference.verdict, "excellent_tip_reference")
        self.assertEqual(reference.targets, ("imaging", "sts"))
        self.assertFalse(reference.recipe_inference_permitted)
        self.assertFalse(reference.source_available)
        self.assertIsNone(reference.run_id)
        self.assertIsNone(reference.database_path)
        self.assertIsNone(reference.database_sha256)
        self.assertEqual(reference.action_identity, "unknown")
        self.assertIn(
            "approximately circular",
            reference.operator_quick_judgment_rule,
        )
        self.assertEqual(reference.segment_id, 2)
        self.assertEqual(reference.signal_samples, 9452)
        self.assertEqual(reference.estimated_missing_samples, 17)
        spectrum = next(
            item for item in reference.evidence if item.evidence_id == "spectrum_primary"
        )
        self.assertEqual(spectrum.metric("points"), 201)
        self.assertAlmostEqual(
            float(spectrum.metric("surface_state_onset_v")),
            -0.068000004,
        )
        self.assertGreater(float(spectrum.metric("didv_step_to_noise")), 40.0)
        partial = next(
            item for item in reference.evidence if item.evidence_id == "scan_primary"
        )
        self.assertIsNone(partial.source_path)
        self.assertIsNone(partial.sha256)
        self.assertEqual(partial.metric("acquired_rows"), 19)
        self.assertAlmostEqual(
            float(partial.metric("protrusion_equivalent_fwhm_radius_m")),
            1.2716705092250608e-9,
        )
        self.assertGreater(
            float(partial.metric("z_forward_backward_correlation")),
            0.98,
        )

    def test_tip_reference_identifier_cannot_escape_knowledge_directory(self) -> None:
        with self.assertRaises(ValueError):
            load_tip_quality_reference("../recording")

    def test_source_paths_keep_foreign_os_semantics_and_reject_relative_paths(self) -> None:
        source = Path(__file__).resolve().parents[1] / "src/knowledge_base/tip_quality_references/ag111_short_scan_reference.toml"
        for value, expected in (("D:/DATA/run.sqlite", PureWindowsPath),
                                (r"\\server\share\run.sqlite", PureWindowsPath),
                                ("/data/run.sqlite", PurePosixPath)):
            with self.subTest(value=value):
                payload = tomllib.loads(source.read_text(encoding="utf-8"))
                payload["reference"]["source_available"] = True
                payload["provenance"]["database_path"] = value
                payload["provenance"]["database_sha256"] = "a" * 64
                for index, entry in enumerate(payload["evidence"]):
                    entry["source_path"] = value
                    entry["sha256"] = str(index + 1) * 64
                with patch("knowledge_base.tip_quality_reference.tomllib.load", return_value=payload):
                    reference = load_tip_quality_reference()
                self.assertIsInstance(reference.database_path, expected)
                self.assertTrue(reference.database_path.is_absolute())
                self.assertTrue(reference.evidence[0].source_path.is_absolute())
                self.assertEqual(json.loads(json.dumps(to_jsonable(reference)))["database_path"],
                                 str(reference.database_path))
        for value in ("relative/run.sqlite", "D:run.sqlite", r"\data\run.sqlite", "", None):
            for section in ("database", "evidence"):
                with self.subTest(value=value, section=section):
                    payload = tomllib.loads(source.read_text(encoding="utf-8"))
                    payload["reference"]["source_available"] = True
                    if section == "database":
                        payload["provenance"]["database_path"] = value
                    else:
                        payload["evidence"][0]["source_path"] = value
                    if section == "database":
                        payload["provenance"]["database_sha256"] = "a" * 64
                    else:
                        payload["evidence"][0]["sha256"] = "b" * 64
                    with patch("knowledge_base.tip_quality_reference.tomllib.load", return_value=payload):
                        with self.assertRaises(ValueError):
                            load_tip_quality_reference()

    def test_source_hashes_stay_strict_when_raw_files_are_available(self) -> None:
        source = Path(__file__).resolve().parents[1] / "src/knowledge_base/tip_quality_references/ag111_short_scan_reference.toml"
        payload = tomllib.loads(source.read_text(encoding="utf-8"))
        payload["reference"]["source_available"] = True
        payload["provenance"]["database_path"] = "D:/lab/run.sqlite"
        payload["provenance"]["database_sha256"] = "a" * 64
        for index, entry in enumerate(payload["evidence"]):
            entry["source_path"] = f"D:/lab/reference-{index}.dat"
            entry["sha256"] = str(index + 1) * 64
        for table, field, value in (
            ("provenance", "database_sha256", "not-published"),
            ("evidence", "sha256", "f" * 63),
        ):
            with self.subTest(table=table, field=field, value=value):
                broken = copy.deepcopy(payload)
                if table == "evidence":
                    broken[table][0][field] = value
                else:
                    broken[table][field] = value
                with patch("knowledge_base.tip_quality_reference.tomllib.load", return_value=broken):
                    with self.assertRaisesRegex(ValueError, "SHA-256"):
                        load_tip_quality_reference()

    def test_unavailable_sources_require_boolean_and_no_source_identifiers(self) -> None:
        source = Path(__file__).resolve().parents[1] / "src/knowledge_base/tip_quality_references/ag111_short_scan_reference.toml"
        original = tomllib.loads(source.read_text(encoding="utf-8"))
        for value in ("false", 0, None):
            with self.subTest(source_available=value):
                payload = copy.deepcopy(original)
                payload["reference"]["source_available"] = value
                with patch("knowledge_base.tip_quality_reference.tomllib.load", return_value=payload):
                    with self.assertRaisesRegex(ValueError, "TOML boolean"):
                        load_tip_quality_reference()
        for table, field, value in (
            ("provenance", "database_path", "D:/private/run.sqlite"),
            ("provenance", "database_sha256", "a" * 64),
            ("evidence", "source_path", "D:/private/scan.sxm"),
            ("evidence", "sha256", "b" * 64),
        ):
            with self.subTest(table=table, field=field):
                payload = copy.deepcopy(original)
                target = payload[table] if table == "provenance" else payload[table][0]
                target[field] = value
                with patch("knowledge_base.tip_quality_reference.tomllib.load", return_value=payload):
                    with self.assertRaises(ValueError):
                        load_tip_quality_reference()


if __name__ == "__main__":
    unittest.main()
