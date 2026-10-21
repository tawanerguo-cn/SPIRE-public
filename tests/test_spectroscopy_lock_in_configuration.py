from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from instrument_layer import FakeNanonisBackend, NanonisDevice, OfficialNanonisBackend, NanonisConnectionConfig
from instrument_layer.nanonis_device import InstrumentPreconditionError, InstrumentPostconditionError, NanonisCommandError
from instrument_layer.safety import build_default_safety_gate
from workflow_layer.control_step import execute_control_step


class SpectroscopyLockInConfigurationTests(unittest.TestCase):
    def test_off_on_uses_no_change_codes_and_retains_other_flags_and_audit(self):
        backend = FakeNanonisBackend(scan_running=False, bias_spectroscopy_advanced=(1, 1, 1, 1))
        events = []
        device = NanonisDevice(backend=backend, action_event_sink=events.append)
        for enabled in (False, True):
            result = execute_control_step(device, {'action': 'set_bias_spectroscopy_lock_in_run',
                'arguments': {'enabled': enabled}, 'purpose': 'offline configuration regression'})
            self.assertEqual(result['result']['after']['lock_in_run'], enabled)
            self.assertEqual(backend.bias_spectroscopy_advanced[:3], (1, 1, 1))
        writes = [(n, a) for n, a in backend.command_log if n.endswith('Set')]
        self.assertEqual(writes, [('BiasSpectr_AdvPropsSet', (0, 0, 0, 2)), ('BiasSpectr_AdvPropsSet', (0, 0, 0, 1))])
        self.assertEqual(sum(e.action_name.value == 'bias_spectroscopy_lock_in_configure' for e in events), 4)

    def test_active_process_or_unsafe_feedback_or_mls_refuses_before_write(self):
        cases = [dict(scan_running=True), dict(auto_approach_running=True),
                 dict(bias_spectroscopy_running=True), dict(z_controller_on=False),
                 dict(bias_spectroscopy_sweep_mode='MLS'), dict(bias_spectroscopy_advanced=(0, 1, 0, 1)),
                 dict(bias_spectroscopy_advanced=(1, 0, 0, 1))]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                backend = FakeNanonisBackend(**(dict(scan_running=False) | overrides))
                with self.assertRaises(InstrumentPreconditionError):
                    NanonisDevice(backend=backend).set_bias_spectroscopy_lock_in_run(False)
                self.assertFalse(any(n.endswith('Set') for n, _ in backend.command_log))

    def test_boolean_and_cancellation_are_checked_before_command(self):
        for value in (0, 1, 'false', None):
            backend = FakeNanonisBackend(scan_running=False)
            with self.assertRaises(ValueError):
                NanonisDevice(backend=backend).set_bias_spectroscopy_lock_in_run(value)
            self.assertFalse(any(n.endswith('Set') for n, _ in backend.command_log))
        backend = FakeNanonisBackend(scan_running=False)
        with self.assertRaises(InstrumentPreconditionError):
            NanonisDevice(backend=backend, stop_requested=lambda: True).set_bias_spectroscopy_lock_in_run(False)
        self.assertNotIn('BiasSpectr_AdvPropsSet', [n for n, _ in backend.command_log])

    def test_readback_mismatch_keeps_evidence_and_does_not_retry_or_withdraw(self):
        backend = FakeNanonisBackend(scan_running=False)
        def changed_other_flag(*args):
            backend.bias_spectroscopy_advanced = (0, 1, 0, 0)
            return ()
        with patch.object(backend, 'BiasSpectr_AdvPropsSet', side_effect=changed_other_flag) as setter:
            with self.assertRaises(InstrumentPostconditionError) as caught:
                NanonisDevice(backend=backend).set_bias_spectroscopy_lock_in_run(False)
            self.assertEqual(setter.call_count, 1)
        self.assertTrue(caught.exception.acquired_evidence['command_sent'])
        self.assertFalse(caught.exception.acquired_evidence['after'].reset_bias)
        self.assertNotIn('ZCtrl_Withdraw', [n for n, _ in backend.command_log])

    def test_lost_ack_is_not_retried_and_preserves_before_configuration(self):
        backend = FakeNanonisBackend(scan_running=False)
        with patch.object(backend, 'BiasSpectr_AdvPropsSet', side_effect=OSError('ack lost')) as setter:
            with self.assertRaises(OSError) as caught:
                NanonisDevice(backend=backend).set_bias_spectroscopy_lock_in_run(False)
            self.assertEqual(setter.call_count, 1)
        self.assertTrue(caught.exception.acquired_evidence['before'].lock_in_run)
        self.assertNotIn('ZCtrl_Withdraw', [n for n, _ in backend.command_log])

    def test_enabled_query_does_not_depend_on_unknown_phase_register(self):
        backend = FakeNanonisBackend(lock_in_phase_register=0)
        device = NanonisDevice(backend=backend)
        self.assertFalse(device.get_lock_in_modulator_enabled())
        self.assertEqual([n for n, _ in backend.command_log], ['LockIn_ModOnOffGet'])
        for value in ((), (2,), (0, 1), ('unsupported', b'', [])):
            with patch.object(backend, 'LockIn_ModOnOffGet', return_value=value), self.assertRaises(NanonisCommandError):
                device.get_lock_in_modulator_enabled()
        for number in (0, True, 1.5):
            with self.assertRaises(ValueError):
                device.get_lock_in_modulator_enabled(number)

    def test_configuration_is_default_live_disabled_and_official_wire_types(self):
        spec = build_default_safety_gate().get_action('bias_spectroscopy_lock_in_configure')
        self.assertFalse(spec.live_enabled)
        self.assertFalse(spec.validate({'enabled': 0}, require_live_enabled=True).ok)
        self.assertFalse(spec.validate({'enabled': 2}).ok)
        with OfficialNanonisBackend(NanonisConnectionConfig(), connection=Mock()) as backend:
            with patch.object(backend._client, 'quickSend', return_value=('', b'', [])) as send:
                backend.BiasSpectr_AdvPropsSet(0, 0, 0, 2)
            send.assert_called_once_with('BiasSpectr.AdvPropsSet', [0, 0, 0, 2], ['H', 'H', 'H', 'H'], [])


if __name__ == '__main__':
    unittest.main()
