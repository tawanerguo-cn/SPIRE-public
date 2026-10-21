from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from instrument_layer import (BiasSpectroscopyRecipe, BiasSpectroscopyTiming,
    FakeNanonisBackend, InstrumentPostconditionError, NanonisDevice)


class SpectroscopySaveRestorationTests(unittest.TestCase):
    def setUp(self):
        self.recipe = BiasSpectroscopyRecipe(-1, 1, 1, 5, (0, 1),
            BiasSpectroscopyTiming(.05, 0, .01, 10, .1, .1, .005, .2))
        self.backend = FakeNanonisBackend(scan_running=False, bias_spectroscopy_autosave=True)
        self.device = NanonisDevice(backend=self.backend)

    def test_all_inherited_save_flags_restored_but_never_enabled_during_start(self):
        for autosave, dialog in ((True, True), (True, False), (False, True), (False, False)):
            with self.subTest(autosave=autosave, dialog=dialog):
                backend = FakeNanonisBackend(scan_running=False,
                    bias_spectroscopy_autosave=autosave, bias_spectroscopy_show_save_dialog=dialog)
                original = backend.BiasSpectr_Start
                def start(*args):
                    self.assertFalse(backend.bias_spectroscopy_autosave)
                    self.assertFalse(backend.bias_spectroscopy_show_save_dialog)
                    return original(*args)
                with patch.object(backend, 'BiasSpectr_Start', side_effect=start):
                    result = NanonisDevice(backend=backend).acquire_bias_spectrum_transaction(self.recipe)
                self.assertEqual((backend.bias_spectroscopy_autosave, backend.bias_spectroscopy_show_save_dialog), (autosave, dialog))
                self.assertFalse(result.settings_applied.properties.autosave)
                self.assertEqual(result.data.columns, 5)
                self.assertEqual(result.save_settings_restoration['status'], 'restored' if autosave or dialog else 'unchanged')
                self.assertEqual(sum(n == 'BiasSpectr_Start' for n, _ in backend.command_log), 1)

    def test_restore_preserves_applied_sweep_parameters_and_nonzero_z_offset(self):
        recipe = replace(self.recipe, number_of_sweeps=2, backward_sweep=True, save_all=True,
                         timing=replace(self.recipe.timing, z_offset_m=1e-10))
        result = self.device.acquire_bias_spectrum_transaction(recipe)
        after = self.device.get_bias_spectroscopy_settings()
        self.assertEqual(after, replace(result.settings_applied,
            properties=replace(result.settings_applied.properties, autosave=True)))
        self.assertEqual([args for n, args in self.backend.command_log if n == 'BiasSpectr_PropsSet'][-1],
                         (0, 0, 0, 0, 1e-10, 1, 2))

    def test_configuration_failure_restores_flags_and_preserves_original_error(self):
        with patch.object(self.backend, 'BiasSpectr_LimitsSet', side_effect=OSError('configuration lost')):
            with self.assertRaisesRegex(OSError, 'configuration lost') as caught:
                self.device.acquire_bias_spectrum_transaction(self.recipe)
        self.assertTrue(self.backend.bias_spectroscopy_autosave)
        self.assertEqual(caught.exception.acquired_evidence['save_settings_restoration']['status'], 'restored')
        self.assertNotIn('BiasSpectr_Start', [n for n, _ in self.backend.command_log])

    def test_lost_restore_ack_is_not_retried_and_retains_acquired_data(self):
        events = []
        self.device.action_events.set_sink(events.append)
        original = self.backend.BiasSpectr_PropsSet
        def write(*args):
            result = original(*args)
            if args[5] == 1:
                raise OSError('restore response lost')
            return result
        with patch.object(self.backend, 'BiasSpectr_PropsSet', side_effect=write):
            with self.assertRaisesRegex(OSError, 'restore response lost') as caught:
                self.device.acquire_bias_spectrum_transaction(self.recipe)
        evidence = caught.exception.acquired_evidence
        self.assertTrue(evidence['response_received'])
        self.assertEqual(evidence['raw_response_parts'][4], 5)
        self.assertTrue(evidence['save_settings_restoration']['properties_on_failure'].autosave)
        self.assertEqual(sum(n == 'BiasSpectr_PropsSet' for n, _ in self.backend.command_log), 2)
        self.assertTrue([e for e in events if e.phase.value == 'failed'][-1].result.value['response_received'])
        self.assertNotIn('ZCtrl_Withdraw', [n for n, _ in self.backend.command_log])

    def test_readback_mismatch_is_failure_not_false_success(self):
        original = self.backend.BiasSpectr_PropsSet
        def write(*args):
            if args[5] == 1:
                return ()
            return original(*args)
        with patch.object(self.backend, 'BiasSpectr_PropsSet', side_effect=write):
            with self.assertRaisesRegex(InstrumentPostconditionError, 'restoration readback mismatch'):
                self.device.acquire_bias_spectrum_transaction(self.recipe)
        self.assertFalse(self.backend.bias_spectroscopy_autosave)

    def test_uncertain_running_sweep_does_not_enable_saving(self):
        def start(*args):
            self.backend.bias_spectroscopy_running = True
            raise OSError('start response lost')
        with patch.object(self.backend, 'BiasSpectr_Start', side_effect=start), \
             patch.object(self.backend, 'BiasSpectr_Stop', side_effect=OSError('stop failed')):
            with self.assertRaisesRegex(OSError, 'start response lost') as caught:
                self.device.acquire_bias_spectrum_transaction(self.recipe)
        self.assertEqual(caught.exception.acquired_evidence['save_settings_restoration']['status'], 'deferred_sweep_running')
        self.assertEqual(sum(n == 'BiasSpectr_PropsSet' for n, _ in self.backend.command_log), 1)

    def test_takeover_defers_restoration_and_all_automatic_control(self):
        stopped = False
        device = NanonisDevice(backend=self.backend, stop_requested=lambda: stopped)
        original = self.backend.BiasSpectr_Start
        def start(*args):
            nonlocal stopped
            result = original(*args)
            stopped = True
            self.backend.bias_spectroscopy_running = True
            return result
        with patch.object(self.backend, 'BiasSpectr_Start', side_effect=start):
            with self.assertRaises(InstrumentPostconditionError) as caught:
                device.acquire_bias_spectrum_transaction(self.recipe)
        self.assertEqual(caught.exception.acquired_evidence['save_settings_restoration']['status'], 'deferred_operator_takeover')
        names = [n for n, _ in self.backend.command_log]
        self.assertNotIn('BiasSpectr_Stop', names)
        self.assertNotIn('ZCtrl_Withdraw', names)
        self.assertEqual(names.count('BiasSpectr_PropsSet'), 1)

    def test_external_save_preference_change_is_not_overwritten(self):
        original = self.backend.BiasSpectr_Start
        def start(*args):
            result = original(*args)
            self.backend.bias_spectroscopy_show_save_dialog = True
            return result
        with patch.object(self.backend, 'BiasSpectr_Start', side_effect=start):
            with self.assertRaisesRegex(InstrumentPostconditionError, 'changed outside'):
                self.device.acquire_bias_spectrum_transaction(self.recipe)
        self.assertTrue(self.backend.bias_spectroscopy_show_save_dialog)
        self.assertEqual(sum(n == 'BiasSpectr_PropsSet' for n, _ in self.backend.command_log), 1)


if __name__ == '__main__':
    unittest.main()
