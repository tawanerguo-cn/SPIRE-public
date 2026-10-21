from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from instrument_layer import FakeNanonisBackend, NanonisDevice
from instrument_layer.safety import build_control_safety_gate
from workflow_layer.conditioning_burst import run_pulse_burst


class ConditioningBurstTests(unittest.TestCase):
    def setUp(self):
        self.backend=FakeNanonisBackend(scan_running=False)
        self.device=NanonisDevice(backend=self.backend,safety_gate=build_control_safety_gate())
        self.observer=NanonisDevice(backend=FakeNanonisBackend(scan_running=False))
        self.options=dict(count=4,polarity='negative',pulse_voltage_v=10.,duration_ms=100.,
            settling_s=3.,maximum_elapsed_s=60.,continue_on_quality_failure=True,
            observation_policy=dict(duration_s=5.,interval_s=.02,maximum_sample_gap_s=.5,
                maximum_abs_current_a=5e-10,z_margin_m=1e-9,settling_s=3.,
                overcurrent_hold_s=.5,withdraw_on_overcurrent=0))

    def result(self,status):
        return {'result':{'observation':{'status':status,'analysis':{'metrics':{}}}}}

    def run_statuses(self,statuses,**kw):
        with patch('workflow_layer.control_step.execute_control_step',side_effect=[self.result(s) for s in statuses]) as one:
            report=run_pulse_burst(self.device,self.observer,**(self.options|kw))
        return report,one

    def test_explicit_burst_continues_quality_failure_but_not_state_fault(self):
        result,one=self.run_statuses(['not_recovered','completed','aborted'])
        self.assertEqual(one.call_count,3)
        self.assertEqual(result['status'],'state_fault')
        self.assertEqual(result['completed_count'],3)
        self.assertEqual(self.backend.command_log, [])

    def test_successful_burst_never_claims_tip_acceptance_or_withdraws(self):
        result,one=self.run_statuses(['completed']*4)
        self.assertEqual(one.call_count,4)
        self.assertEqual(result['status'],'burst_completed')
        self.assertEqual(result['tip_quality'],'awaiting_imaging_and_spectroscopy')
        self.assertNotIn('ZCtrl_Withdraw',[n for n,_ in self.backend.command_log])
        self.assertTrue(all(c['tip_quality']=='not_accepted' for c in result['quick_checks']))

    def test_quality_stop_and_unresolved_final_window_preserve_observer_decision(self):
        result,one=self.run_statuses(['not_recovered'],continue_on_quality_failure=False)
        self.assertEqual(one.call_count,1)
        self.assertEqual(result['status'],'unresolved_quality_at_burst_end')
        result,one=self.run_statuses(['completed','unsettled'],count=2)
        self.assertEqual(result['status'],'unresolved_quality_at_burst_end')
        self.assertEqual(self.backend.command_log, [])

    def test_unknown_action_is_never_retried(self):
        with patch('workflow_layer.control_step.execute_control_step',side_effect=OSError('ack lost')) as one,self.assertRaisesRegex(OSError,'ack lost'):
            run_pulse_burst(self.device,self.observer,**self.options)
        self.assertEqual(one.call_count,1)
        self.assertEqual(self.backend.command_log, [])

    def test_single_pulse_response_is_reused_and_unsent_pulse_is_not_counted(self):
        sent=self.result('completed')
        response={'peak_abs_current_a':1e-8,'current_rms_ratio':.2,'delta_z_m':-2e-9}
        sent['result']['response']=response
        unsent=self.result('aborted'); unsent['result']['pulse']=None
        with patch('workflow_layer.control_step.execute_control_step',side_effect=[sent,unsent]) as one:
            result=run_pulse_burst(self.device,self.observer,**(self.options|{'baseline_s':.1}))
        self.assertEqual(result['quick_checks'][0]['response'],response)
        self.assertEqual(result['completed_count'],1)
        self.assertEqual(result['status'],'pulse_not_sent')
        self.assertEqual(one.call_args.args[1]['arguments']['baseline_s'],.1)

    def test_cancel_and_budget_never_send_next_pulse(self):
        result,one=self.run_statuses([],stop_requested=lambda:True)
        one.assert_not_called()
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(self.backend.command_log, [])
        result,one=self.run_statuses([],clock=iter([0.,59.,59.]).__next__)
        one.assert_not_called()
        self.assertEqual(result['status'],'burst_time_budget_reached')

    def test_invalid_inputs_and_policy_are_rejected_before_pulse(self):
        for options in ({'count':True},{'count':0},{'polarity':'bad'}, {'duration_ms':10000},
                {'continue_on_quality_failure':1},{'maximum_elapsed_s':1.},
                {'settling_s':float('nan')},{'pulse_voltage_v':float('inf')}):
            with self.subTest(options=options),patch('workflow_layer.control_step.execute_control_step') as one,self.assertRaises((ValueError,RuntimeError)):
                run_pulse_burst(self.device,self.observer,**(self.options|options))
            one.assert_not_called()

    def candidate_options(self):
        from analysis_layer.signal_windows import STABILITY_LIMIT_METRICS
        return dict(stop_on_signal_candidate=True,analysis_options={
            'current_full_scale_a':1e-8,'analysis_limits':{k:1. for k in STABILITY_LIMIT_METRICS}})

    def test_candidate_stops_before_next_pulse_but_completed_alone_does_not(self):
        bad=self.result('completed')
        bad['result']['decision']='reassess_within_explicit_attempt_budget'
        good=self.result('completed')
        good['result']['decision']='candidate_for_next_stage'
        good['result']['assessment']={'status':'stable_candidate','threshold_failures':[]}
        with patch('workflow_layer.control_step.execute_control_step',side_effect=[bad,good]) as one:
            result=run_pulse_burst(self.device,self.observer,**(self.options|self.candidate_options()))
        self.assertEqual(one.call_count,2)
        self.assertEqual(result['status'],'signal_candidate_for_review')
        self.assertEqual(result['tip_quality'],'awaiting_imaging_and_spectroscopy')
        self.assertNotIn('ZCtrl_Withdraw',[n for n,_ in self.backend.command_log])

    def test_adaptive_watchdog_without_candidate_preserves_contact(self):
        result,one=self.run_statuses(['completed']*4,**self.candidate_options())
        self.assertEqual(one.call_count,4)
        self.assertEqual(result['status'],'no_signal_candidate_within_budget')
        self.assertEqual(self.backend.command_log, [])

    def test_candidate_mode_requires_valid_explicit_criteria_before_pulse(self):
        for extra in ({'stop_on_signal_candidate':True},
                      {'stop_on_signal_candidate':True,'analysis_options':{'analysis_limits':{'bad':1},'current_full_scale_a':1e-8}},
                      {'analysis_options':{'current_full_scale_a':float('nan')}}):
            with patch('workflow_layer.control_step.execute_control_step') as one,self.assertRaises(ValueError):
                run_pulse_burst(self.device,self.observer,**(self.options|extra))
            one.assert_not_called()

    def test_invalid_complete_window_stops_instead_of_repeating(self):
        result=self.result('completed')
        result['result']['assessment']={'status':'invalid_data','quality_flags':['constant_both_channels_possible_frozen_data']}
        with patch('workflow_layer.control_step.execute_control_step',return_value=result) as one:
            report=run_pulse_burst(self.device,self.observer,**(self.options|self.candidate_options()))
        self.assertEqual(one.call_count,1)
        self.assertEqual(report['status'],'invalid_assessment_evidence')
        self.assertEqual(self.backend.command_log, [])

    def test_later_failure_retains_previous_pulse_evidence_without_cleanup(self):
        error = OSError('audit disk full')
        error.acquired_evidence = {'original_error': 'retained'}
        with patch('workflow_layer.control_step.execute_control_step', side_effect=[self.result('completed'), error]) as one:
            with self.assertRaises(OSError) as caught:
                run_pulse_burst(self.device, self.observer, **self.options)
        self.assertEqual(one.call_count, 2)
        evidence = caught.exception.acquired_evidence
        self.assertEqual(evidence['original_error'], 'retained')
        self.assertEqual(evidence['pulse_burst']['completed_count'], 1)
        self.assertEqual(evidence['pulse_burst']['status'], 'failed')
        self.assertEqual(self.backend.command_log, [])
