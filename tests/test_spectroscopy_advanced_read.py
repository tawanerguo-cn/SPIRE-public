from pathlib import Path
import importlib.util
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import struct

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from instrument_layer import FakeNanonisBackend, NanonisDevice, NanonisCommandError
from instrument_layer import OfficialNanonisBackend, NanonisConnectionConfig


# Exact read-only Device 1 body, 2026-09-20 19:12 +08:00. No names count after size.
CHANNEL_BODY = bytes.fromhex(
    '00000003000000000000005600000057000000370000000b43757272656e7420284129'
    '000000104c492044656d6f642031205820284129000000104c492044656d6f642031205920284129'
    '0000000000000000')


class SpectroscopyAdvancedReadTests(unittest.TestCase):
    def test_channel_index_shapes_share_the_existing_integer_array_decoder(self):
        for indexes in ([0, 86, 87], [(0,), (86,), (87,)], [[0], [86], [87]]):
            with self.subTest(indexes=indexes):
                backend = FakeNanonisBackend()
                response = (3, indexes, 55, 3, ['Current (A)', 'LI Demod 1 X (A)', 'LI Demod 1 Y (A)'])
                with patch.object(backend, 'BiasSpectr_ChsGet', return_value=response) as read:
                    actual, names = NanonisDevice(backend=backend).get_bias_spectroscopy_channels()
                self.assertEqual(actual, (0, 86, 87))
                self.assertEqual(len(names), 3)
                read.assert_called_once_with()
                self.assertEqual(backend.command_log, [])

    def test_channel_shape_and_counts_cannot_silently_disagree(self):
        for response in (
            (), (1, [0]),
            (2, [0], 9, 1, ['X (m)']),
            (1, [0], 18, 2, ['X (m)', 'Y (m)']),
            (1, [0], 9, 2, ['X (m)']),
            (1, [(0, 1)], 9, 1, ['X (m)']),
        ):
            with self.subTest(response=response):
                backend = FakeNanonisBackend()
                with patch.object(backend, 'BiasSpectr_ChsGet', return_value=response) as read:
                    with self.assertRaisesRegex(NanonisCommandError, 'BiasSpectr.ChsGet'):
                        NanonisDevice(backend=backend).get_bias_spectroscopy_channels()
                read.assert_called_once_with()
                self.assertEqual(backend.command_log, [])

    def test_unknown_property_layout_or_flags_are_not_interpreted_as_enabled(self):
        valid = list(FakeNanonisBackend().BiasSpectr_PropsGet())
        invalid = [[], valid[:-1], valid + [0]]
        for index in (0, 2, 10, 11):
            response = valid.copy()
            response[index] = 2
            invalid.append(response)
        for response in invalid:
            with self.subTest(response=response):
                backend = FakeNanonisBackend()
                with patch.object(backend, 'BiasSpectr_PropsGet', return_value=response) as read:
                    with self.assertRaisesRegex(NanonisCommandError, 'BiasSpectr.PropsGet'):
                        NanonisDevice(backend=backend).get_bias_spectroscopy_properties()
                read.assert_called_once_with()
                self.assertEqual(backend.command_log, [])

    @unittest.skipUnless(importlib.util.find_spec('nanonis_spm'), 'optional official client parser, offline only')
    def test_recorded_channel_variants_through_actual_official_parser(self):
        counted = CHANNEL_BODY[:20] + struct.pack('>i', 3) + CHANNEL_BODY[20:]
        for body in (CHANNEL_BODY, counted):
            with self.subTest(counted=body == counted):
                # Supplying an inert connection prevents any hardware connection.
                with patch('instrument_layer.backends.socket.create_connection', side_effect=AssertionError('offline only')):
                    backend = OfficialNanonisBackend(NanonisConnectionConfig(), connection=object())
                    with patch.object(backend._client, 'send', return_value=body) as send:
                        response = backend.BiasSpectr_ChsGet()
                        self.assertEqual(response[1], body)
                        send.assert_called_once_with('BiasSpectr.ChsGet', [], [])
                        send.reset_mock()
                        indexes, names = NanonisDevice(backend=backend).get_bias_spectroscopy_channels()
                        send.assert_called_once_with('BiasSpectr.ChsGet', [], [])
                self.assertEqual(indexes, (0, 86, 87))
                self.assertEqual(names, ('Current (A)', 'LI Demod 1 X (A)', 'LI Demod 1 Y (A)'))

    @unittest.skipUnless(importlib.util.find_spec('nanonis_spm'), 'optional official client parser, offline only')
    def test_current_properties_through_actual_official_parser(self):
        def string_array(names):
            strings = b''.join(struct.pack('>i', len(name)) + name for name in names)
            return struct.pack('>ii', len(strings), len(names)) + strings

        # Synthetic current-layout packet; this is not a site acceptance capture.
        body = (struct.pack('>HiHi', 0, 2, 1, 256)
                + string_array([b'X (m)', b'Y (m)'])
                + string_array([b'Sweep Start'])
                + struct.pack('>HHII', 0, 1, 0, 0))
        with patch('instrument_layer.backends.socket.create_connection', side_effect=AssertionError('offline only')):
            backend = OfficialNanonisBackend(NanonisConnectionConfig(), connection=object())
            with patch.object(backend._client, 'send', return_value=body) as send:
                properties = NanonisDevice(backend=backend).get_bias_spectroscopy_properties()
                send.assert_called_once_with('BiasSpectr.PropsGet', [], [])
        self.assertEqual(properties.number_of_sweeps, 2)
        self.assertEqual(properties.number_of_points, 256)
        self.assertTrue(properties.backward_sweep)
        self.assertFalse(properties.save_all)
        self.assertFalse(properties.autosave)
        self.assertTrue(properties.show_save_dialog)
        self.assertEqual(properties.parameter_names, ('X (m)', 'Y (m)'))
        self.assertEqual(properties.fixed_parameter_names, ('Sweep Start',))

    def test_recorded_channel_body_gets_only_missing_count_inserted(self):
        expected = CHANNEL_BODY[:20] + struct.pack('>i', 3) + CHANNEL_BODY[20:]
        normalize = OfficialNanonisBackend._normalize_spectroscopy_channel_names
        self.assertEqual(normalize(CHANNEL_BODY), expected)
        self.assertEqual(normalize(expected), expected)
        for malformed in (CHANNEL_BODY[:-1], CHANNEL_BODY[:16], CHANNEL_BODY[:16] + struct.pack('>i', 54) + CHANNEL_BODY[20:]):
            self.assertEqual(normalize(malformed), malformed)

    def test_recorded_channels_and_current_property_schema_through_sole_backend(self):
        commands = []
        class Client:
            def __init__(self, connection): pass
            def send(self, name, args, types):
                commands.append(name)
                return CHANNEL_BODY if name == 'BiasSpectr.ChsGet' else b'properties'
            def parseGeneralResponse(self, body, types):
                if body == b'properties':
                    self_test.assertEqual(types, ['H','i','H','i','i','i','*+c','i','i','*+c','H','H'])
                    return ('', body, [0,1,0,256,9,1,['X (m)'],15,1,['Sweep Start'],0,1])
                self_test.assertEqual(body, CHANNEL_BODY[:20] + struct.pack('>i',3) + CHANNEL_BODY[20:])
                return ('', body, [3,[0,86,87],55,3,['Current (A)','LI Demod 1 X (A)','LI Demod 1 Y (A)']])
        self_test = self
        with patch.dict(sys.modules, {'nanonis_spm':SimpleNamespace(Nanonis=Client)}):
            device = NanonisDevice(backend=OfficialNanonisBackend(NanonisConnectionConfig(),connection=object()))
            indexes, names = device.get_bias_spectroscopy_channels()
            self.assertEqual(indexes, (0,86,87))
            self.assertEqual(names[-1], 'LI Demod 1 Y (A)')
            properties = device.get_bias_spectroscopy_properties()
            self.assertEqual(properties.parameter_names, ('X (m)',))
            self.assertEqual(properties.fixed_parameter_names, ('Sweep Start',))
            self.assertFalse(properties.autosave)
            self.assertTrue(properties.show_save_dialog)
        self.assertEqual(commands, ['BiasSpectr.ChsGet','BiasSpectr.PropsGet'])

    def test_linear_mode_does_not_query_mls_only_command_or_change_mode(self):
        backend = FakeNanonisBackend()
        with patch.object(backend, "BiasSpectr_MLSLockinPerSegGet", side_effect=AssertionError("MLS only")):
            state = NanonisDevice(backend=backend).get_bias_spectroscopy_advanced_settings()
        self.assertEqual(state.sweep_mode, "Linear")
        self.assertIsNone(state.lock_in_per_segment)
        self.assertEqual([name for name, _ in backend.command_log],
                         ["BiasSpectr_AdvPropsGet", "BiasSpectr_MLSModeGet"])

    def test_read_only_preserves_inherited_flags_and_mls_mode(self):
        backend = FakeNanonisBackend(bias_spectroscopy_advanced=(0, 1, 1, 0),
                                    bias_spectroscopy_sweep_mode="MLS",
                                    bias_spectroscopy_lock_in_per_segment=1)
        state = NanonisDevice(backend=backend).get_bias_spectroscopy_advanced_settings()
        self.assertFalse(state.reset_bias)
        self.assertTrue(state.z_controller_hold)
        self.assertTrue(state.record_final_z)
        self.assertFalse(state.lock_in_run)
        self.assertEqual(state.sweep_mode, "MLS")
        self.assertTrue(state.lock_in_per_segment)
        self.assertEqual([name for name, _ in backend.command_log],
                         ["BiasSpectr_AdvPropsGet", "BiasSpectr_MLSModeGet", "BiasSpectr_MLSLockinPerSegGet"])

    def test_malformed_or_unknown_values_are_not_a_valid_sts_configuration(self):
        for method, response in (
            ("BiasSpectr_AdvPropsGet", (1, 2, 0, 1)),
            ("BiasSpectr_AdvPropsGet", (1, 1)),
            ("BiasSpectr_MLSModeGet", (3, "Linear")),
            ("BiasSpectr_MLSModeGet", (5, "Other")),
            ("BiasSpectr_MLSModeGet", ()),
            ("BiasSpectr_MLSLockinPerSegGet", (2,)),
            ("BiasSpectr_AdvPropsGet", ("unsupported", b"", [])),
        ):
            with self.subTest(method=method, response=response):
                backend = FakeNanonisBackend(bias_spectroscopy_sweep_mode="MLS")
                with patch.object(backend, method, return_value=response):
                    with self.assertRaises(NanonisCommandError):
                        NanonisDevice(backend=backend).get_bias_spectroscopy_advanced_settings()
                self.assertFalse(any("Set" in name or "Start" in name for name, _ in backend.command_log))

    def test_lock_in_enum_diagnostic_keeps_raw_values_without_guessing(self):
        backend = FakeNanonisBackend(lock_in_phase_register=0)
        with self.assertRaisesRegex(NanonisCommandError, "phase_register") as caught:
            NanonisDevice(backend=backend).get_lock_in_modulator()
        evidence = caught.exception.acquired_evidence
        self.assertEqual(evidence['raw_fields']['phase_register'], (0,))
        self.assertEqual(evidence['values']['harmonic'], 1)
        self.assertEqual(evidence['values']['phase_deg'], 0.)
        self.assertNotIn('phase_register', evidence['values'])
        self.assertNotIn("LockIn_ModPhasFreqGet", [name for name, _ in backend.command_log])


if __name__ == "__main__":
    unittest.main()
