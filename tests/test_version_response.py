"""Exercise the installed official response parser without a TCP connection."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import Mock, patch, PropertyMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from instrument_layer import NanonisConnectionConfig, NanonisDevice, NanonisCommandError
from instrument_layer.backends import OfficialNanonisBackend


def version_response(product, version, host, rt, error=b""):
    return (struct.pack(">i", len(product)) + product
            + struct.pack(">i", len(version)) + version
            + struct.pack(">IIII", host, rt, bool(error), len(error)) + error)


@unittest.skipUnless(importlib.util.find_spec("nanonis_spm"), "optional live client parser")
class VersionResponseTests(unittest.TestCase):
    def test_captured_seven_field_drift_and_modern_layout_preserve_error_boundary(self):
        captured = bytes.fromhex("000000010000000000000000ab8cbccc0000000000000000000000000000000000000000")
        modern = captured[:28] + struct.pack(">f", 5.) + captured[28:]
        with OfficialNanonisBackend(NanonisConnectionConfig(), connection=Mock()) as backend:
            with patch.object(backend._client, "send", side_effect=[captured, modern]):
                device = NanonisDevice(backend=backend)
                legacy = device.get_drift_compensation()
                self.assertTrue(legacy.enabled)
                self.assertAlmostEqual(legacy.vz_m_per_s / 1e-12, -1.)
                self.assertIsNone(legacy.saturation_limit_percent)
                self.assertEqual(device.get_drift_compensation().saturation_limit_percent, 5.)

    def test_legacy_stop_sends_one_four_argument_off_and_checks_seven_fields(self):
        before = struct.pack(">IfffIII", 1, 0., 0., -1e-12, 0, 0, 0) + bytes(8)
        after = bytes(36)
        with OfficialNanonisBackend(NanonisConnectionConfig(), connection=Mock()) as backend:
            # Offline adapter verification only: fake classification avoids live grants.
            with patch.object(NanonisDevice, "is_fake_backend", new_callable=PropertyMock, return_value=True), patch.object(backend._client, "send", side_effect=[before, bytes(12), bytes(8), after]) as send:
                state = NanonisDevice(backend=backend).stop_drift_compensation()
                self.assertFalse(state.enabled)
                self.assertIsNone(state.saturation_limit_percent)
                self.assertEqual(send.call_count, 4)
                self.assertEqual(send.call_args_list[2].args,
                                 ("Piezo.DriftCompSet", [0, 0., 0., 0.], ["I", "f", "f", "f"]))

    def test_drift_error_and_malformed_bodies_never_become_saturation_data(self):
        for body, expected in [
            (bytes(28) + struct.pack(">Ii", 1, 4) + b"oops", NanonisCommandError),
            (struct.pack(">Ii", 1, 4) + b"oops", NanonisCommandError),
            (bytes(35), ValueError), (bytes(41), ValueError),
            (bytes(32) + struct.pack(">Ii", 0, -1), ValueError),
            (bytes(28) + struct.pack(">Ii", 1, 0), NanonisCommandError),
        ]:
            with self.subTest(body=body.hex()), OfficialNanonisBackend(NanonisConnectionConfig(), connection=Mock()) as backend:
                with patch.object(backend._client, "send", return_value=body):
                    with self.assertRaises(expected): NanonisDevice(backend=backend).get_drift_compensation()

    def test_tip_shaper_success_error_only_and_next_command_use_official_parser(self):
        params = (.05, 0, .1, -.6e-9, .2, .1, .2, 1e-9, .3, .1, 1)
        success = struct.pack('>fIffffffffI', *params) + struct.pack('>II', 0, 0)
        error = b'Tip Shaper unavailable'
        rejected = struct.pack('>Ii', 1, len(error)) + error
        with OfficialNanonisBackend(NanonisConnectionConfig(), connection=Mock()) as backend:
            with patch.object(backend._client, 'send', side_effect=[success, rejected, success]) as send:
                device = NanonisDevice(backend=backend)
                self.assertAlmostEqual(device.get_tip_shaper_properties().tip_lift_m / 1e-9, -.6)
                with self.assertRaisesRegex(NanonisCommandError, 'Tip Shaper unavailable'):
                    device.get_tip_shaper_properties()
                self.assertTrue(device.get_tip_shaper_properties().restore_feedback)
                self.assertEqual(send.call_count, 3)
                send.assert_called_with('TipShaper.PropsGet', [], [])

    def test_official_parser_reads_length_prefixed_strings_and_unsigned_releases(self):
        for product, version, host, rt in [
            (b"Nanonis SPM Control Software", b"Generic 5", 13468, 10131),
            (b"Nanonis Tramea Software", b"Different version length", 0x80000001, 0xffffffff),
        ]:
            with self.subTest(product=product), OfficialNanonisBackend(
                NanonisConnectionConfig(), connection=Mock()
            ) as backend:
                body = version_response(product, version, host, rt)
                with patch.object(backend._client, "send", return_value=body) as send:
                    result = NanonisDevice(backend=backend).get_version()
                self.assertEqual(result.product_line, product.decode())
                self.assertEqual(result.version, version.decode())
                self.assertEqual(result.host_release, host)
                self.assertEqual(result.rt_release, rt)
                self.assertTrue(result.available)
                send.assert_called_once_with("Util.VersionGet", [], [])

    def test_instrument_error_is_not_reported_as_a_successful_version(self):
        with OfficialNanonisBackend(NanonisConnectionConfig(), connection=Mock()) as backend:
            body = version_response(b"Nanonis", b"Generic 5", 1, 2, b"Version unavailable")
            with patch.object(backend._client, "send", return_value=body):
                with self.assertRaisesRegex(NanonisCommandError, "Version unavailable"):
                    NanonisDevice(backend=backend).get_version()

    def test_captured_error_only_response_preserves_next_session_read(self):
        body = bytes.fromhex(
            "00000001000000314572726f7220747279696e6720746f207265616420746865204d6573"
            "736167652066726f6d2074686520436c69656e742e"
        )
        session = b"D:\\Data\\test-session"
        session_body = struct.pack(">i", len(session)) + session + struct.pack(">II", 0, 0)
        with OfficialNanonisBackend(NanonisConnectionConfig(), connection=Mock()) as backend:
            with patch.object(backend._client, "send", side_effect=[body, session_body]) as send:
                device = NanonisDevice(backend=backend)
                version = device.get_version()
                self.assertFalse(version.available)
                self.assertIsNone(version.host_release)
                self.assertIsNone(version.rt_release)
                self.assertEqual(version.error, "Error trying to read the Message from the Client.")
                self.assertEqual(device.get_session_path(), session.decode())
                self.assertEqual(send.call_count, 2)

    def test_transport_timeout_is_not_hidden_as_an_unavailable_version(self):
        with OfficialNanonisBackend(NanonisConnectionConfig(), connection=Mock()) as backend:
            with patch.object(backend._client, "send", side_effect=TimeoutError("timeout")):
                with self.assertRaises(TimeoutError):
                    NanonisDevice(backend=backend).get_version()
