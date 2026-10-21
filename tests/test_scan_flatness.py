from __future__ import annotations

from dataclasses import replace
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analysis_layer.scan_flatness import FlatnessSettings, screen_flatness
from analysis_layer.scan_images import process_scan_image
from knowledge_base import load_tip_conditioning_knowledge
from runtime_layer.spm_tools import SPMTools
from workflow_layer.scan_flatness import assess_saved_scan_flatness
from workflow_layer.scan_observation import observe_saved_scan
from test_tip_reference_verification import _write_sxm


def _write_flatness_fixture(path, image):
    """Write one deterministic height field through the standard SXM reader."""
    image = np.asarray(image, dtype=float)
    _write_sxm(path, forward=image, backward=np.fliplr(image), scan_direction="down",
               range_x_m=image.shape[1] * 1e-9, range_y_m=image.shape[0] * 1e-9,
               scan_angle_deg=0.0)


class FlatnessTests(unittest.TestCase):
    def setUp(self):
        self.settings = FlatnessSettings(**load_tip_conditioning_knowledge()["flatness_screen_profiles"]["ag100_site_screen_v1"]["settings"])
        self.rng = np.random.default_rng(771)
        self.y, self.x = np.indices((32, 128))
        self.plane = self.x * 50e-12 + self.y * 30e-12

    def screen(self, image, noise=3e-12):
        return screen_flatness(image + self.rng.normal(0, noise, image.shape),
                               image + self.rng.normal(0, noise, image.shape), settings=self.settings)

    def test_linear_fit_preserves_mask_and_does_not_modify_raw(self):
        raw = self.plane.copy()
        raw[0, :3] = np.nan
        raw[1] = np.nan
        before = raw.copy()
        result = process_scan_image(raw, "line_linear")
        np.testing.assert_array_equal(raw, before)
        np.testing.assert_array_equal(np.isnan(raw), np.isnan(result))
        self.assertLess(np.nanmax(abs(result)), 1e-22)
        self.assertFalse(result.flags.writeable)

    def test_tilt_and_bounded_noise_are_flat_not_corrugation(self):
        self.assertEqual(self.screen(self.plane)["verdict"], "flat_candidate")
        self.assertEqual(self.screen(self.plane, noise=12e-12)["verdict"], "flat_candidate")

    def test_vertical_diagonal_and_horizontal_steps_not_rejected_as_rough(self):
        for extra in (self.x > 38, self.x > 38 + self.y/4, self.y > 12):
            with self.subTest(extra=extra[0].sum()):
                result = self.screen(self.plane + extra * .23e-9)
                self.assertNotIn(result["verdict"], ("rough_or_unstable", "uncertain_retrace"))
                self.assertGreater(result["flat_or_terraced_fraction"], .9)

    def test_whole_line_jump_is_not_erased_by_display(self):
        result = self.screen(self.plane + (self.y > 12)*2e-9)
        self.assertIn(13, result["line_boundaries"])
        self.assertNotEqual(result["verdict"], "flat_candidate")

    def test_widespread_bumps_rejected_and_isolated_feature_retained(self):
        bumpy = self.plane + 1e-9*np.cos(self.x/3)*np.cos(self.y/4)
        self.assertEqual(self.screen(bumpy)["verdict"], "rough_or_unstable")
        bump = self.plane + .8e-9*np.exp(-((self.x-60)**2+(self.y-16)**2)/10)
        result = self.screen(bump)
        self.assertEqual(result["verdict"], "mixed")
        self.assertGreater(result["area_fractions"]["flat"], .7)

    def test_opposite_artifacts_cannot_average_to_flat(self):
        disturbance = .5e-9*np.sin(self.x/3)
        result = screen_flatness(self.plane+disturbance, self.plane-disturbance, settings=self.settings)
        self.assertEqual(result["verdict"], "uncertain_retrace")

    def test_missing_and_frozen_inputs_not_flat(self):
        self.assertEqual(screen_flatness(np.zeros((8, 16)), np.zeros((8, 16)), settings=self.settings)["verdict"], "insufficient_evidence")
        for value in (np.nan, np.inf):
            with self.assertRaises(ValueError):
                screen_flatness([[value]*4]*4, [[0]*4]*4, settings=self.settings)
        with self.assertRaises(ValueError):
            replace(self.settings, residual_limit_pm=float("nan"))

    def test_up_down_prefix_and_gap_never_use_future_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"source.sxm"
            _write_sxm(path)
            path.write_bytes(path.read_bytes().replace(b":SCAN_DIR:", b":SCAN_ANGLE:\n0\n:SCAN_DIR:"))
            original = observe_saved_scan(path)
            for direction in ("up", "down"):
                raw = self.plane[:32, :32].copy()
                # Geometrically early rows are clean; later rows have severe bumps.
                indices = list(range(31, -1, -1)) if direction == "up" else list(range(32))
                raw[indices[12:]] += np.cos(np.arange(32)/2)*1e-9
                observation = replace(original, scan_direction=direction, raw=raw, backward=raw.copy())
                with patch("workflow_layer.scan_flatness.observe_saved_scan", return_value=observation):
                    report, _ = assess_saved_scan_flatness(path, profile="ag100_site_screen_v1", render=False)
                    self.assertEqual(report["selected_rows_acquisition_order"], indices[:12])
                    self.assertEqual(report["verdict"], "flat_candidate")
                raw[indices[2], 0] = np.nan
                with patch("workflow_layer.scan_flatness.observe_saved_scan", return_value=observation):
                    report, _ = assess_saved_scan_flatness(path, profile="ag100_site_screen_v1", render=False)
                    self.assertEqual(report["verdict"], "insufficient_evidence")
                    self.assertEqual(report["selected_rows_acquisition_order"], indices[:2])

    def test_synthetic_flat_rough_and_terraced_scans_keep_separate_verdicts(self):
        synthetic = {
            "flat": self.plane.copy(),
            "rough": self.plane + 400e-12 * np.sin(self.x / 2) * np.cos(self.y / 3),
            "terraced": self.plane + 250e-12 * (self.x > 53),
        }
        expected = {"flat": "flat_candidate", "rough": "rough_or_unstable",
                    "terraced": "terraced_candidate"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, image in synthetic.items():
                path = root / f"synthetic-{name}.sxm"
                _write_flatness_fixture(path, image)
                report, _ = assess_saved_scan_flatness(path, profile="ag100_site_screen_v1",
                                                       first_lines=None, render=False)
                with self.subTest(shape=name):
                    self.assertEqual(report["verdict"], expected[name])
                    self.assertEqual(report["source"]["rows"], 32)
                    self.assertEqual(report["source"]["columns"], 128)
                    self.assertEqual(len(report["selected_rows_acquisition_order"]), 32)
                    self.assertFalse(report["automatic_tip_acceptance"])

    def test_synthetic_surface_persists_report_rejects_roots_and_bad_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            path = allowed / "synthetic-flat.sxm"
            _write_flatness_fixture(path, self.plane)
            external_path = outside / "synthetic-outside.sxm"
            _write_flatness_fixture(external_path, self.plane)
            surface = SPMTools(read_roots=[allowed], output_root=root / "out")
            result = surface.assess_scan_flatness(str(path), "ag100_site_screen_v1", mode="metrics")
            saved = json.loads(Path(result.data["report_path"]).read_text(encoding="utf-8"))
            self.assertTrue(saved["tiles"])
            self.assertFalse(result.data["authorizes_live_action"])
            for kwargs in ({"first_lines": True}, {"first_lines": 0}, {"roi": [-1,4,0,4]}, {"mode": "bad"}):
                with self.assertRaises(ValueError):
                    surface.assess_scan_flatness(str(path), "ag100_site_screen_v1", **kwargs)
            with self.assertRaises(ValueError):
                surface.assess_scan_flatness(str(path), "universal")
            with self.assertRaisesRegex(ValueError, "outside"):
                surface.assess_scan_flatness(str(external_path), "ag100_site_screen_v1")

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "optional validation dependency")
    def test_synthetic_preview_renders_raw_and_linear_pair_from_exact_prefix(self):
        image = self.plane.copy()
        image[12:] += 400e-12 * np.sin(self.x[12:] / 2) * np.cos(self.y[12:] / 3)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic-flat-prefix.sxm"
            _write_flatness_fixture(path, image)
            report, preview = assess_saved_scan_flatness(path, profile="ag100_site_screen_v1")
        self.assertTrue(preview.startswith(b"\x89PNG"))
        self.assertEqual(report["selected_rows_acquisition_order"], list(range(12)))
        self.assertEqual(report["selected_roi"], [0, 12, 0, 128])
        self.assertTrue(report["requested_prefix_complete"])
        from matplotlib import image as mpimg
        rendered = mpimg.imread(BytesIO(preview), format="png")
        self.assertEqual(rendered.ndim, 3)
        self.assertIn(rendered.shape[2], (3, 4))
        self.assertGreater(rendered.shape[1], rendered.shape[0])


if __name__ == "__main__":
    unittest.main()
