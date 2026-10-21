from dataclasses import asdict
from pathlib import Path
import sys
from threading import Event
import time
import unittest
from unittest.mock import patch, PropertyMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from instrument_layer import (NanonisDevice, NanonisConnectionConfig, FakeNanonisBackend,
    BiasSpectroscopyRecipe, BiasSpectroscopyTiming, InstrumentPreconditionError, SafetyGateError)
from workflow_layer.control_step import execute_control_step


class GuardedSpectroscopyTests(unittest.TestCase):
    def setUp(self):
        self.backend=FakeNanonisBackend(scan_running=False)
        self.backend.signal_names=['Current (A)','X (m)','Y (m)','Z (m)']
        self.backend.signal_values={0:1e-10,1:0.,2:0.,3:0.}
        self.events=[]
        self.device=NanonisDevice(backend=self.backend,config=NanonisConnectionConfig(port=6503),action_event_sink=self.events.append)
        self.observer=NanonisDevice(backend=self.backend,config=NanonisConnectionConfig(port=6504,timeout_s=.1))
        self.recipe=BiasSpectroscopyRecipe(-1,1,1,5,(0,1),BiasSpectroscopyTiming(.01,0,.01,10,.01,.01,.01,.01),restoration_timeout_ms=10)
        self.arguments=asdict(self.recipe)|{'observation_policy':dict(duration_s=2,interval_s=.01,
            maximum_sample_gap_s=.1,maximum_abs_current_a=2e-9,z_margin_m=1e-9,
            settling_s=0,overcurrent_hold_s=.03,withdraw_on_overcurrent=1),
            'preparation':{'bias_v':self.backend.bias_v,'setpoint_a':self.backend.z_setpoint_a}}

    def execute(self):
        return execute_control_step(self.device,{'action':'acquire_bias_spectrum',
            'arguments':self.arguments,'purpose':'fake current guard regression'},observer=self.observer)

    def test_success_returns_raw_data_and_independent_samples_with_one_start(self):
        result=self.execute()['result']
        self.assertEqual(result['acquisition']['data']['columns'],5)
        self.assertEqual(result['current_guard']['status'],'completed')
        self.assertGreaterEqual(len(result['current_guard']['rows']),2)
        self.assertNotIn('analysis',result['current_guard'])
        self.assertEqual(sum(n=='BiasSpectr_Start' for n,_ in self.backend.command_log),1)
        self.assertNotIn('ZCtrl_Withdraw',[n for n,_ in self.backend.command_log])
        acquisition_events=[e for e in self.events if e.action_name.value=='bias_spectroscopy_acquire']
        self.assertEqual(acquisition_events[0].arguments.value['observation_ready'],{'callback_supplied':True})

    def test_guard_stops_and_withdraws_while_main_acquisition_is_blocked(self):
        protected=Event()
        original_start=self.backend.BiasSpectr_Start
        original_withdraw=self.backend.ZCtrl_Withdraw
        def blocking(*args):
            self.backend.bias_spectroscopy_running=True
            self.backend.signal_values[0]=1e-8
            self.assertTrue(protected.wait(1),'guard must protect before Start returns')
            return original_start(*args)
        def withdraw(*args):
            result=original_withdraw(*args);protected.set();return result
        with patch.object(self.backend,'BiasSpectr_Start',side_effect=blocking),patch.object(self.backend,'ZCtrl_Withdraw',side_effect=withdraw):
            with self.assertRaises(Exception) as caught:self.execute()
        evidence=caught.exception.acquired_evidence
        self.assertEqual(evidence['current_guard']['status'],'aborted')
        self.assertIn('Current exceeded',evidence['current_guard']['error'])
        names=[n for n,_ in self.backend.command_log]
        self.assertLess(names.index('BiasSpectr_Stop'),names.index('ZCtrl_Withdraw'))
        self.assertNotIn('ZCtrl_OnOffSet',names)

    def test_configuration_error_does_not_withdraw_a_stable_junction(self):
        with patch.object(self.backend,'BiasSpectr_PropsSet',return_value=('configuration rejected',b'',[])):
            with self.assertRaisesRegex(Exception,'configuration rejected') as caught:self.execute()
        self.assertEqual(caught.exception.acquired_evidence['current_guard']['status'],'completed')
        self.assertNotIn('ZCtrl_Withdraw',[n for n,_ in self.backend.command_log])
        self.assertNotIn('BiasSpectr_Start',[n for n,_ in self.backend.command_log])

    def test_preparation_and_independent_connection_rejected_before_start(self):
        self.arguments['preparation']['bias_v']=2.
        with self.assertRaisesRegex(InstrumentPreconditionError,'preparation'):self.execute()
        self.assertNotIn('BiasSpectr_Start',[n for n,_ in self.backend.command_log])
        self.arguments['preparation']['bias_v']=self.backend.bias_v
        self.observer=self.device
        with self.assertRaisesRegex(ValueError,'distinct'):self.execute()

    def test_spectroscopy_has_no_unprotected_settling_or_deferred_overcurrent(self):
        for key,value in [('settling_s',.01),('withdraw_on_overcurrent',0)]:
            old=self.arguments['observation_policy'][key]
            self.arguments['observation_policy'][key]=value
            with self.assertRaises(SafetyGateError):self.execute()
            self.arguments['observation_policy'][key]=old
        self.assertNotIn('BiasSpectr_Start',[n for n,_ in self.backend.command_log])

    def test_live_typed_acquisition_rejects_absent_guard_before_any_io(self):
        with patch.object(self.device,'_require_bounded_action',return_value={}),patch.object(NanonisDevice,'is_fake_backend',new_callable=PropertyMock,return_value=False):
            with self.assertRaisesRegex(InstrumentPreconditionError,'independent active current guard'):
                self.device.acquire_bias_spectrum_transaction(self.recipe)
        self.assertEqual(self.backend.command_log,[])

if __name__=='__main__':unittest.main()
