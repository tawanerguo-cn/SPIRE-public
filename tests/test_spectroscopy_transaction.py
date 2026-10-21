from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from instrument_layer import (NanonisDevice,FakeNanonisBackend,BiasSpectroscopyRecipe,
    BiasSpectroscopyTiming,InstrumentPreconditionError,InstrumentPostconditionError,SafetyGateError,NanonisCommandError)
from instrument_layer.models import to_jsonable
from instrument_layer.models import ZControllerStatus
from workflow_layer.control_step import execute_control_step


class SpectroscopyTransactionTests(unittest.TestCase):
    def setUp(self):
        self.recipe=BiasSpectroscopyRecipe(-1,1,1,5,(0,1),BiasSpectroscopyTiming(.05,0,.01,10,.1,.1,.005,.2),restoration_timeout_ms=10)
        self.backend=FakeNanonisBackend(scan_running=False)
        self.device=NanonisDevice(backend=self.backend)

    def test_official_full_rt_index_domain_and_unknown_channel_precheck(self):
        self.backend.signal_names=[f'Signal {i}' for i in range(128)]
        recipe=replace(self.recipe,channel_indexes=(0,86,87))
        result=self.device.acquire_bias_spectrum_transaction(recipe)
        self.assertEqual(result.settings_applied.channel_indexes,(0,86,87))
        with self.assertRaises(SafetyGateError):
            self.device.acquire_bias_spectrum_transaction(replace(recipe,channel_indexes=(128,)))
        self.backend.signal_names=['Current']
        with self.assertRaisesRegex(InstrumentPreconditionError,'unavailable'):
            self.device.acquire_bias_spectrum_transaction(recipe)

    def test_readback_mismatch_prevents_start_without_disturbing_junction(self):
        with patch.object(self.backend,'BiasSpectr_LimitsSet',return_value=()):
            with self.assertRaisesRegex(InstrumentPostconditionError,'readback'):
                self.device.acquire_bias_spectrum_transaction(self.recipe)
        names=[n for n,_ in self.backend.command_log]
        self.assertNotIn('BiasSpectr_Start',names)
        self.assertNotIn('ZCtrl_Withdraw',names)
        self.assertNotIn('BiasSpectr_Stop',names)

    def test_original_setting_error_retains_state_without_motion(self):
        with patch.object(self.backend,'BiasSpectr_PropsSet',return_value=('write rejected',b'',[])):
            with self.assertRaisesRegex(NanonisCommandError,'write rejected') as error:
                self.device.acquire_bias_spectrum_transaction(self.recipe)
        self.assertTrue(error.exception.acquired_evidence['tip_state_on_failure'].z_controller_on)
        self.assertFalse(error.exception.acquired_evidence['start_sent'])
        self.assertNotIn('ZCtrl_Withdraw',[n for n,_ in self.backend.command_log])
        self.assertNotIn('BiasSpectr_Stop',[n for n,_ in self.backend.command_log])

    def test_feedback_off_and_incompatible_inherited_settings_reject_before_writes(self):
        for off,advanced in [(True,(1,1,0,1)),(False,(0,1,0,1)),(False,(1,0,0,1))]:
            b=FakeNanonisBackend(scan_running=False,z_controller_on=not off,bias_spectroscopy_advanced=advanced)
            with self.assertRaises(InstrumentPreconditionError):
                NanonisDevice(backend=b).acquire_bias_spectrum_transaction(self.recipe)
            self.assertFalse(any(n.endswith('Set') or n=='BiasSpectr_Start' for n,_ in b.command_log))

    def test_lost_feedback_after_acquisition_is_not_success_or_retried(self):
        events=[]
        self.device.action_events.set_sink(events.append)
        original=self.backend.BiasSpectr_Start
        def acquire(*args):
            data=original(*args);self.backend.z_controller_on=False;return data
        with patch.object(self.backend,'BiasSpectr_Start',side_effect=acquire):
            with self.assertRaisesRegex(InstrumentPostconditionError,'restore feedback') as error:
                self.device.acquire_bias_spectrum_transaction(self.recipe)
        self.assertTrue(error.exception.acquired_evidence['response_received'])
        self.assertEqual(error.exception.acquired_evidence['raw_response_parts'][4],5)
        failed=[event for event in events if event.phase.value=='failed']
        self.assertEqual(failed[-1].result.value['raw_response_parts'][4],5)
        self.assertEqual(sum(n=='BiasSpectr_Start' for n,_ in self.backend.command_log),1)
        self.assertNotIn('ZCtrl_Withdraw',[n for n,_ in self.backend.command_log])
        self.assertNotIn('BiasSpectr_Stop',[n for n,_ in self.backend.command_log])
        self.assertFalse(error.exception.acquired_evidence['tip_state_on_failure'].z_controller_on)

    def test_uncertain_start_stops_sweep_without_using_error_as_contact_alarm(self):
        def uncertain(*args):
            self.backend.bias_spectroscopy_running=True
            raise OSError('response lost')
        with patch.object(self.backend,'BiasSpectr_Start',side_effect=uncertain),patch.object(self.backend,'BiasSpectr_Stop',side_effect=OSError('stop failed')):
            with self.assertRaisesRegex(OSError,'response lost') as error:
                self.device.acquire_bias_spectrum_transaction(self.recipe)
        self.assertTrue(error.exception.acquired_evidence['start_sent'])
        self.assertFalse(error.exception.acquired_evidence['response_received'])
        self.assertIn('stop failed',str(error.exception.__notes__))
        self.assertNotIn('ZCtrl_Withdraw',[n for n,_ in self.backend.command_log])

    def test_delayed_feedback_restoration_is_read_not_forced_or_remeasured(self):
        before=self.device.get_tip_state_snapshot()
        off=replace(before,z_controller_on=False,z_controller_status=ZControllerStatus.OFF)
        with patch.object(self.device,'get_tip_state_snapshot',side_effect=[before,off,before]):
            result=self.device.acquire_bias_spectrum_transaction(replace(self.recipe,restoration_timeout_ms=1000))
        self.assertTrue(result.tip_state_after.z_controller_on)
        self.assertEqual(sum(n=='BiasSpectr_Start' for n,_ in self.backend.command_log),1)
        self.assertNotIn('ZCtrl_OnOffSet',[n for n,_ in self.backend.command_log])

    def test_nonfinite_inherited_readback_is_evidence_not_invalid_json(self):
        timing=list(self.backend.bias_spectroscopy_timing);timing[3]=float('inf')
        self.backend.bias_spectroscopy_timing=tuple(timing)
        result=self.device.acquire_bias_spectrum_transaction(self.recipe)
        decoded=json.loads(json.dumps(to_jsonable(result),allow_nan=False))
        self.assertEqual(decoded['settings_before']['timing']['maximum_slew_rate_v_per_s'],{'nonfinite_float':'Infinity'})
        self.assertEqual(decoded['settings_applied']['timing']['maximum_slew_rate_v_per_s'],10)
        with self.assertRaises(ValueError):
            self.device.acquire_bias_spectrum_transaction(replace(self.recipe,timing=replace(self.recipe.timing,maximum_slew_rate_v_per_s=float('inf'))))

    def test_control_dispatch_rejects_missing_independent_guard(self):
        with self.assertRaisesRegex(ValueError,'independent'):
            execute_control_step(self.device,{'action':'acquire_bias_spectrum','arguments':asdict(self.recipe),'purpose':'offline dispatch regression'})
        self.assertNotIn('BiasSpectr_Start',[n for n,_ in self.backend.command_log])

if __name__=='__main__':unittest.main()
