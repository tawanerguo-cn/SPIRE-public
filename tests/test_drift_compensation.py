from dataclasses import replace
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import struct
import sys
from threading import Event
import unittest
from unittest.mock import patch, PropertyMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from instrument_layer import (
    ActionName, ActionPhase, FakeNanonisBackend, NanonisDevice, SafetyGate,
    SafetyGateError, build_default_safety_gate, build_nanonis_capability_registry, find_capability,
)
from instrument_layer.nanonis_device import (
    InstrumentPreconditionError, InstrumentPostconditionError, NanonisCommandError,
)
from workflow_layer.control_step import execute_control_step


class DriftCompensationTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeNanonisBackend(scan_running=False)
        self.events = []
        self.device = NanonisDevice(backend=self.backend, action_event_sink=self.events.append)
        self.request = dict(enabled=True, vx_m_per_s=23e-12, vy_m_per_s=-19e-12,
                            vz_m_per_s=0., saturation_limit_percent=1.)

    def writes(self):
        return [args for name, args in self.backend.command_log if name == "Piezo_DriftCompSet"]

    def assert_no_unrelated_motion(self):
        names = {name for name, _ in self.backend.command_log}
        self.assertFalse(names.intersection({"Scan_Action", "ZCtrl_Withdraw", "ZCtrl_OnOffSet",
                                             "FolMe_XYPosSet", "Motor_StartMove"}), names)

    def test_complete_read_is_read_only_and_preserves_si_values_and_saturation(self):
        self.backend.drift_compensation = (1, 2e-12, -3e-12, 4e-12, 0, 1, 0, 7.)
        state = self.device.get_drift_compensation()
        self.assertEqual(state.vy_m_per_s, -3e-12)
        self.assertTrue(state.enabled)
        self.assertTrue(state.y_saturated)
        self.assertTrue(state.saturated)
        self.assertEqual(state.saturation_limit_percent, 7.)
        self.assertEqual(self.writes(), [])

    def test_bad_layout_enum_numbers_and_vendor_error_fail_explicitly(self):
        good = list(self.backend.drift_compensation)
        cases = [good[:-2], good + [0]]
        for index, value in [(0, 2), (4, .5), (5, -1), (6, "0"), (1, float("nan")),
                             (2, float("inf")), (3, True), (7, 0), (7, 101)]:
            bad = good.copy()
            bad[index] = value
            cases.append(bad)
        for response in cases:
            with self.subTest(response=response), patch.object(self.backend, "Piezo_DriftCompGet", return_value=response):
                with self.assertRaises(NanonisCommandError): self.device.get_drift_compensation()
        with patch.object(self.backend, "Piezo_DriftCompGet", return_value=("module unavailable", b"raw", [])):
            with self.assertRaisesRegex(NanonisCommandError, "module unavailable"):
                self.device.get_drift_compensation()
        self.assertEqual(self.writes(), [])

    def test_seven_fields_report_unknown_limit_block_configuration_and_allow_stop(self):
        self.backend.drift_compensation = (1, 0., 0., -1e-12, 0, 0, 0)
        state = self.device.get_drift_compensation()
        self.assertTrue(state.enabled)
        self.assertIsNone(state.saturation_limit_percent)
        self.assertEqual(state.vz_m_per_s, -1e-12)
        with self.assertRaisesRegex(InstrumentPreconditionError, "write protocol is unverified"):
            self.device.set_drift_compensation(**self.request)
        with self.assertRaisesRegex(InstrumentPreconditionError, "cannot set a saturation limit"):
            self.device.stop_drift_compensation(saturation_limit_percent=1.)
        self.assertEqual(self.writes(), [])
        with self.assertRaisesRegex(InstrumentPreconditionError, "feedback OFF"):
            self.device.stop_drift_compensation()
        self.assertEqual(self.writes(), [])
        self.backend.z_controller_on = False
        stopped = self.device.stop_drift_compensation()
        self.assertFalse(stopped.enabled)
        self.assertEqual(stopped.vz_m_per_s, 0.)
        self.assertIsNone(stopped.saturation_limit_percent)
        self.assertEqual(self.writes(), [(2, 0., 0., 0., None)])

    def test_set_enum_readback_and_audit_do_not_start_other_operations(self):
        result = self.device.set_drift_compensation(**self.request)
        self.assertFalse(result.before.enabled)
        self.assertTrue(result.after.enabled)
        self.assertEqual(self.writes(), [(1, 23e-12, -19e-12, 0., 1.)])
        self.device.set_drift_compensation(**(self.request | {"enabled": False}))
        self.assertEqual(self.writes()[-1][0], 2)
        self.assertFalse(self.device.get_drift_compensation().enabled)
        forbidden = {"Scan_Action", "FolMe_XYPosSet", "AtomTrack_CtrlSet", "Scan_FrameSet"}
        self.assertFalse(forbidden.intersection(name for name, _ in self.backend.command_log))
        precheck = next(e for e in self.events if e.source.endswith(".precheck") and e.phase == ActionPhase.AFTER)
        self.assertIn("drift", precheck.result.value)
        self.assertIn("xy", precheck.result.value)
        action = next(e for e in self.events if e.action_name == ActionName.DRIFT_COMPENSATION_SET and e.phase == ActionPhase.AFTER)
        self.assertIn("before", action.result.value)

    def test_protocol_float32_rounding_is_accepted_but_small_real_mismatch_is_not(self):
        original = self.backend.Piezo_DriftCompGet
        def rounded():
            values = list(original())
            for i in (1, 2, 3, 7):
                values[i] = struct.unpack(">f", struct.pack(">f", values[i]))[0]
            return ("", b"raw", values)
        with patch.object(self.backend, "Piezo_DriftCompGet", side_effect=rounded):
            self.device.set_drift_compensation(**self.request)
        setter = self.backend.Piezo_DriftCompSet
        def mismatch(*args):
            result = setter(*args)
            if args[0] == 1:
                v = list(self.backend.drift_compensation)
                v[1] += 1e-15
                self.backend.drift_compensation = tuple(v)
            return result
        with patch.object(self.backend, "Piezo_DriftCompSet", side_effect=mismatch):
            with self.assertRaises(InstrumentPostconditionError):
                self.device.set_drift_compensation(**self.request)
        self.assertEqual(self.writes()[-1], (2, 0., 0., 0., 1.))
        self.assert_no_unrelated_motion()

    def test_invalid_request_or_busy_state_never_writes(self):
        for changes in ({"vx_m_per_s": 1.1e-9}, {"vy_m_per_s": float("nan")},
                        {"vz_m_per_s": 1.1e-10}, {"saturation_limit_percent": 11},
                        {"saturation_limit_percent": 0}, {"vx_m_per_s": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.device.set_drift_compensation(**(self.request | changes))
        self.assertFalse(build_default_safety_gate().validate("drift_compensation_set",
            dict(self.request, enabled=1, vy_m_per_s=float("nan"))).ok)
        with self.assertRaises(TypeError): self.device.set_drift_compensation(**(self.request | {"enabled": 1}))
        for name, value in (("scan_running", True), ("auto_approach_running", True),
                            ("bias_spectroscopy_running", True), ("z_status_code", 3),
                            ("z_status_code", 99), ("z_controller_on", False)):
            with self.subTest(name=name, value=value), patch.object(self.backend, name, value):
                with self.assertRaises(InstrumentPreconditionError): self.device.set_drift_compensation(**self.request)
        with patch.object(self.device, "_paused_scan_context", ("paused",)):
            with self.assertRaises(InstrumentPreconditionError): self.device.set_drift_compensation(**self.request)
        for status in (True, 2):
            with patch.dict(self.backend.atom_tracking_status, {1: status}):
                with self.assertRaises((InstrumentPreconditionError, NanonisCommandError)):
                    self.device.set_drift_compensation(**self.request)
        with patch.object(self.device, "_stop_requested", lambda: True):
            with self.assertRaises(InstrumentPreconditionError): self.device.set_drift_compensation(**self.request)
        self.assertEqual(self.writes(), [])

    def test_saturated_state_is_reported_and_never_automatically_rearmed(self):
        self.backend.drift_compensation = (1, 23e-12, 0., 0., 1, 0, 0, 1.)
        with self.assertRaises(InstrumentPreconditionError): self.device.set_drift_compensation(**self.request)
        self.assertEqual(self.writes(), [])
        stopped = self.device.stop_drift_compensation(saturation_limit_percent=1.)
        self.assertFalse(stopped.enabled)
        self.assertTrue(stopped.x_saturated)  # A historical saturation LED need not clear on OFF.
        self.assertEqual(self.writes(), [(2, 0., 0., 0., 1.)])

    def test_unknown_write_result_gets_one_stop_without_retry_and_retains_original_error(self):
        setter = self.backend.Piezo_DriftCompSet
        def error_after_apply(*args):
            result = setter(*args)
            if args[0] == 1: raise TimeoutError("unknown apply outcome")
            return result
        with patch.object(self.backend, "Piezo_DriftCompSet", side_effect=error_after_apply):
            with self.assertRaisesRegex(TimeoutError, "unknown apply outcome"):
                self.device.set_drift_compensation(**self.request)
        self.assertEqual([args[0] for args in self.writes()], [1, 2])
        self.assertFalse(self.device.get_drift_compensation().enabled)
        self.assertTrue(any(e.phase == ActionPhase.FAILED and e.action_name == ActionName.DRIFT_COMPENSATION_SET for e in self.events))
        self.assert_no_unrelated_motion()

    def test_failed_stop_retains_original_error_and_evidence_without_unrelated_motion(self):
        setter = self.backend.Piezo_DriftCompSet
        original_error = TimeoutError("unknown configuration outcome")
        def error_after_apply(*args):
            setter(*args)
            if args[0] == 1:
                raise original_error
            raise TimeoutError("connection failed")
        with patch.object(self.backend, "Piezo_DriftCompSet", side_effect=error_after_apply):
            with self.assertRaises(TimeoutError) as caught: self.device.set_drift_compensation(**self.request)
        self.assertIs(caught.exception, original_error)
        self.assertEqual([args[0] for args in self.writes()], [1, 2])
        self.assert_no_unrelated_motion()
        self.assertIn("Drift stop could not be confirmed", caught.exception.__notes__[0])
        evidence = caught.exception.acquired_evidence
        self.assertTrue(evidence["write_sent"])
        self.assertFalse(evidence["response_received"])
        self.assertTrue(evidence["recovery_required"])
        self.assertEqual(evidence["cleanup_error"]["message"], "connection failed")
        self.assertFalse(evidence["drift_on_failure"].enabled)
        self.assertIn("tip_state_on_failure", evidence)
        failed = next(e for e in self.events if e.action_name == ActionName.DRIFT_COMPENSATION_SET
                      and e.phase == ActionPhase.FAILED)
        self.assertEqual(failed.error_message, str(original_error))
        self.assertEqual(failed.result.value["cleanup_error"]["message"], "connection failed")

    def test_explicit_stop_is_allowed_when_busy_but_not_after_control_handoff(self):
        self.backend.scan_running = True
        with patch.object(self.device, "_stop_requested", lambda: True):
            with self.assertRaisesRegex(InstrumentPreconditionError, "cancelled"):
                self.device.stop_drift_compensation(saturation_limit_percent=1.)
        self.assertEqual(self.backend.command_log, [])
        state = self.device.stop_drift_compensation(saturation_limit_percent=1.)
        self.assertFalse(state.enabled)
        self.assertEqual(self.backend.command_log[0][0], "Piezo_DriftCompGet")
        self.assertEqual(len(self.writes()), 1)

    def test_handoff_during_write_or_readback_never_sends_cleanup(self):
        for phase in ("write_return", "write_error", "readback"):
            with self.subTest(phase=phase):
                self.setUp()
                cancelled = Event()
                self.device._stop_requested = cancelled.is_set
                setter, reader = self.backend.Piezo_DriftCompSet, self.backend.Piezo_DriftCompGet
                def write(*args):
                    result = setter(*args)
                    if phase != "readback":
                        cancelled.set()
                    if phase == "write_error":
                        raise TimeoutError("operator took control after write")
                    return result
                def read():
                    result = reader()
                    if self.writes():
                        cancelled.set()
                    return result
                with patch.object(self.backend, "Piezo_DriftCompSet", side_effect=write), \
                     patch.object(self.backend, "Piezo_DriftCompGet", side_effect=read):
                    expected = TimeoutError if phase == "write_error" else InstrumentPostconditionError
                    with self.assertRaises(expected) as caught:
                        self.device.set_drift_compensation(**self.request)
                self.assertEqual([args[0] for args in self.writes()], [1])
                self.assert_no_unrelated_motion()
                self.assertTrue(caught.exception.acquired_evidence["drift_on_failure"].enabled)
                self.assertIn("cancelled", caught.exception.acquired_evidence["cleanup_error"]["message"])

    def test_handoff_during_stop_precheck_or_audit_is_rechecked_before_write(self):
        for phase in ("readback", "audit"):
            with self.subTest(phase=phase):
                self.setUp()
                cancelled = Event()
                self.device._stop_requested = cancelled.is_set
                reader = self.backend.Piezo_DriftCompGet
                def read():
                    result = reader()
                    if phase == "readback":
                        cancelled.set()
                    return result
                def audit(event):
                    self.events.append(event)
                    if phase == "audit" and event.source.endswith("stop_drift_compensation.precheck"):
                        cancelled.set()
                self.device.action_events.set_sink(audit)
                with patch.object(self.backend, "Piezo_DriftCompGet", side_effect=read):
                    with self.assertRaisesRegex(InstrumentPreconditionError, "cancelled"):
                        self.device.stop_drift_compensation()
                self.assertEqual(self.writes(), [])
                self.assert_no_unrelated_motion()

    def test_interruption_after_write_does_not_send_automatic_commands(self):
        setter = self.backend.Piezo_DriftCompSet
        def interrupted(*args):
            setter(*args)
            raise KeyboardInterrupt("operator interrupted")
        with patch.object(self.backend, "Piezo_DriftCompSet", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.device.set_drift_compensation(**self.request)
        self.assertEqual([args[0] for args in self.writes()], [1])
        self.assert_no_unrelated_motion()
        self.assertTrue(caught.exception.acquired_evidence["drift_on_failure"].enabled)

    def test_postcheck_faults_do_not_withdraw_but_measured_z_violation_still_does(self):
        for fault in ("feedback_off", "invalid_current", "z_limit", "external_scan"):
            with self.subTest(fault=fault):
                self.setUp()
                setter = self.backend.Piezo_DriftCompSet
                def change_after_write(*args):
                    result = setter(*args)
                    if args[0] == 1:
                        if fault == "feedback_off": self.backend.z_controller_on = False
                        elif fault == "invalid_current": self.backend.current_a = float("nan")
                        elif fault == "z_limit": self.backend.z_position_m = 1.0
                        else: self.backend.scan_running = True
                    return result
                with patch.object(self.backend, "Piezo_DriftCompSet", side_effect=change_after_write):
                    with self.assertRaises((InstrumentPreconditionError, InstrumentPostconditionError)):
                        self.device.set_drift_compensation(**self.request)
                self.assertEqual([args[0] for args in self.writes()], [1, 2])
                names = [name for name, _ in self.backend.command_log]
                self.assertNotIn("Scan_Action", names)
                self.assertEqual(names.count("ZCtrl_Withdraw"), int(fault == "z_limit"))

    def test_failed_failure_readbacks_are_retained_without_masking_original_error(self):
        setter, reader = self.backend.Piezo_DriftCompSet, self.backend.Piezo_DriftCompGet
        original_error = TimeoutError("unknown apply outcome")
        def failed_write(*args):
            setter(*args)
            raise original_error
        def failed_read():
            if self.writes():
                raise ValueError("unparseable drift response")
            return reader()
        with patch.object(self.backend, "Piezo_DriftCompSet", side_effect=failed_write), \
             patch.object(self.backend, "Piezo_DriftCompGet", side_effect=failed_read):
            with self.assertRaises(TimeoutError) as caught:
                self.device.set_drift_compensation(**self.request)
        self.assertIs(caught.exception, original_error)
        self.assertEqual([args[0] for args in self.writes()], [1])
        self.assert_no_unrelated_motion()
        self.assertEqual(caught.exception.acquired_evidence["drift_on_failure_error"]["type"], "ValueError")
        self.assertIn("tip_state_on_failure", caught.exception.acquired_evidence)

    def test_handoff_or_interrupt_during_cleanup_prevents_even_z_limit_withdrawal(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                self.setUp()
                cancelled = Event()
                self.device._stop_requested = cancelled.is_set
                setter, reader = self.backend.Piezo_DriftCompSet, self.backend.Piezo_DriftCompGet
                reads = 0
                def change_after_write(*args):
                    result = setter(*args)
                    self.backend.z_position_m = 1.0
                    return result
                def handoff_on_cleanup():
                    nonlocal reads
                    reads += 1
                    result = reader()
                    if reads == 3:  # Before, after, then stop precheck.
                        if interrupted:
                            raise KeyboardInterrupt("operator interrupted cleanup")
                        cancelled.set()
                    return result
                with patch.object(self.backend, "Piezo_DriftCompSet", side_effect=change_after_write), \
                     patch.object(self.backend, "Piezo_DriftCompGet", side_effect=handoff_on_cleanup):
                    with self.assertRaisesRegex(InstrumentPreconditionError, "outside the enabled Z limits"):
                        self.device.set_drift_compensation(**self.request)
                self.assertEqual([args[0] for args in self.writes()], [1])
                self.assert_no_unrelated_motion()

    def test_stop_preserves_modern_limit_and_does_not_retry_failed_write(self):
        self.backend.drift_compensation = (1, 0., 0., 1e-12, 0, 0, 0, 3.)
        self.assertEqual(self.device.stop_drift_compensation().saturation_limit_percent, 3.)
        with patch.object(self.backend, "Piezo_DriftCompSet", side_effect=TimeoutError("unknown")) as setter:
            with self.assertRaises(TimeoutError): self.device.stop_drift_compensation()
            setter.assert_called_once()

    def test_live_defaults_and_missing_cleanup_permission_reject_before_write(self):
        gate = build_default_safety_gate()
        for name in ("drift_compensation_set", "drift_compensation_stop"):
            self.assertFalse(gate.get_action(name).live_enabled)
        with patch.object(NanonisDevice, "is_fake_backend", new_callable=PropertyMock, return_value=False):
            with self.assertRaises(SafetyGateError): self.device.set_drift_compensation(**self.request)
            with self.assertRaises(SafetyGateError): self.device.stop_drift_compensation(saturation_limit_percent=1.)
            self.device.safety_gate = SafetyGate([replace(a, live_enabled=a.name == "drift_compensation_set") for a in gate.list_actions()])
            with self.assertRaises(SafetyGateError): self.device.set_drift_compensation(**self.request)
        self.assertEqual(self.writes(), [])

    def test_live_writes_require_audit_and_precheck_audit_failure_prevents_motion(self):
        self.device.safety_gate = SafetyGate([replace(a, live_enabled=a.name in {
            "drift_compensation_set", "drift_compensation_stop"}) for a in build_default_safety_gate().list_actions()])
        from instrument_layer import ActionAuditRequiredError
        self.device.action_events.set_sink(None)
        with patch.object(NanonisDevice, "is_fake_backend", new_callable=PropertyMock, return_value=False):
            with self.assertRaises(ActionAuditRequiredError): self.device.set_drift_compensation(**self.request)
            with self.assertRaises(ActionAuditRequiredError): self.device.stop_drift_compensation(saturation_limit_percent=1.)
        def unavailable(event):
            if event.source.endswith(".precheck"): raise OSError("audit unavailable")
        self.device.action_events.set_sink(unavailable)
        with self.assertRaisesRegex(OSError, "audit unavailable"):
            self.device.set_drift_compensation(**self.request)
        self.assertEqual(self.writes(), [])

    def test_cli_read_does_not_require_authorization_or_audit_database(self):
        from tools import nanonis_control
        output = io.StringIO()
        with patch.object(nanonis_control, "NanonisDevice", return_value=self.device), \
             patch.object(nanonis_control, "ExistingRunActionEventSink") as sink, redirect_stdout(output):
            self.assertEqual(nanonis_control.main(["drift-status"]), 0)
        sink.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())["enabled"])
        self.assertEqual([name for name, _ in self.backend.command_log], ["Piezo_DriftCompGet", "close"])

    def test_capability_and_control_step_use_the_shared_wrapper(self):
        registry = build_nanonis_capability_registry(["Piezo_DriftCompGet", "Piezo_DriftCompSet"])
        self.assertTrue(find_capability(registry, "Piezo_DriftCompGet").is_read_only)
        write = find_capability(registry, "Piezo_DriftCompSet")
        self.assertEqual(write.wrapper_bounded_actions["stop_drift_compensation"], "drift_compensation_stop")
        result = execute_control_step(self.device, {"action": "set_drift_compensation",
            "arguments": self.request, "purpose": "Fake drift commissioning"})
        self.assertTrue(result["result"]["after"]["enabled"])
        self.assertEqual(len(self.writes()), 1)


if __name__ == "__main__": unittest.main()
