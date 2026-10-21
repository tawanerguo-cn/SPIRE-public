from pathlib import Path
import importlib.util
import json
import struct
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from instrument_layer import (FakeNanonisBackend, NanonisDevice, NanonisCommandError,
                              OfficialNanonisBackend, NanonisConnectionConfig)
from instrument_layer.models import to_jsonable
from instrument_layer.capabilities import IMPLEMENTED_NANONIS_OVERLAYS, CapabilityAccess
from workflow_layer.lock_in_observation import inspect_lock_in_configuration
from workflow_layer.control_step import execute_control_step


class LockInReadbackTests(unittest.TestCase):
    def test_independent_demodulator_binding_and_filter_units(self):
        backend = FakeNanonisBackend(lock_in_phase_register=1, lock_in_demod_phase_register=2,
            lock_in_demod_phase_deg=-37., lock_in_demod_harmonic=2,
            lock_in_demod_high_pass=(2, 20.), lock_in_demod_low_pass=(8, 45.),
            lock_in_demod_sync_filter=1, lock_in_demod_rt_signals=1)
        state = NanonisDevice(backend=backend).get_lock_in_demodulator(3)
        self.assertEqual(state.number, 3)
        self.assertEqual(state.signal.name, 'Current (A)')
        self.assertEqual(state.phase_register_frequency_hz, 823.)
        self.assertEqual(state.phase_deg, -37.)
        self.assertEqual(state.harmonic, 2)
        self.assertEqual((state.low_pass.order, state.low_pass.cutoff_frequency_hz), (8, 45.))
        self.assertEqual(state.high_pass.cutoff_frequency_hz, 20.)
        self.assertTrue(state.sync_filter_enabled)
        self.assertEqual(state.rt_signals, 'R/phi')
        self.assertIn(('LockIn_ModPhasFreqGet', (2,)), backend.command_log)
        self.assertTrue(all(args == (3,) for name, args in backend.command_log if name.startswith('LockIn_Demod')))
        self.assertTrue(all(name.endswith('Get') for name, _ in backend.command_log))

    def test_station_register_zero_retains_all_other_fields_and_never_guesses(self):
        # Reconstruct observed scalar fields from 176-lockin.json; not a wire capture.
        names = ['unused'] * 25
        names[0], names[24] = 'Current (A)', 'Bias (V)'
        backend = FakeNanonisBackend(lock_in_phase_register=0, lock_in_signal_index=24,
            lock_in_amplitude=0.009999999776482582, lock_in_demod_phase_register=0,
            signal_names=names)
        report = inspect_lock_in_configuration(NanonisDevice(backend=backend))
        self.assertEqual(report['status'], 'partial')
        mod = report['errors']['modulator']['acquired_evidence']
        demod = report['errors']['demodulator']['acquired_evidence']
        self.assertEqual(mod['raw_fields']['phase_register'], [0])
        self.assertEqual(mod['values']['signal']['index'], 24)
        self.assertEqual(mod['values']['amplitude'], 0.009999999776482582)
        self.assertIn('phase_deg', mod['values'])
        self.assertIn('low_pass', demod['values'])
        self.assertIn('spectroscopy', report['readings'])
        self.assertEqual(report['scientific_acceptance'], 'not_assessed')
        self.assertFalse(report['authorizes_next_action'])
        self.assertNotIn('LockIn_ModPhasFreqGet', [n for n, _ in backend.command_log])
        self.assertTrue(all(name.endswith('Get') for name, _ in backend.command_log))

    def test_explicit_frequency_read_is_not_a_register_zero_mapping(self):
        backend = FakeNanonisBackend(lock_in_phase_register=0, lock_in_demod_phase_register=0)
        report = inspect_lock_in_configuration(NanonisDevice(backend=backend), phase_registers=[2])
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['readings']['phase_register_2'], 823.)
        self.assertEqual([args for name, args in backend.command_log if name == 'LockIn_ModPhasFreqGet'], [(2,)])

    def test_unknown_enums_fractional_indices_and_nonfinite_values_rejected_with_evidence(self):
        cases = [('LockIn_DemodRTSignalsGet', (2,)), ('LockIn_DemodRTSignalsGet', (0.5,)),
            ('LockIn_DemodSyncFilterGet', (True,)), ('LockIn_DemodPhasRegGet', (1.5,)),
            ('LockIn_DemodHarmonicGet', (0,)), ('LockIn_DemodSignalGet', (128,)),
            ('LockIn_DemodHPFilterGet', (-1, 1.)), ('LockIn_DemodLPFilterGet', (9, 1.)),
            ('LockIn_DemodLPFilterGet', (4, 0.)), ('LockIn_DemodLPFilterGet', (4, -2.)),
            ('LockIn_DemodLPFilterGet', (4, float('nan'))), ('LockIn_DemodLPFilterGet', (4,)),
            ('LockIn_DemodPhasGet', (float('inf'),)), ('LockIn_DemodPhasGet', (10., 20.))]
        for method, response in cases:
            with self.subTest(method=method, response=response):
                backend = FakeNanonisBackend()
                with patch.object(backend, method, return_value=response):
                    with self.assertRaises(NanonisCommandError) as caught:
                        NanonisDevice(backend=backend).get_lock_in_demodulator()
                evidence = to_jsonable(caught.exception.acquired_evidence)
                json.dumps(evidence, allow_nan=False)
                self.assertTrue(evidence['errors'])
                self.assertTrue(all(name.endswith('Get') for name, _ in backend.command_log))

    def test_completed_vendor_error_does_not_hide_independent_reads(self):
        backend = FakeNanonisBackend()
        with patch.object(backend, 'LockIn_DemodHPFilterGet', return_value=('not available', b'raw', [])) as read:
            report = inspect_lock_in_configuration(NanonisDevice(backend=backend))
        read.assert_called_once_with(1)
        evidence = report['errors']['demodulator']['acquired_evidence']
        self.assertEqual(evidence['raw_body_hex']['high_pass'], b'raw'.hex())
        self.assertEqual(evidence['values']['rt_signals'], 'X/Y')
        self.assertEqual(evidence['errors']['high_pass'], 'not available')

    def test_timeout_stops_reads_preserves_evidence_and_does_not_retry_or_cleanup(self):
        backend = FakeNanonisBackend()
        with patch.object(backend, 'LockIn_ModAmpGet', side_effect=TimeoutError('lost response')) as read:
            report = inspect_lock_in_configuration(NanonisDevice(backend=backend))
        read.assert_called_once_with(1)
        self.assertEqual(report['status'], 'failed')
        self.assertIn('enabled', report['errors']['modulator']['acquired_evidence']['values'])
        self.assertFalse(any(name.startswith('LockIn_Demod') for name, _ in backend.command_log))
        self.assertNotIn('BiasSpectr_AdvPropsGet', [name for name, _ in backend.command_log])
        self.assertTrue(all(name.endswith('Get') for name, _ in backend.command_log))

    def test_invalid_query_arguments_rejected_before_any_read(self):
        backend = FakeNanonisBackend()
        device = NanonisDevice(backend=backend)
        for kwargs in ({'modulator_number': True}, {'demodulator_number': 0},
                       {'phase_registers': [0]}, {'phase_registers': [1, 1]}, {'phase_registers': [True]}):
            with self.assertRaises(ValueError): inspect_lock_in_configuration(device, **kwargs)
        for register in (0, 9, True, 1.5):
            with self.assertRaises(ValueError): device.get_lock_in_phase_frequency_hz(register)
        self.assertEqual(backend.command_log, [])

    def test_audited_step_retains_partial_report_and_cli_returns_nonzero(self):
        events = []
        backend = FakeNanonisBackend(lock_in_phase_register=0)
        result = execute_control_step(NanonisDevice(backend=backend, action_event_sink=events.append),
            dict(action='inspect_lock_in_configuration', arguments={}, purpose='offline diagnostic'))
        self.assertEqual(result['result']['status'], 'partial')
        self.assertIn('not_read', result['after'])
        self.assertEqual(len(events), 2)
        spec = importlib.util.spec_from_file_location('control_cli', Path(__file__).resolve().parents[1]/'tools/nanonis_control.py')
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        self.assertEqual(cli.step_outcome('inspect_lock_in_configuration', result), ('partial', 2))
        self.assertTrue(all(name.endswith('Get') for name, _ in backend.command_log))

    def test_new_capabilities_are_read_only_and_setters_remain_unexposed(self):
        names = ('LockIn_ModPhasGet', 'LockIn_DemodSignalGet', 'LockIn_DemodHarmonicGet',
                 'LockIn_DemodPhasRegGet', 'LockIn_DemodPhasGet', 'LockIn_DemodHPFilterGet',
                 'LockIn_DemodLPFilterGet', 'LockIn_DemodSyncFilterGet', 'LockIn_DemodRTSignalsGet')
        for name in names:
            self.assertEqual(IMPLEMENTED_NANONIS_OVERLAYS[name].access, CapabilityAccess.READ_ONLY)
            self.assertNotIn(name[:-3]+'Set', IMPLEMENTED_NANONIS_OVERLAYS)

    @unittest.skipUnless(importlib.util.find_spec('nanonis_spm'), 'optional official parser; offline only')
    def test_official_error_only_packet_is_not_decoded_as_filter_settings(self):
        message = b'filter unavailable'
        body = struct.pack('>Ii', 1, len(message)) + message
        with patch('instrument_layer.backends.socket.create_connection', side_effect=AssertionError('offline only')):
            backend = OfficialNanonisBackend(NanonisConnectionConfig(), connection=object())
            with patch.object(backend._client, 'send', return_value=body) as send:
                response = backend.LockIn_DemodHPFilterGet(1)
                send.assert_called_once_with('LockIn.DemodHPFilterGet', [1], ['i'])
        self.assertEqual(response, ('filter unavailable', body, []))

    @unittest.skipUnless(importlib.util.find_spec('nanonis_spm'), 'optional official parser; offline only')
    def test_official_unknown_layout_is_rejected_with_raw_body_without_retry(self):
        valid = struct.pack('>ifIi', 4, 30., 0, 0)
        for body in (valid[:-1], valid+b'\0', struct.pack('>ifIi', 4, 30., 0, 1)):
            with self.subTest(body=body), patch('instrument_layer.backends.socket.create_connection',
                    side_effect=AssertionError('offline only')):
                backend = OfficialNanonisBackend(NanonisConnectionConfig(), connection=object())
                with patch.object(backend._client, 'send', return_value=body) as send:
                    with self.assertRaisesRegex(ValueError, 'raw_body_hex='+body.hex()):
                        backend.LockIn_DemodLPFilterGet(1)
                    send.assert_called_once()

    @unittest.skipUnless(importlib.util.find_spec('nanonis_spm'), 'optional official parser; offline only')
    def test_official_wire_types_and_real_parser_with_inert_connection(self):
        # Synthetic protocol bodies, including the observed integer-zero shape.
        responses = {'LockIn.DemodSignalGet': ('i', 0), 'LockIn.DemodHarmonicGet': ('i', 2),
            'LockIn.DemodPhasRegGet': ('i', 2), 'LockIn.DemodPhasGet': ('f', -31.5),
            'LockIn.DemodHPFilterGet': ('if', 0, 0.), 'LockIn.DemodLPFilterGet': ('if', 4, 30.),
            'LockIn.DemodSyncFilterGet': ('I', 1), 'LockIn.DemodRTSignalsGet': ('I', 1),
            'LockIn.ModPhasFreqGet': ('d', 823.)}
        calls = []
        def send(command, arguments, types):
            calls.append((command, arguments, types))
            packet = responses[command]
            return struct.pack('>'+packet[0], *packet[1:]) + struct.pack('>Ii', 0, 0)
        with patch('instrument_layer.backends.socket.create_connection', side_effect=AssertionError('offline only')):
            backend = OfficialNanonisBackend(NanonisConnectionConfig(), connection=object())
            with patch.object(backend._client, 'send', side_effect=send), patch.object(
                    backend._client, 'Signals_NamesGet', return_value=FakeNanonisBackend().Signals_NamesGet()):
                state = NanonisDevice(backend=backend).get_lock_in_demodulator(3)
                self.assertEqual(state.phase_deg, -31.5)
                self.assertEqual(state.phase_register_frequency_hz, 823.)
                self.assertEqual(state.rt_signals, 'R/phi')
                self.assertTrue(state.sync_filter_enabled)
                self.assertEqual(state.low_pass.cutoff_frequency_hz, 30.)
                self.assertIn(('LockIn.ModPhasFreqGet', [2], ['i']), calls)
                self.assertTrue(all(args == [3] for cmd, args, _ in calls if cmd.startswith('LockIn.Demod')))
                responses['LockIn.DemodPhasRegGet'] = ('i', 0)
                calls.clear()
                with self.assertRaises(NanonisCommandError) as caught:
                    NanonisDevice(backend=backend).get_lock_in_demodulator(3)
                self.assertEqual(caught.exception.acquired_evidence['raw_fields']['phase_register'], (0,))
                self.assertNotIn('LockIn.ModPhasFreqGet', [cmd for cmd, _, _ in calls])


if __name__ == '__main__':
    unittest.main()
