from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from instrument_layer import FakeNanonisBackend, NanonisDevice
from instrument_layer.safety import SafetyGateError
from workflow_layer.feedback_observation import observe_feedback


class FeedbackObservationTests(unittest.TestCase):
    def test_spectroscopy_read_failure_stops_sweep_without_blind_withdraw(self):
        with patch("workflow_layer.feedback_observation.sample_signal_chart", side_effect=OSError("lost read")):
            result=self.observe(observation_mode="spectroscopy")
        self.assertEqual(result['status'],'aborted')
        names=[name for name,_ in self.backend.command_log]
        self.assertIn('BiasSpectr_Stop',names)
        self.assertNotIn('ZCtrl_Withdraw',names)

    def test_spectroscopy_hold_and_changing_bias_are_expected(self):
        from instrument_layer.models import ZControllerStatus
        with patch.object(NanonisDevice,'get_z_controller_status',return_value=ZControllerStatus.HOLD),patch.object(NanonisDevice,'get_bias_v',side_effect=[-.1,.7]):
            result=self.observe(FakeNanonisBackend(scan_running=False,z_controller_on=False),observation_mode='spectroscopy',finished=lambda:self.now>=1.1)
        self.assertEqual(result['status'],'completed')
        self.assertNotIn('ZCtrl_Withdraw',[n for n,_ in self.backend.command_log])

    def observe(self, backend=None, **kw):
        self.backend = backend or FakeNanonisBackend(scan_running=False)
        self.backend.signal_names = ["Current (A)", "X (m)", "Y (m)", "Z (m)"]
        self.backend.signal_values = {0: self.backend.current_a, 1: 0., 2: 0., 3: 0.}
        self.device = NanonisDevice(backend=self.backend)
        self.now = 1.
        def wait(seconds): self.now += seconds
        options = dict(duration_s=.2, interval_s=.02, maximum_sample_gap_s=.1,
                       maximum_abs_current_a=5e-10, z_margin_m=1e-9,
                       clock=lambda: self.now, wait=wait)
        options.update(kw)
        return observe_feedback(self.device, **options)

    def test_direct_finite_observation_needs_no_recorder_and_never_commands_on_success(self):
        result = self.observe()
        self.assertEqual(result["status"], "completed")
        self.assertGreater(len(result["rows"]), 3)
        self.assertEqual(result["source"], "direct_nanonis_signals")
        self.assertEqual(result["tip_quality"], "not_assessed")
        self.assertNotIn("ZCtrl_Withdraw", [name for name, _ in self.backend.command_log])

    def test_overcurrent_withdraws_even_if_scan_stop_or_audit_fails(self):
        class Fault(FakeNanonisBackend):
            def Scan_Action(self, *args):
                raise OSError("scan stop unavailable")
        result = self.observe(Fault(scan_running=False, current_a=2e-9))
        self.assertEqual(result["status"], "aborted")
        self.assertIn("Current exceeded", result["error"])
        self.assertEqual(result["protective_errors"][0]["action"], "stop_scan")
        self.assertIn("ZCtrl_Withdraw", [name for name, _ in self.backend.command_log])

    def test_cancel_has_no_cleanup_and_feedback_off_does_not_force_withdraw(self):
        result = self.observe(stop_requested=lambda: True, stop_scan_at_end=True)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.backend.command_log, [])
        result = self.observe(backend=FakeNanonisBackend(z_controller_on=False))
        self.assertEqual(result['status'], 'aborted')
        self.assertNotIn('ZCtrl_Withdraw', [name for name, _ in self.backend.command_log])

    def test_cancellation_during_signal_read_suppresses_all_cleanup(self):
        from instrument_layer.signal_chart import sample_signal_chart
        cancelled = False
        def read(*args, **kwargs):
            nonlocal cancelled
            sample = sample_signal_chart(*args, **kwargs)
            cancelled = True
            return sample
        with patch('workflow_layer.feedback_observation.sample_signal_chart', side_effect=read):
            report = self.observe(FakeNanonisBackend(current_a=2e-9),
                                  stop_requested=lambda: cancelled, stop_scan_at_end=True)
        self.assertEqual(report['status'], 'cancelled')
        names = [name for name, _ in self.backend.command_log]
        for command in ('ZCtrl_Withdraw', 'Scan_Action', 'AutoApproach_OnOffSet', 'BiasSpectr_Stop'):
            self.assertNotIn(command, names)

    def test_progress_failure_does_not_interrupt_observation_or_trigger_withdraw(self):
        report = self.observe(progress=lambda _: (_ for _ in ()).throw(OSError('disk full')))
        self.assertEqual(report['status'], 'completed')
        self.assertGreater(len(report['rows']), 3)
        self.assertEqual(report['reporting_errors'], ['disk full'])
        self.assertNotIn('ZCtrl_Withdraw', [name for name, _ in self.backend.command_log])
        report = self.observe(FakeNanonisBackend(current_a=2e-9), settling_s=.04,
                              progress=lambda _: (_ for _ in ()).throw(OSError('disk full')))
        self.assertEqual(report['withdraw_reason'], 'sustained_overcurrent')
        self.assertIn('ZCtrl_Withdraw', [name for name, _ in self.backend.command_log])

    def test_gap_or_read_error_aborts(self):
        from instrument_layer.signal_chart import sample_signal_chart
        def delayed(*a, **kw):
            self.now += .2
            return sample_signal_chart(*a, **kw)
        with patch("workflow_layer.feedback_observation.sample_signal_chart", side_effect=delayed):
            result = self.observe()
        self.assertIn("sample gap", result["error"])
        with patch("workflow_layer.feedback_observation.sample_signal_chart", side_effect=OSError("lost link")):
            result = self.observe()
        self.assertEqual(result["status"], "aborted")
        self.assertEqual(result["error"], "lost link")
        self.assertNotIn('ZCtrl_Withdraw', [name for name, _ in self.backend.command_log])

    def test_invalid_policy_rejected_before_any_command(self):
        with self.assertRaises(SafetyGateError):
            self.observe(maximum_abs_current_a=0)
        self.assertEqual(self.backend.command_log, [])

    def test_nonfinite_and_z_endpoint_samples_trigger_protection(self):
        from instrument_layer.signal_chart import sample_signal_chart
        def invalid(device, references, **kw):
            self.backend.signal_values[3] = self.backend.z_high_limit_m
            return sample_signal_chart(device, references, **kw)
        with patch("workflow_layer.feedback_observation.sample_signal_chart", side_effect=invalid):
            result = self.observe()
        self.assertIn("Z reached", result["error"])
        self.assertIn("ZCtrl_Withdraw", [name for name, _ in self.backend.command_log])
        result = self.observe(FakeNanonisBackend(current_a=float("nan")))
        self.assertIn("Invalid direct", result["error"])

    def test_working_point_change_at_final_state_aborts(self):
        with patch.object(NanonisDevice, "get_bias_v", side_effect=[.1, .2]):
            result = self.observe()
        self.assertIn("changed", result["error"])

    def test_end_of_scan_observation_explicitly_stops_scanning(self):
        result = self.observe(stop_scan_at_end=True)
        self.assertEqual(result["status"], "completed")
        self.assertIn("Scan_Action", [name for name, _ in self.backend.command_log])
        self.assertNotIn("ZCtrl_Withdraw", [name for name, _ in self.backend.command_log])

    def test_tcp_latency_does_not_accumulate_in_the_sample_period(self):
        from instrument_layer.signal_chart import sample_signal_chart
        def read(device, references, **kw):
            self.now += .004  # Simulated 4 ms TCP/processing time.
            return sample_signal_chart(device, references, **kw)
        with patch("workflow_layer.feedback_observation.sample_signal_chart", side_effect=read):
            result = self.observe(duration_s=1.)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["rows"]), 50)
        self.assertEqual(result["timing"]["missed_slots"], 0)
        times = [row[2] for row in result["rows"]]
        self.assertTrue(all(abs((b-a)/1e9-.02) < 1e-6 for a,b in zip(times, times[1:])))

    def test_late_read_skips_slots_without_command_bursts(self):
        from instrument_layer.signal_chart import sample_signal_chart
        def read(device, references, **kw):
            self.now += .05
            return sample_signal_chart(device, references, **kw)
        with patch("workflow_layer.feedback_observation.sample_signal_chart", side_effect=read):
            result = self.observe(duration_s=1.)
        self.assertEqual(result["status"], "completed")
        self.assertGreater(result["timing"]["missed_slots"], 0)
        times = [row[2] for row in result["rows"]]
        self.assertTrue(all((b-a)/1e9 >= .05999 for a,b in zip(times, times[1:])))

    def test_settling_keeps_raw_peak_but_excludes_it_from_assessment(self):
        from instrument_layer.signal_chart import sample_signal_chart
        def transient(device, references, **kw):
            self.backend.signal_values[0] = 1.0022e-8 if self.now < 1.1 else 1e-10
            return sample_signal_chart(device, references, **kw)
        with patch("workflow_layer.feedback_observation.sample_signal_chart", side_effect=transient):
            result = self.observe(duration_s=.4, settling_s=.12, overcurrent_hold_s=.08)
        self.assertEqual(result["status"], "completed")
        self.assertGreater(result["current_check"]["maximum_abs_current_a"], 1e-8)
        self.assertAlmostEqual(result["analysis"]["metrics"]["current_median_a"], 1e-10)
        self.assertNotIn("ZCtrl_Withdraw", [n for n,_ in self.backend.command_log])

    def test_persistent_current_aborts_after_settling_and_hold_not_first_peak(self):
        result = self.observe(FakeNanonisBackend(current_a=2e-9), duration_s=.8,
                              settling_s=.2, overcurrent_hold_s=.1)
        self.assertEqual(result["status"], "aborted")
        self.assertGreaterEqual(self.now, 1.3)
        self.assertLess(self.now, 1.35)
        self.assertIn("ZCtrl_Withdraw", [n for n,_ in self.backend.command_log])

    def test_settling_does_not_disable_z_or_communication_checks(self):
        from instrument_layer.signal_chart import sample_signal_chart
        def endpoint(device, references, **kw):
            self.backend.signal_values[3] = self.backend.z_low_limit_m
            return sample_signal_chart(device, references, **kw)
        with patch("workflow_layer.feedback_observation.sample_signal_chart", side_effect=endpoint):
            result = self.observe(FakeNanonisBackend(current_a=2e-9), duration_s=.5,
                settling_s=0., overcurrent_hold_s=0., withdraw_on_overcurrent=0)
        self.assertIn("Z reached", result["error"])

    def test_conditioning_quality_failure_stops_scan_and_preserves_feedback(self):
        result = self.observe(FakeNanonisBackend(current_a=2e-9), duration_s=.8,
            settling_s=.2, overcurrent_hold_s=.1, withdraw_on_overcurrent=0)
        self.assertEqual(result["status"], "not_recovered")
        self.assertIn("Scan_Action", [n for n,_ in self.backend.command_log])
        self.assertNotIn("ZCtrl_Withdraw", [n for n,_ in self.backend.command_log])
        self.assertTrue(self.device.get_z_controller_on())

    def test_window_cannot_be_all_settling_or_unbounded_hold(self):
        with self.assertRaises(ValueError): self.observe(settling_s=.2)
        with self.assertRaises(SafetyGateError): self.observe(overcurrent_hold_s=11.)


if __name__ == "__main__":
    unittest.main()
