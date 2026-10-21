from pathlib import Path
import sys
import unittest
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from instrument_layer import FakeNanonisBackend, NanonisDevice
from instrument_layer.safety import build_control_safety_gate, build_default_safety_gate, SafetyGateError
from workflow_layer.tip_conditioning import pulse_and_assess
from workflow_layer.feedback_observation import observe_feedback


class PulseAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeNanonisBackend(scan_running=False)
        self.device = NanonisDevice(backend=self.backend,
            safety_gate=build_control_safety_gate())
        self.observer = NanonisDevice(backend=FakeNanonisBackend(scan_running=False,
            z_setpoint_a=100e-12, signal_names=['Current (A)', 'X (m)', 'Y (m)', 'Z (m)'],
            signal_values={0:100e-12, 1:0., 2:0., 3:10e-9}))
        self.policy = dict(duration_s=.2, interval_s=.02, maximum_sample_gap_s=.1,
                           maximum_abs_current_a=5e-10, z_margin_m=1e-9)

    def run_once(self, **kw):
        return pulse_and_assess(self.device, self.observer, polarity="negative",
                                observation_policy=self.policy, settling_s=0., **kw)

    def test_one_pulse_and_direct_window_without_an_automatic_retry(self):
        with patch("workflow_layer.tip_conditioning.observe_feedback", wraps=observe_feedback) as observed:
            result = self.run_once()
        pulses = [args for name, args in self.backend.command_log if name == "Bias_Pulse"]
        self.assertEqual(len(pulses), 1)
        self.assertEqual(pulses[0][2], -10.)
        self.assertIn('response',result)
        self.assertEqual(result['observation']['status'],'completed')
        self.assertIn('before',result['observation']['row_phases'])
        self.assertIn('after',result['observation']['row_phases'])
        self.assertEqual(observed.call_args.args, (self.observer,))
        self.assertEqual(observed.call_args.kwargs["settling_s"], 0.)

    def test_settling_and_hold_reach_observer_and_validate_before_pulse(self):
        self.policy.update(duration_s=10., overcurrent_hold_s=.5)
        with patch("workflow_layer.tip_conditioning.observe_feedback", return_value={"status":"aborted", "rows":[]}) as observed:
            pulse_and_assess(self.device, self.observer, polarity="positive",
                observation_policy=self.policy, settling_s=3.)
        self.assertEqual(observed.call_args.kwargs["settling_s"], 3.)
        self.assertEqual(observed.call_args.kwargs["overcurrent_hold_s"], .5)
        self.backend.command_log.clear()
        self.policy["overcurrent_hold_s"] = 10.
        with self.assertRaises(ValueError):
            pulse_and_assess(self.device, self.observer, polarity="positive",
                observation_policy=self.policy, settling_s=3.)
        self.assertNotIn("Bias_Pulse", [n for n,_ in self.backend.command_log])

    def test_bad_policy_or_assessment_rejected_before_pulse(self):
        with self.assertRaises(ValueError):
            self.run_once(analysis_options={"analysis_limits": {"unknown": 1}})
        self.policy["interval_s"] = .2
        with self.assertRaises(ValueError): self.run_once()
        self.assertNotIn("Bias_Pulse", [name for name, _ in self.backend.command_log])

    def test_stop_marker_prevents_pulse(self):
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.run_once(stop_requested=lambda: True)
        self.assertNotIn("Bias_Pulse", [name for name, _ in self.backend.command_log])

    def test_uncertain_pulse_has_one_independent_check_and_keeps_original_error(self):
        with patch.object(self.backend, 'Bias_Pulse', side_effect=OSError('response lost')) as pulse, \
                patch('workflow_layer.tip_conditioning.observe_feedback', wraps=observe_feedback) as observed:
            with self.assertRaisesRegex(OSError, 'response lost') as caught:
                self.run_once()
        pulse.assert_called_once()
        observed.assert_called_once()
        observation=caught.exception.acquired_evidence['post_failure_observation']
        self.assertEqual(observation['pulse_window']['status'],'failed')
        self.assertIn('after',observation['row_phases'])
        self.assertNotIn('ZCtrl_Withdraw', [name for name, _ in self.backend.command_log])

    def test_handoff_during_pulse_stops_existing_observer_without_cleanup(self):
        stopped = False
        def pulse(*args):
            nonlocal stopped
            stopped = True
            raise OSError('response lost during handoff')
        with patch.object(self.backend, 'Bias_Pulse', side_effect=pulse), \
                patch('workflow_layer.tip_conditioning.observe_feedback',wraps=observe_feedback) as observed:
            with self.assertRaises(OSError) as caught: self.run_once(stop_requested=lambda: stopped)
        observed.assert_called_once()
        self.assertEqual(caught.exception.acquired_evidence['post_failure_observation']['status'],'cancelled')
        self.assertNotIn('ZCtrl_Withdraw', [name for name, _ in self.backend.command_log])
        self.assertNotIn('ZCtrl_Withdraw', [name for name, _ in self.observer.backend.command_log])

    def test_captures_rail_noise_change_and_z_change_without_waiting_seconds(self):
        state={'phase':'before','sample':0}
        original_pulse=self.backend.Bias_Pulse
        original_read=self.observer.backend.Signals_ValsGet
        def pulse(*args):
            original_pulse(*args)
            state['phase']='during'
            time.sleep(.1)
            state['phase']='after'
            return ()
        def read(*args):
            state['sample']+=1
            phase=state['phase']; sign=1 if state['sample']%2 else -1
            self.observer.backend.signal_values[0]=(9.99e-9 if phase=='during' else
                100e-12+sign*(10e-12 if phase=='before' else 2e-12))
            self.observer.backend.signal_values[3]=10e-9 if phase!='after' else 8e-9
            return original_read(*args)
        started=time.perf_counter()
        with patch.object(self.backend,'Bias_Pulse',side_effect=pulse), \
                patch.object(self.observer.backend,'Signals_ValsGet',side_effect=read):
            result=self.run_once(analysis_options={'current_full_scale_a':1e-8})
        q=result['response']
        self.assertTrue(q['comparison_valid'])
        self.assertTrue(q['near_full_scale'])
        self.assertGreaterEqual(q['pulse_sample_count'],3)
        self.assertAlmostEqual(q['delta_z_m']*1e9,-2.,places=5)
        self.assertAlmostEqual(q['current_rms_ratio'],.2,delta=.03)
        self.assertLess(time.perf_counter()-started,2.)
        self.assertEqual(result['observation']['status'],'completed')
        self.assertNotIn('ZCtrl_Withdraw',[n for n,_ in self.observer.backend.command_log])

    def test_invalid_baseline_and_observer_fault_never_send_pulse(self):
        with self.assertRaises(ValueError): self.run_once(baseline_s=.001)
        with patch.object(self.observer.backend,'Signals_ValsGet',side_effect=OSError('read lost')):
            result=self.run_once()
        self.assertIsNone(result['pulse'])
        self.assertEqual(result['observation']['status'],'aborted')
        self.assertNotIn('Bias_Pulse',[n for n,_ in self.backend.command_log])

    def test_baseline_does_not_apply_the_existing_post_action_current_policy(self):
        self.observer.backend.signal_values[0]=2e-9
        original=self.backend.Bias_Pulse
        def pulse(*args):
            self.observer.backend.signal_values[0]=100e-12
            return original(*args)
        with patch.object(self.backend,'Bias_Pulse',side_effect=pulse) as sent:
            result=self.run_once()
        sent.assert_called_once()
        self.assertEqual(result['observation']['status'],'completed')
        self.assertEqual(result['response']['current_before_a'],2e-9)
        self.assertNotIn('ZCtrl_Withdraw',[n for n,_ in self.observer.backend.command_log])

    def test_existing_post_action_overcurrent_still_protects(self):
        original=self.backend.Bias_Pulse
        def pulse(*args):
            self.observer.backend.signal_values[0]=2e-9
            return original(*args)
        with patch.object(self.backend,'Bias_Pulse',side_effect=pulse) as sent:
            result=self.run_once()
        sent.assert_called_once()
        self.assertEqual(result['observation']['withdraw_reason'],'sustained_overcurrent')
        self.assertIn('ZCtrl_Withdraw',[n for n,_ in self.observer.backend.command_log])

    def test_shared_bounds_accept_standard_and_gentler_pulses_without_enabling_live(self):
        self.assertTrue(build_default_safety_gate().validate("bias_pulse", {"target_bias_v": 10, "duration_ms": 200}).ok)
        for voltage in (-10., -3., 10.):
            self.assertTrue(self.device.safety_gate.validate("bias_pulse", {"target_bias_v": voltage, "duration_ms": 100}).ok)
        self.assertFalse(self.device.safety_gate.get_action("bias_pulse").live_enabled)
        with self.assertRaises(SafetyGateError): self.run_once(pulse_voltage_v=10.1)
        with self.assertRaises(ValueError): build_control_safety_gate("unknown")


if __name__ == "__main__": unittest.main()
