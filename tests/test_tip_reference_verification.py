from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analysis_layer import (  # noqa: E402
    analyze_ag111_spectrum,
    analyze_compact_protrusion,
)
from data_layer import read_nanonis_dat, read_nanonis_sxm  # noqa: E402
from workflow_layer import analyze_tip_evidence  # noqa: E402


class TipReferenceVerificationTests(unittest.TestCase):
    def test_offline_readers_and_analysis_are_reusable_without_instrument(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scan_path = root / "partial.sxm"
            spectrum_path = root / "spectrum.dat"
            _write_sxm(scan_path)
            _write_dat(spectrum_path)

            scan_file = read_nanonis_sxm(scan_path)
            spectrum_file = read_nanonis_dat(spectrum_path)
            protrusion = analyze_compact_protrusion(scan_file)
            spectrum = analyze_ag111_spectrum(spectrum_file)

            self.assertEqual(scan_file.channel("z").direction, "both")
            self.assertEqual(protrusion.acquired_rows, 32)
            self.assertAlmostEqual(protrusion.center_x_m, 20.5e-9)
            self.assertAlmostEqual(protrusion.center_y_m, 16.5e-9)
            self.assertLess(protrusion.protrusion_fwhm_aspect_ratio, 1.2)
            self.assertGreater(protrusion.z_forward_backward_correlation, 0.999)
            self.assertTrue(spectrum.finite_and_monotonic)
            self.assertAlmostEqual(spectrum.surface_state_onset_v, -0.068, delta=0.004)
            self.assertGreater(spectrum.didv_step_to_noise, 20.0)

            combined = analyze_tip_evidence(scan_path, spectrum_path)
            self.assertEqual(combined.scan_sha256, scan_file.sha256)
            self.assertEqual(combined.spectrum_sha256, spectrum_file.sha256)

    def test_sxm_reader_rejects_truncated_binary_payload(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "truncated.sxm"
            _write_sxm(path)
            path.write_bytes(path.read_bytes()[:-4])

            with self.assertRaisesRegex(ValueError, "binary payload"):
                read_nanonis_sxm(path)

    def test_dat_reader_rejects_malformed_rows(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "malformed.dat"
            path.write_text("[DATA]\nA\tB\n1\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "expected 2"):
                read_nanonis_dat(path)


def _write_sxm(path: Path, *, forward=None, backward=None, scan_direction="up",
               range_x_m=None, range_y_m=None, acquired_rows=None,
               scan_angle_deg=None) -> None:
    """Write the default fixture or caller-supplied synthetic SXM channel arrays."""
    if forward is None and backward is None:
        rows = columns = 32
        y, x = np.indices((rows, columns))
        background = 0.02e-9 * x + 0.01e-9 * y
        bump = 0.8e-9 * np.exp(
            -0.5 * (((x - 20.0) / 1.2) ** 2 + ((y - 16.0) / 1.25) ** 2)
        )
        first = background + bump
        second = np.fliplr(first).copy()
        range_x_m = range_x_m or 32e-9
        range_y_m = range_y_m or 32e-9
    elif forward is not None and backward is not None:
        first, second = np.asarray(forward, dtype=float), np.asarray(backward, dtype=float)
        if first.ndim != 2 or first.shape != second.shape or min(first.shape) < 4:
            raise ValueError("synthetic SXM channels must be equal 2-D arrays at least 4 by 4")
        rows, columns = first.shape
        range_x_m = range_x_m or columns * 1e-9
        range_y_m = range_y_m or rows * 1e-9
    else:
        raise ValueError("provide both synthetic forward and backward channels")
    if scan_direction not in {"up", "down"}:
        raise ValueError("scan_direction must be up or down")
    if acquired_rows is not None:
        if type(acquired_rows) is not int or not 1 <= acquired_rows <= rows:
            raise ValueError("acquired_rows must select at least one and at most all scan rows")
        first, second = first.copy(), second.copy()
        first[acquired_rows:, :] = np.nan
        second[acquired_rows:, :] = np.nan
    first = np.asarray(first, dtype=np.float32)
    second = np.asarray(second, dtype=np.float32)
    angle = "" if scan_angle_deg is None else f":SCAN_ANGLE:\n{scan_angle_deg:g}\n"
    header = f""":SXM_VERSION:
2
:REC_DATE:
05.08.2026
:REC_TIME:
18:10:42
:SCAN_PIXELS:
{columns} {rows}
:SCAN_RANGE:
{range_x_m:.12e} {range_y_m:.12e}
:SCAN_OFFSET:
0 0
{angle}:SCAN_DIR:
{scan_direction}
:BIAS:
-0.2
:Z-CONTROLLER:
\tName\ton\tSetpoint\tP-gain\tI-gain\tT-const
\tlog Current\t1\t1e-10 A\t1e-11 m\t1e-7 m/s\t1e-4 s
:DATA_INFO:
\tChannel\tName\tUnit\tDirection\tCalibration\tOffset
\t14\tZ\tm\tboth\t1\t0
:SCANIT_END:


""".encode("latin-1")
    binary = np.concatenate([first.ravel(), second.ravel()]).astype(">f4")
    path.write_bytes(header + b"\x1a\x04" + binary.tobytes())


def _write_dat(path: Path) -> None:
    bias = np.linspace(-0.2, 0.2, 201)
    didv = 1.2e-12 + 3.3e-12 / (1.0 + np.exp(-(bias + 0.067) / 0.003))
    didv += 0.03e-12 * np.sin(np.arange(len(bias)) * 0.71)
    current = -bias * 500e-12
    quadrature = np.full_like(bias, 3.7e-12)
    lines = [
        "Experiment\tbias spectroscopy\t",
        "Saved Date\t05.08.2026 18:11:46\t",
        "[DATA]",
        "Bias calc (V)\tCurrent (A)\tLI Demod 1 X (A)\tLI Demod 1 Y (A)",
    ]
    lines.extend(
        "\t".join(f"{value:.12e}" for value in row)
        for row in zip(bias, current, didv, quadrature)
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
