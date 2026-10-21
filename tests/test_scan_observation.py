from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import sys
import importlib.util
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analysis_layer import AtomDetectorSettings, LocalMaximumAtomDetector, pixel_center_to_xy
from analysis_layer.scan_images import process_scan_image, render_scan_observation
from data_layer import read_nanonis_sxm
from instrument_layer import ScanFrame
from data_layer.scan_observation import write_observation_bundle
from workflow_layer.scan_observation import export_saved_scan_observation, inspect_saved_scan, observe_saved_scan
from test_tip_reference_verification import _write_sxm


class ScanObservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "scan.sxm"
        _write_sxm(self.path)
        self.path.write_bytes(self.path.read_bytes().replace(
            b":SCAN_DIR:\nup", b":SCAN_ANGLE:\n90\n:SCAN_DIR:\nup"))

    def test_up_retrace_rotation_and_pixel_coordinates(self):
        raw = read_nanonis_sxm(self.path)
        observed = observe_saved_scan(self.path)
        np.testing.assert_array_equal(observed.raw, raw.channel("Z").forward[::-1])
        np.testing.assert_array_equal(observed.raw, observed.backward)
        # Original feature at (16,20) becomes top-origin row 15. At +90 degrees
        # the +local_x displacement rotates toward -world_y.
        x, y = pixel_center_to_xy(observed.frame, 32, 32, 15, 20)
        self.assertAlmostEqual(x / 1e-9, 0.5)
        self.assertAlmostEqual(y / 1e-9, -4.5)
        self.assertFalse(observed.raw.flags.writeable)

    def test_down_forward_is_not_flipped(self):
        self.path.write_bytes(self.path.read_bytes().replace(b":SCAN_DIR:\nup", b":SCAN_DIR:\ndown"))
        raw = read_nanonis_sxm(self.path)
        observed = observe_saved_scan(self.path)
        np.testing.assert_array_equal(observed.raw, raw.channel("Z").forward)
        np.testing.assert_array_equal(observed.raw, observed.backward)

    def test_missing_angle_cannot_be_used_for_targeting(self):
        self.path.write_bytes(self.path.read_bytes().replace(b":SCAN_ANGLE:\n90\n", b""))
        scan = read_nanonis_sxm(self.path)
        self.assertIsNone(scan.scan_angle_deg)
        with self.assertRaisesRegex(ValueError, "SCAN_ANGLE"):
            observe_saved_scan(self.path)

    def test_partial_scan_keeps_full_grid_and_excludes_nan_neighbours(self):
        payload = self.path.read_bytes()
        start = payload.index(b"\x1a\x04") + 2
        values = np.frombuffer(payload[start:], dtype=">f4").copy().reshape(2, 32, 32)
        values[:, 20:, :] = np.nan
        self.path.write_bytes(payload[:start] + values.astype(">f4").tobytes())
        observed, candidates, summary = inspect_saved_scan(self.path)
        self.assertEqual(observed.raw.shape, (32, 32))
        self.assertTrue(np.isnan(observed.raw[:12]).all())
        self.assertAlmostEqual(summary["valid_fraction"], 20 / 32)
        self.assertTrue(all(candidate.row >= 13 for candidate in candidates))

    def test_source_or_processing_change_invalidates_identity(self):
        original = observe_saved_scan(self.path)
        self.assertNotEqual(original.observation_id, observe_saved_scan(self.path, processing="raw").observation_id)
        self.path.write_bytes(self.path.read_bytes().replace(b":SCAN_ANGLE:\n90", b":SCAN_ANGLE:\n45"))
        self.assertNotEqual(original.observation_id, observe_saved_scan(self.path).observation_id)

    def test_export_is_non_overwriting_and_has_source_evidence(self):
        observed, _, result = inspect_saved_scan(self.path)
        write_observation_bundle(observed, self.root / "output", summary=result, images={})
        self.assertEqual(result["source_sha256"], read_nanonis_sxm(self.path).sha256)
        with self.assertRaises(FileExistsError):
            write_observation_bundle(observed, self.root / "output", summary=result, images={})

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "rendering requires the validation extra")
    def test_rendered_bundle_has_png_previews(self):
        export_saved_scan_observation(self.path, self.root / "output")
        self.assertTrue((self.root / "output" / "overview.png").read_bytes().startswith(b"\x89PNG"))

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "rendering requires the validation extra")
    def test_rectangular_partial_preview_preserves_physical_shape_and_retrace_scale(self):
        from matplotlib.figure import Figure
        observed = observe_saved_scan(self.path)
        # Unequal pixel pitches reproduce the field case: a100x15nm short strip,
        # stored in a304x400 grid. Pixel-square and subplot-fill rendering both fail.
        data = np.full((400, 304), np.nan)
        data[340:] = np.arange(60 * 304).reshape(60, 304) * 1e-12
        backward = data + 2e-9
        observed = replace(observed, frame=ScanFrame(0, 0, 100e-9, 100e-9, 0),
                           raw=data, backward=backward, processed=process_scan_image(data),
                           valid_mask=np.isfinite(data))
        figures = []
        save = Figure.savefig
        def capture(figure, *args, **kwargs):
            figures.append(figure)
            return save(figure, *args, **kwargs)
        with patch.object(Figure, "savefig", capture):
            png = render_scan_observation(observed, crop=(340, 400, 0, 304))
        self.assertTrue(png.startswith(b"\x89PNG"))
        figure = figures[0]
        figure.canvas.draw()
        axes = [axis for axis in figure.axes if axis.images]
        self.assertEqual(len(axes), 4)
        for axis in axes:
            box = axis.get_window_extent()
            self.assertAlmostEqual(box.width / box.height, 100 / 15)
            # A nanometre has identical screen length in both directions.
            p0, px, py = axis.transData.transform([(0, 0), (1, 0), (0, 1)])
            self.assertAlmostEqual(np.linalg.norm(px - p0), np.linalg.norm(py - p0))
        self.assertEqual(axes[0].images[0].get_clim(), axes[1].images[0].get_clim())
        self.assertEqual(axes[2].images[0].get_clim(), axes[3].images[0].get_clim())
        np.testing.assert_allclose(axes[0].images[0].get_extent(), [-50, 50, -50, -35])
        np.testing.assert_array_equal(axes[0].images[0].get_array(), data[340:] * 1e9)
        np.testing.assert_array_equal(axes[1].images[0].get_array(), backward[340:] * 1e9)

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "rendering requires the validation extra")
    def test_acquired_only_preview_keeps_full_arrays_and_missing_rows(self):
        payload = self.path.read_bytes()
        start = payload.index(b"\x1a\x04") + 2
        values = np.frombuffer(payload[start:], dtype=">f4").copy().reshape(2, 32, 32)
        values[:, 20:] = np.nan
        values[:, 10] = np.nan  # Preserve a real hole, never compress acquired rows.
        self.path.write_bytes(payload[:start] + values.astype(">f4").tobytes())
        result = export_saved_scan_observation(self.path, self.root / "partial", acquired_only=True)
        self.assertEqual(result["preview_crop"], [12, 32, 0, 32])
        saved = np.load(self.root / "partial" / "arrays.npz")
        self.assertEqual(saved["raw"].shape, (32, 32))
        self.assertTrue(np.isnan(saved["raw"][:12]).all())
        self.assertTrue(np.isnan(saved["raw"][21]).all())
        self.assertEqual(result["source_sha256"], read_nanonis_sxm(self.path).sha256)
        with self.assertRaisesRegex(ValueError, "either"):
            export_saved_scan_observation(self.path, self.root / "invalid", acquired_only=True, crop=(12, 32, 0, 32))

    def test_invalid_geometry_and_binary_format_are_rejected(self):
        payload = self.path.read_bytes()
        _write_sxm(self.path, range_x_m=-1e-9)
        with self.assertRaisesRegex(ValueError, "ranges"):
            read_nanonis_sxm(self.path)
        self.path.write_bytes(b":SCANIT_TYPE:\nFLOAT LSBFIRST\n" + payload)
        with self.assertRaisesRegex(ValueError, "MSBFIRST"):
            read_nanonis_sxm(self.path)

    def test_detector_settings_and_out_of_image_positions_rejected(self):
        with self.assertRaises(ValueError):
            LocalMaximumAtomDetector(AtomDetectorSettings(maximum_candidates=0))
        observed = observe_saved_scan(self.path)
        with self.assertRaises(ValueError):
            pixel_center_to_xy(observed.frame, 32, 32, 32, 0)


if __name__ == "__main__":
    unittest.main()
