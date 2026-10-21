from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import platform
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from instrument_layer import FakeNanonisBackend, NanonisConnectionConfig, NanonisDevice, build_default_safety_gate
from instrument_layer.control_authorization import load_control_authorization
from instrument_layer.nanonis_device import InstrumentPreconditionError, InstrumentPostconditionError, NanonisCommandError
from instrument_layer.models import FeedbackPokeRecipe
from instrument_layer.safety import build_control_safety_gate, SafetyGateError
from workflow_layer.control_step import execute_control_step


class ControlAuthorizationTests(unittest.TestCase):
    def policy(self, **overrides):
        return {"schema_version": 1, "authorization_id": "test", "operator_instruction": "Explicit test authorization",
            "machine": platform.node(), "host": "127.0.0.1", "port": 6503,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "actions": {"bias_pulse": {"duration_ms": [.1, 100]}, "coarse_z_retract": {"group": [1, 1]}}} | overrides

    def load(self, value):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "grant.json"
            path.write_text(json.dumps(value), encoding="utf8")
            return load_control_authorization(path, connection=NanonisConnectionConfig(port=6503))

    def test_local_grant_narrows_bounds_without_changing_defaults_or_opening_minus_z(self):
        gate = self.load(self.policy())
        self.assertTrue(gate.validate("bias_pulse", {"target_bias_v": 2, "duration_ms": 20}, require_live_enabled=True).ok)
        self.assertFalse(gate.validate("bias_pulse", {"target_bias_v": 2, "duration_ms": 101}, require_live_enabled=True).ok)
        self.assertFalse(gate.validate("coarse_z_retract", {"direction_code": 5, "number_of_steps": 1, "group": 1}, require_live_enabled=True).ok)
        self.assertFalse(gate.get_action("z_poke").live_enabled)
        self.assertFalse(build_default_safety_gate().get_action("bias_pulse").live_enabled)
        gate.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertFalse(gate.validate("bias_pulse", {"target_bias_v": 2, "duration_ms": 20}, require_live_enabled=True).ok)

    def test_invalid_authorization_fails_closed(self):
        cases = [self.policy(machine="different-host"), self.policy(port=6502),
            self.policy(expires_at="2000-01-01T00:00:00+00:00"), self.policy(expires_at="2040-01-01T00:00:00"),
            self.policy(actions={"motor_minus_z": {}}),
            self.policy(actions={"coarse_z_retract": {"direction_code": [4, 5]}}),
            self.policy(actions={"bias_pulse": {"target_bias_v": [-10.1, 10.1]}}),
            self.policy(actions={"bias_pulse": {"duration_ms": [True, 100]}}),
            self.policy(actions={"bias_pulse": {"unknown": [0, 1]}})]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(value)

    def test_shared_pulse_bounds_through_grant_and_typed_command(self):
        for profile in (None, "stm_deep_contact"):
            gate = self.load(self.policy(safety_profile=profile, actions={"bias_pulse": {}}))
            for voltage, duration in ((-10, .1), (-3, 10), (10, 200), (-10, 200)):
                with self.subTest(profile=profile, voltage=voltage, duration=duration):
                    self.assertTrue(gate.validate("bias_pulse", {
                        "target_bias_v": voltage, "duration_ms": duration}, require_live_enabled=True).ok)
                    backend = FakeNanonisBackend(scan_running=False)
                    events = []
                    device = NanonisDevice(backend=backend, safety_gate=gate, action_event_sink=events.append)
                    execute_control_step(device, {"action": "apply_bias_pulse", "arguments": {
                        "target_bias_v": voltage, "duration_ms": duration}, "purpose": "pulse boundary regression"})
                    pulses = [args for name, args in backend.command_log if name == "Bias_Pulse"]
                    self.assertEqual(pulses, [(1, duration / 1000., float(voltage), 1, 2)])
                    self.assertEqual(events[0].arguments.value["authorization"]["authorization_id"], "test")
            for voltage, duration in ((10.1, 200), (-10.1, 200), (10, 200.1), (1, .09)):
                with self.subTest(profile=profile, voltage=voltage, duration=duration):
                    backend = FakeNanonisBackend(scan_running=False)
                    device = NanonisDevice(backend=backend, safety_gate=gate)
                    with self.assertRaises(SafetyGateError):
                        device.apply_bias_pulse(voltage, duration)
                    self.assertNotIn("Bias_Pulse", [name for name, _ in backend.command_log])
            self.assertFalse(gate.get_action("tip_shaper").live_enabled)
        for restrictions in ({"target_bias_v": [-10.1, 10]}, {"duration_ms": [.1, 200.1]}):
            with self.subTest(restrictions=restrictions), self.assertRaises(ValueError):
                self.load(self.policy(actions={"bias_pulse": restrictions}))

    def test_retired_and_unknown_profiles_are_not_silent_aliases(self):
        for profile in ("device1_au111_20260920", "device1_au111_deep_contact_20260922", "unknown"):
            with self.subTest(profile=profile), self.assertRaisesRegex(ValueError, "omit safety_profile"):
                self.load(self.policy(safety_profile=profile))

    def test_single_step_audits_authorization_and_rejects_raw_motion(self):
        events = []
        backend = FakeNanonisBackend(scan_running=False)
        device = NanonisDevice(backend=backend, safety_gate=self.load(self.policy()), action_event_sink=events.append)
        result = execute_control_step(device, {"action": "apply_bias_pulse", "arguments": {"target_bias_v": 1, "duration_ms": 10}, "purpose": "one test pulse"})
        self.assertEqual(result["tip_quality"], "awaiting_independent_postcheck")
        self.assertEqual(events[0].arguments.value["authorization"]["authorization_id"], "test")
        self.assertEqual(sum(name == "Bias_Pulse" for name, _ in backend.command_log), 1)
        with self.assertRaises(ValueError):
            execute_control_step(device, {"action": "Motor_StartMove", "arguments": {}, "purpose": "forbidden"})

    def test_deep_contact_trial_requires_explicit_profile_and_stays_bounded(self):
        profile = "stm_deep_contact"
        self.assertFalse(build_control_safety_gate(profile).get_action("tip_shaper").live_enabled)
        for selected, depth, allowed in ((None, 5e-9, False),
                (profile, 5e-9, True), (profile, 10e-9, True),
                (profile, 50e-9, True), (profile, 50.01e-9, False)):
            backend = FakeNanonisBackend(scan_running=False, bias_v=-.1)
            device = NanonisDevice(backend=backend, safety_gate=build_control_safety_gate(selected))
            with self.subTest(profile=selected, depth=depth):
                if allowed:
                    device.run_feedback_poke(FeedbackPokeRecipe(approach_depth_m=depth))
                    props = next(args for name, args in backend.command_log if name == "TipShaper_PropsSet")
                    self.assertEqual((props[1], props[3], props[7], props[-1]), (2, -depth, depth, 1))
                    self.assertEqual(sum(name == "TipShaper_Start" for name, _ in backend.command_log), 1)
                else:
                    with self.assertRaises(SafetyGateError):
                        device.run_feedback_poke(FeedbackPokeRecipe(approach_depth_m=depth))
                    self.assertNotIn("TipShaper_Start", [name for name, _ in backend.command_log])
        for limits in ({"tip_lift_m": [-50.01e-9, 0]}, {"change_bias": [0, 1]}, {"restore_feedback": [0, 1]}):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                self.load(self.policy(safety_profile=profile, actions={"tip_shaper": limits}))

    def test_deep_site_grant_can_limit_current_session_to_ten_nm(self):
        gate = self.load(self.policy(safety_profile="stm_deep_contact",
            actions={"tip_shaper": {"tip_lift_m": [-10e-9, 0], "lift_height_m": [0, 10e-9]},
                     "bias_pulse": {"duration_ms": [.1, 100]}}))
        backend = FakeNanonisBackend(scan_running=False, bias_v=1.)
        events = []
        device = NanonisDevice(backend=backend, safety_gate=gate, action_event_sink=events.append)
        execute_control_step(device, {"action": "apply_bias_pulse",
            "arguments": {"target_bias_v": 10., "duration_ms": 10.}, "purpose": "site standard pulse"})
        execute_control_step(device, {"action": "run_feedback_poke",
            "arguments": {"approach_depth_m": 10e-9}, "purpose": "site deep restructuring"})
        self.assertEqual(sum(name == "Bias_Pulse" for name, _ in backend.command_log), 1)
        self.assertEqual(sum(name == "TipShaper_Start" for name, _ in backend.command_log), 1)
        self.assertNotIn("ZCtrl_Withdraw", [name for name, _ in backend.command_log])
        with self.assertRaises(SafetyGateError):
            device.run_feedback_poke(FeedbackPokeRecipe(approach_depth_m=10.01e-9))
        self.assertEqual(sum(name == "TipShaper_Start" for name, _ in backend.command_log), 1)
        self.assertTrue(events)

    def test_bias_pulse_surfaces_vendor_error_without_blind_withdraw(self):
        backend = FakeNanonisBackend(scan_running=False)
        device = NanonisDevice(backend=backend)
        with patch.object(backend, "Bias_Pulse", return_value=("pulse failed", b"", [])):
            with self.assertRaises(NanonisCommandError):
                device.apply_bias_pulse(1, 10)
        self.assertNotIn("ZCtrl_Withdraw", [name for name, _ in backend.command_log])

    def test_bias_pulse_requires_restored_feedback_and_bias(self):
        for changed in ("hold", "bias"):
            with self.subTest(changed=changed):
                backend = FakeNanonisBackend(scan_running=False)
                device = NanonisDevice(backend=backend)
                original = backend.Bias_Pulse
                def pulse(*args):
                    result = original(*args)
                    if changed == "hold": backend.z_status_code = 3
                    else: backend.bias_v += .1
                    return result
                with patch.object(backend, "Bias_Pulse", side_effect=pulse), self.assertRaises(InstrumentPostconditionError):
                    device.apply_bias_pulse(1, 10)
                self.assertNotIn("ZCtrl_Withdraw", [name for name, _ in backend.command_log])

    def test_hold_is_not_permission_to_condition(self):
        backend = FakeNanonisBackend(scan_running=False, z_status_code=3)
        with self.assertRaises(InstrumentPreconditionError):
            NanonisDevice(backend=backend).apply_bias_pulse(1, 10)
        self.assertNotIn("Bias_Pulse", [name for name, _ in backend.command_log])

    def test_feedback_switch_waits_for_delayed_rt_readback_without_resending(self):
        backend = FakeNanonisBackend(scan_running=False, z_controller_on=False)
        device = NanonisDevice(backend=backend)
        with patch.object(device, "get_z_controller_on", side_effect=[False, False, True]):
            self.assertTrue(device.set_z_controller_on(True))
        self.assertEqual(sum(name == "ZCtrl_OnOffSet" for name, _ in backend.command_log), 1)

    def test_poke_interruption_and_failed_postcheck_do_not_withdraw_or_retry(self):
        for mode in ("interrupt", "hold", "read_error"):
            backend = FakeNanonisBackend(scan_running=False)
            device = NanonisDevice(backend=backend)
            original = backend.TipShaper_Start
            def started(*args):
                result = original(*args)
                if mode == "interrupt": raise KeyboardInterrupt()
                if mode == "hold": backend.z_status_code = 3
                if mode == "read_error":
                    device.get_tip_state_snapshot = lambda: (_ for _ in ()).throw(OSError("status lost"))
                return result
            with self.subTest(mode=mode), patch.object(backend, "TipShaper_Start", side_effect=started):
                with self.assertRaises(BaseException):
                    device.run_feedback_poke(FeedbackPokeRecipe(approach_depth_m=.5e-9))
            names = [name for name, _ in backend.command_log]
            self.assertEqual(names.count("TipShaper_Start"), 1)
            self.assertNotIn("ZCtrl_Withdraw", names)

    def test_feedback_switch_timeout_does_not_withdraw_or_retry(self):
        backend = FakeNanonisBackend(scan_running=False, z_controller_on=False)
        device = NanonisDevice(backend=backend)
        with patch.object(device, "get_z_controller_on", return_value=False):
            with self.assertRaises(InstrumentPostconditionError):
                device.set_z_controller_on(True, timeout_ms=10)
        names = [name for name, _ in backend.command_log]
        self.assertEqual(names.count("ZCtrl_OnOffSet"), 1)
        self.assertNotIn("ZCtrl_Withdraw", names)

    def test_interrupted_approach_stops_without_blind_withdraw(self):
        backend = FakeNanonisBackend(scan_running=False, z_controller_on=False, z_status_code=1)
        device = NanonisDevice(backend=backend)
        with patch.object(device, "get_auto_approach_running", side_effect=[False, KeyboardInterrupt()]):
            with self.assertRaises(KeyboardInterrupt):
                device.run_auto_approach(timeout_ms=1000, poll_interval_ms=10)
        names = [name for name, _ in backend.command_log]
        self.assertIn("AutoApproach_OnOffSet", names)
        self.assertNotIn("ZCtrl_Withdraw", names)
        self.assertIn(('AutoApproach_OnOffSet', (0,)), backend.command_log)

    def test_explicit_withdrawn_approach_completion_does_not_claim_engaged_feedback(self):
        class WithdrawOnLanding(FakeNanonisBackend):
            def AutoApproach_OnOffSet(self, value):
                result = super().AutoApproach_OnOffSet(value)
                if value:
                    self.ZCtrl_Withdraw(1, 1000)
                    self.z_status_code = 1  # Completed withdrawal, not an in-progress transient.
                    self.z_controller_on = False
                    self.z_position_m = self.ZCtrl_LimitsGet()[0]
                return result
        backend = WithdrawOnLanding(scan_running=False, z_controller_on=False, z_status_code=1)
        result = NanonisDevice(backend=backend).run_auto_approach(
            timeout_ms=1000, poll_interval_ms=10, completion_mode="withdrawn")
        self.assertEqual(result.completion_mode, "withdrawn")
        self.assertFalse(result.tip_state_after.z_controller_on)

    def test_cooperative_stop_ends_approach_without_extra_withdraw(self):
        backend = FakeNanonisBackend(scan_running=False, z_controller_on=False, z_status_code=1)
        from itertools import chain, repeat
        requests = chain([False], repeat(True))
        device = NanonisDevice(backend=backend, stop_requested=lambda: next(requests))
        with patch.object(device, "get_auto_approach_running", side_effect=[False, True, False]):
            with self.assertRaisesRegex(InstrumentPostconditionError, "cancelled"):
                device.run_auto_approach(timeout_ms=1000, poll_interval_ms=10)
        self.assertNotIn("ZCtrl_Withdraw", [name for name, _ in backend.command_log])
        self.assertIn(('AutoApproach_OnOffSet', (0,)), backend.command_log)

    def test_conditioning_real_z_violation_still_withdraws_once(self):
        for command in ('Bias_Pulse', 'TipShaper_Start'):
            with self.subTest(command=command):
                backend = FakeNanonisBackend(scan_running=False)
                original = getattr(backend, command)
                def run(*args):
                    result = original(*args)
                    backend.z_position_m = backend.z_high_limit_m + 1e-9
                    return result
                device = NanonisDevice(backend=backend)
                with patch.object(backend, command, side_effect=run), self.assertRaisesRegex(InstrumentPreconditionError, 'outside'):
                    if command == 'Bias_Pulse': device.apply_bias_pulse(1, 10)
                    else: device.run_feedback_poke(FeedbackPokeRecipe(approach_depth_m=.5e-9))
                self.assertEqual(sum(n == 'ZCtrl_Withdraw' for n, _ in backend.command_log), 1)

    def test_invalid_limit_readback_is_not_a_measured_z_excursion(self):
        backend = FakeNanonisBackend(scan_running=False)
        device = NanonisDevice(backend=backend)
        original = backend.Bias_Pulse
        def pulse(*args):
            result = original(*args)
            backend.z_high_limit_m = float('nan')
            return result
        with patch.object(backend, 'Bias_Pulse', side_effect=pulse), self.assertRaisesRegex(InstrumentPreconditionError, 'limits are invalid'):
            device.apply_bias_pulse(1, 10)
        self.assertEqual(sum(n == 'Bias_Pulse' for n, _ in backend.command_log), 1)
        self.assertNotIn('ZCtrl_Withdraw', [n for n, _ in backend.command_log])

    def test_tip_shaper_config_error_or_handoff_never_starts_or_withdraws(self):
        for handoff in (False, True):
            with self.subTest(handoff=handoff):
                backend = FakeNanonisBackend(scan_running=False)
                stopped = False
                def configure(*args):
                    nonlocal stopped
                    stopped = handoff
                    return ('configuration rejected', b'', []) if not handoff else ('', b'', [])
                device = NanonisDevice(backend=backend, stop_requested=lambda: stopped)
                with patch.object(backend, 'TipShaper_PropsSet', side_effect=configure), self.assertRaises((NanonisCommandError, InstrumentPreconditionError)):
                    device.run_feedback_poke(FeedbackPokeRecipe(approach_depth_m=.5e-9))
                names = [n for n, _ in backend.command_log]
                self.assertNotIn('TipShaper_Start', names)
                self.assertNotIn('ZCtrl_Withdraw', names)

    def test_protective_step_survives_failed_snapshot_and_failed_audit(self):
        backend = FakeNanonisBackend(scan_running=False)
        def unavailable(_event): raise OSError("audit unavailable")
        device = NanonisDevice(backend=backend, action_event_sink=unavailable)
        with patch.object(device, "get_tip_state_snapshot", side_effect=OSError("status unavailable")):
            execute_control_step(device, {"action": "withdraw_tip", "arguments": {}, "purpose": "protective stop"})
        self.assertIn("ZCtrl_Withdraw", [name for name, _ in backend.command_log])
