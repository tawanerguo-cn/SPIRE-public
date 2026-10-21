from dataclasses import replace
import importlib.util
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from instrument_layer import (ActionName, ActionPhase, CapabilityAccess, FakeNanonisBackend,
    NanonisConnectionConfig, NanonisDevice, OfficialNanonisBackend, build_nanonis_capability_registry,
    find_capability, build_default_safety_gate)
from instrument_layer.nanonis_device import InstrumentPreconditionError, NanonisCommandError
from instrument_layer.safety import SafetyGate, SafetyGateError
from instrument_layer.action_events import ActionAuditRequiredError
from workflow_layer.control_step import execute_control_step


class ZHomeTests(unittest.TestCase):
    def test_absolute_and_relative_home_use_native_command_once_and_audit(self):
        for relative, position, expected in [(False, -100e-9, -100e-9), (True, -20e-9, 108e-9)]:
            backend = FakeNanonisBackend(scan_running=False, home_relative=relative, home_position_m=position)
            events = []
            device = NanonisDevice(backend=backend, action_event_sink=events.append)
            result = execute_control_step(device, {"action": "home_z_controller",
                "arguments": {"maximum_displacement_m": 300e-9}, "purpose": "fake Home"})
            self.assertAlmostEqual(result['result']['requested_target_z_m'], expected)
            self.assertAlmostEqual(backend.z_position_m, expected)
            self.assertEqual(result['result']['motion_completion'], 'awaiting_independent_postcheck')
            names = [name for name, _ in backend.command_log]
            self.assertEqual(names.count('ZCtrl_Home'), 1)
            self.assertNotIn('ZCtrl_OnOffSet', names)
            self.assertNotIn('ZCtrl_Withdraw', names)
            self.assertEqual([e.phase for e in events if e.action_name is ActionName.Z_HOME],
                             [ActionPhase.BEFORE, ActionPhase.AFTER])

    def test_unknown_or_nonfinite_home_properties_fail_before_motion(self):
        backend = FakeNanonisBackend(scan_running=False)
        device = NanonisDevice(backend=backend)
        for response in [(), (2, 0.), (True, 0.), (0, float('nan')), (1, float('inf')), (0, 'bad'), (0, True), (0, 0., 0)]:
            with self.subTest(response=response), patch.object(backend, 'ZCtrl_HomePropsGet', return_value=response):
                with self.assertRaises(NanonisCommandError):
                    device.home_z_controller(maximum_displacement_m=300e-9)
        self.assertNotIn('ZCtrl_Home', [n for n, _ in backend.command_log])

    def test_travel_and_target_bounds_apply_even_when_z_limits_disabled(self):
        for settings in [dict(home_position_m=-100e-9), dict(home_position_m=220e-9),
                         dict(home_relative=True, home_position_m=100e-9)]:
            backend = FakeNanonisBackend(scan_running=False, z_limits_enabled=False, **settings)
            with self.subTest(settings=settings), self.assertRaises(InstrumentPreconditionError):
                NanonisDevice(backend=backend).home_z_controller(maximum_displacement_m=200e-9)
            self.assertNotIn('ZCtrl_Home', [n for n, _ in backend.command_log])

    def test_busy_or_unknown_feedback_refuses_without_stopping_other_processes(self):
        for settings in [dict(scan_running=True), dict(auto_approach_running=True),
                         dict(bias_spectroscopy_running=True), dict(z_status_code=3), dict(z_status_code=0),
                         dict(atom_tracking_status={0: False, 1: True, 2: False})]:
            backend = FakeNanonisBackend(**(dict(scan_running=False) | settings))
            with self.subTest(settings=settings), self.assertRaises(InstrumentPreconditionError):
                NanonisDevice(backend=backend).home_z_controller(maximum_displacement_m=300e-9)
            self.assertNotIn('ZCtrl_Home', [n for n, _ in backend.command_log])
            self.assertNotIn('ZCtrl_Withdraw', [n for n, _ in backend.command_log])
        backend = FakeNanonisBackend(scan_running=True)
        device = NanonisDevice(backend=backend)
        device.pause_scan()
        with self.assertRaises(InstrumentPreconditionError):
            device.home_z_controller(maximum_displacement_m=300e-9)
        self.assertNotIn('ZCtrl_Home', [n for n, _ in backend.command_log])

    def test_home_settings_changed_before_send_are_not_used(self):
        backend = FakeNanonisBackend(scan_running=False)
        with patch.object(backend, 'ZCtrl_HomePropsGet', side_effect=[(0, 0.), (1, -20e-9)]):
            with self.assertRaises(InstrumentPreconditionError):
                NanonisDevice(backend=backend).home_z_controller(maximum_displacement_m=300e-9)
        self.assertNotIn('ZCtrl_Home', [n for n, _ in backend.command_log])

    def test_relative_reference_is_recomputed_without_an_arbitrary_drift_tolerance(self):
        backend = FakeNanonisBackend(scan_running=False, home_relative=True, home_position_m=-20e-9)
        def moved_at_audit(event):
            if event.action_name is ActionName.INSTRUMENT_STATE_SNAPSHOT:
                backend.z_position_m = 150e-9
        result = NanonisDevice(backend=backend, action_event_sink=moved_at_audit).home_z_controller(
            maximum_displacement_m=30e-9)
        self.assertAlmostEqual(result.requested_target_z_m, 130e-9)
        self.assertAlmostEqual(backend.z_position_m, 130e-9)

    def test_cancellation_before_and_after_command_never_adds_cleanup(self):
        for before in (True, False):
            backend = FakeNanonisBackend(scan_running=False)
            stopped = False
            def audit(event):
                nonlocal stopped
                if before and event.action_name is ActionName.INSTRUMENT_STATE_SNAPSHOT: stopped = True
            original = backend.ZCtrl_Home
            def home():
                nonlocal stopped
                value = original(); stopped = True; return value
            with patch.object(backend, 'ZCtrl_Home', side_effect=home):
                with self.assertRaises((InstrumentPreconditionError, RuntimeError)):
                    NanonisDevice(backend=backend, action_event_sink=audit,
                        stop_requested=lambda: stopped).home_z_controller(maximum_displacement_m=300e-9)
            names = [n for n, _ in backend.command_log]
            self.assertEqual(names.count('ZCtrl_Home'), 0 if before else 1)
            self.assertNotIn('ZCtrl_Withdraw', names)
            self.assertNotIn('ZCtrl_OnOffSet', names)

    def test_lost_response_retains_original_evidence_without_retry_or_withdraw(self):
        backend = FakeNanonisBackend(scan_running=False)
        with patch.object(backend, 'ZCtrl_Home', side_effect=OSError('lost acknowledgment')) as command:
            with self.assertRaises(OSError) as caught:
                NanonisDevice(backend=backend).home_z_controller(maximum_displacement_m=300e-9)
            command.assert_called_once_with()
        self.assertTrue(caught.exception.acquired_evidence['command_sent'])
        self.assertFalse(caught.exception.acquired_evidence['response_received'])
        self.assertNotIn('ZCtrl_Withdraw', [n for n, _ in backend.command_log])

    def test_measured_post_home_z_violation_keeps_existing_protection(self):
        backend = FakeNanonisBackend(scan_running=False)
        def violation():
            backend._record('ZCtrl_Home'); backend.z_position_m = 201e-9; return ()
        with patch.object(backend, 'ZCtrl_Home', side_effect=violation), self.assertRaises(InstrumentPreconditionError):
            NanonisDevice(backend=backend).home_z_controller(maximum_displacement_m=300e-9)
        self.assertEqual(sum(n == 'ZCtrl_Withdraw' for n, _ in backend.command_log), 1)

    def test_default_live_disabled_and_audit_required_when_enabled(self):
        class LiveLike:
            def __getattr__(self, name): return getattr(backend, name)
        backend = FakeNanonisBackend(scan_running=False)
        with self.assertRaises(SafetyGateError):
            NanonisDevice(backend=LiveLike()).home_z_controller(maximum_displacement_m=300e-9)
        gate = SafetyGate([replace(s, live_enabled=True) if s.name == 'z_home' else s
                          for s in build_default_safety_gate().list_actions()])
        with self.assertRaises(ActionAuditRequiredError):
            NanonisDevice(backend=LiveLike(), safety_gate=gate).home_z_controller(maximum_displacement_m=300e-9)
        self.assertNotIn('ZCtrl_Home', [n for n, _ in backend.command_log])

    def test_capability_and_cli_route_home_as_bounded_action_for_postcheck(self):
        caps = build_nanonis_capability_registry(['ZCtrl_Home', 'ZCtrl_HomePropsGet'])
        self.assertEqual(find_capability(caps, 'ZCtrl_Home').bounded_action, 'z_home')
        self.assertEqual(find_capability(caps, 'ZCtrl_HomePropsGet').access, CapabilityAccess.READ_ONLY)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
        from nanonis_control import step_outcome
        self.assertEqual(step_outcome('home_z_controller', {'result': {'motion_completion': 'awaiting_independent_postcheck'}}),
                         ('awaiting_independent_postcheck', 2))

    @unittest.skipUnless(importlib.util.find_spec('nanonis_spm'), 'optional official parser, offline')
    def test_official_wire_and_actual_property_parser_need_no_socket(self):
        body = struct.pack('>HfIi', 1, -20e-9, 0, 0)
        with patch('instrument_layer.backends.socket.create_connection', side_effect=AssertionError('offline only')):
            backend = OfficialNanonisBackend(NanonisConnectionConfig(), connection=Mock())
            with patch.object(backend._client, 'send', return_value=body):
                properties = NanonisDevice(backend=backend).get_z_home_properties()
            self.assertTrue(properties.relative)
            self.assertAlmostEqual(properties.position_m, -20e-9)
            with patch.object(backend._client, 'quickSend', return_value=('', b'', [])) as send:
                backend.ZCtrl_Home()
            send.assert_called_once_with('ZCtrl.Home', [], [], [])
