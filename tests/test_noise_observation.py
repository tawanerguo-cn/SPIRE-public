from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from instrument_layer import NanonisDevice, FakeNanonisBackend
from instrument_layer.safety import build_control_safety_gate
from workflow_layer.noise_observation import observe_noise

class Clock:
    t=0.
    def __call__(self): return self.t
    def wait(self,v): self.t+=v

class WithdrawalFake(FakeNanonisBackend):
    def ZCtrl_Withdraw(self, *args):
        super().ZCtrl_Withdraw(*args)
        self.z_controller_on = False
        self.signal_values[0] = 0.

class NoiseObservationTests(unittest.TestCase):
    def run_mode(self,mode,*,current=-100e-12,configure=None):
        b=WithdrawalFake(scan_running=False,tip_lift_m=0,z_position_m=0,
            current_a=current,z_setpoint_a=100e-12,signal_names=['Current (A)','X (m)','Y (m)','Z (m)'],
            signal_values={0:current,1:0.,2:0.,3:0.})
        d=NanonisDevice(backend=b,safety_gate=build_control_safety_gate())
        c=Clock()
        if configure is not None:
            configure(b,d,c)
        result=observe_noise(d,mode=mode,duration_s=.2,interval_s=.02,maximum_sample_gap_s=.1,
            maximum_abs_current_a=1e-9,z_margin_m=1e-9,spectrum_interval_s=.05,
            clock=c,utc_ns=lambda:int(c.t*1e9),wait=c.wait)
        return result,b

    def test_closed_only_reads(self):
        r,b=self.run_mode('closed')
        self.assertEqual(r['status'],'completed')
        self.assertFalse(any(n.endswith('Set') or n=='ZCtrl_Withdraw' for n,_ in b.command_log))
        self.assertGreater(len(r['spectra']),1)

    def test_hold_restores_feedback_once(self):
        r,b=self.run_mode('hold')
        self.assertEqual(r['status'],'completed')
        self.assertEqual([v for n,v in b.command_log if n=='ZCtrl_OnOffSet'],[(0,),(1,)])

    def test_hold_abort_withdraws_without_reengaging(self):
        r,b=self.run_mode('hold',current=-2e-9)
        self.assertEqual(r['status'],'aborted')
        self.assertIn('ZCtrl_Withdraw',[n for n,_ in b.command_log])
        self.assertEqual([v for n,v in b.command_log if n=='ZCtrl_OnOffSet'],[(0,)])

    def test_keyboard_interrupt_withdraws_without_restoring_contact(self):
        with patch('workflow_layer.noise_observation.sample_signal_chart', side_effect=KeyboardInterrupt):
            r,b=self.run_mode('hold')
        self.assertEqual(r['status'],'aborted')
        self.assertIn('ZCtrl_Withdraw',[n for n,_ in b.command_log])
        self.assertEqual([v for n,v in b.command_log if n=='ZCtrl_OnOffSet'],[(0,)])

    def test_restore_failure_keeps_typed_withdrawal_and_reports_abort(self):
        original = WithdrawalFake.ZCtrl_OnOffSet
        def switch(backend, enabled):
            if enabled:
                raise OSError('restore acknowledgment lost')
            return original(backend, enabled)
        with patch.object(WithdrawalFake, 'ZCtrl_OnOffSet', switch):
            result, backend = self.run_mode('hold')
        self.assertEqual(result['status'], 'aborted')
        self.assertIn('Feedback restore failed', result['error'])
        self.assertFalse(result['after']['tip']['z_controller_on'])
        self.assertGreaterEqual(sum(name == 'ZCtrl_Withdraw' for name, _ in backend.command_log), 1)

    def test_final_manual_change_does_not_pass(self):
        original=NanonisDevice.get_follow_me_position
        calls=[]
        def position(device):
            value=original(device)
            calls.append(value)
            if len(calls)>=6:
                from dataclasses import replace
                return replace(value,x_m=value.x_m+1e-9)
            return value
        with patch.object(NanonisDevice,'get_follow_me_position',position):
            r,b=self.run_mode('closed')
        self.assertEqual(r['status'],'aborted')
        self.assertIn('state',r['error'])

    def test_manual_withdraw_at_end_of_hold_is_not_undone(self):
        def configure(b,d,c):
            original=c.wait
            def wait(v):
                original(v)
                if c.t >= .19:
                    b.z_position_m = b.z_high_limit_m
                    b.z_controller_on = False
            c.wait=wait
        r,b=self.run_mode('hold',configure=configure)
        self.assertEqual(r['status'],'aborted')
        self.assertEqual([v for n,v in b.command_log if n=='ZCtrl_OnOffSet'],[(0,)])

    def test_restore_rejected_before_typed_cleanup_still_withdraws(self):
        original=NanonisDevice.set_z_controller_on
        def switch(device, enabled):
            if enabled:
                raise RuntimeError('authorization expired before command')
            return original(device, enabled)
        with patch.object(NanonisDevice,'set_z_controller_on',switch):
            r,b=self.run_mode('hold')
        self.assertEqual(r['status'],'aborted')
        self.assertIn('authorization expired',r['error'])
        self.assertIn('ZCtrl_Withdraw',[n for n,_ in b.command_log])

    def test_protective_and_final_read_failures_preserve_samples_and_original_error(self):
        def configure(b,d,c):
            d.withdraw_tip=lambda **kw: (_ for _ in ()).throw(OSError('withdraw unavailable'))
            original=d.get_tip_state_snapshot
            calls=[]
            def snapshot():
                calls.append(1)
                if len(calls)>1: raise OSError('readback unavailable')
                return original()
            d.get_tip_state_snapshot=snapshot
        r,b=self.run_mode('hold',current=-2e-9,configure=configure)
        self.assertEqual(r['status'],'aborted')
        self.assertIn('current ceiling',r['error'])
        self.assertTrue(r['rows'])
        self.assertTrue(r['protective_errors'])
        self.assertIn('readback unavailable',r['final_state_error'])

    def test_scan_or_fft_configuration_change_invalidates_contrast(self):
        for change in ('scan','fft'):
            def configure(b,d,c):
                original=c.wait
                def wait(v):
                    original(v)
                    if c.t >= .19:
                        if change=='scan': b.scan_running=True
                        else: b.spectrum_fft_windows[1]+=1
                c.wait=wait
            with self.subTest(change=change):
                r,b=self.run_mode('closed',configure=configure)
                self.assertEqual(r['status'],'aborted')
                self.assertFalse(any(n.endswith('Set') or n=='ZCtrl_Withdraw' for n,_ in b.command_log))

    def test_withdrawn_stays_retracted(self):
        r,b=self.run_mode('withdrawn')
        self.assertEqual(r['status'],'completed')
        self.assertFalse(r['after']['tip']['z_controller_on'])
        self.assertEqual(sum(n=='ZCtrl_Withdraw' for n,_ in b.command_log),1)

    def test_handoff_during_hold_does_not_restore_or_withdraw(self):
        def configure(b, d, c):
            d._stop_requested = lambda: c.t >= .05
        result, backend = self.run_mode('hold', configure=configure)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual([args for name, args in backend.command_log if name == 'ZCtrl_OnOffSet'], [(0,)])
        self.assertNotIn('ZCtrl_Withdraw', [name for name, _ in backend.command_log])
