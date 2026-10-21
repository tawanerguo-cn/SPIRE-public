"""Actual client decoding with a synthetic wire body; no instrument connection."""
from pathlib import Path
import struct
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from instrument_layer import NanonisDevice, NanonisConnectionConfig, NanonisCommandError
from instrument_layer.backends import OfficialNanonisBackend

class SpectrumProtocolTests(unittest.TestCase):
    def backend(self, body):
        try:
            import nanonis_spm
        except ImportError:
            self.skipTest("optional nanonis-spm client not installed")
        backend = OfficialNanonisBackend(NanonisConnectionConfig(), connection=object())
        self.calls = []
        def send(*args):
            self.calls.append(args)
            return body
        backend._client.send = send
        return backend

    def test_float64_axes_and_density_decode_without_a_second_command(self):
        values = [1.2e-12, 2.3e-12, 4.56e-11]
        body = struct.pack(">ddi3dIi", 0.0, .9765625, 3, *values, 0, 0)
        result = NanonisDevice(backend=self.backend(body)).get_spectrum_analyzer_data()
        self.assertEqual(result.df_hz, .9765625)
        self.assertEqual(result.density, tuple(values))
        self.assertEqual(self.calls, [("SpectrumAnlzr.DataGet", [1], ["i"])])

    def test_closed_analyzer_reports_the_original_error(self):
        msg = b"Cannot access the Spectrum Analyzer."
        body = struct.pack(">Ii", 1, len(msg)) + msg
        with self.assertRaisesRegex(NanonisCommandError, "Cannot access"):
            NanonisDevice(backend=self.backend(body)).get_spectrum_analyzer_data(2)
        self.assertEqual(len(self.calls), 1)
