"""Offline fault injection for the motor service and composed relocation transaction."""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from data_layer import RunStore
from data_layer.recording import read_run_event_page
from instrument_layer import (ActionName, ActionPhase, FakeNanonisBackend, NanonisDevice,
                              InstrumentPreconditionError, SafetyGateError, build_default_safety_gate)
from instrument_layer.safety import SafetyGate
from instrument_layer.capabilities import build_nanonis_capability_registry
from runtime_layer.spm_tools import SPMTools
from workflow_layer.coarse_relocation import CoarseRelocationPlan, CoarseRelocationWorkflow


def idle_backend(**values):
    return FakeNanonisBackend(**({"scan_running": False, "z_controller_on": False,
                                 "z_status_code": 1} | values))


def motor_calls(backend):
    return [args for name, args in backend.command_log if name == "Motor_StartMove"]


class CoarseRetractionTests(unittest.TestCase):
    def test_retreat_only_direction_steps_group_and_audit(self):
        backend = idle_backend()
        events = []
        device = NanonisDevice(backend=backend, action_event_sink=events.append)
        result = device.retract_coarse_z(10, group=2)
        self.assertEqual(motor_calls(backend), [(4, 10, 1, 1)])
        self.assertEqual(backend.motor_step_counts[1], [0, 0, 10])
        self.assertEqual(result.direction, "z_retract")
        self.assertIn("not read back", result.completion_evidence)
        self.assertEqual([(e.action_name, e.phase) for e in events], [
            (ActionName.COARSE_Z_RETRACT, ActionPhase.BEFORE),
            (ActionName.COARSE_Z_RETRACT, ActionPhase.AFTER)])
        self.assertFalse(hasattr(device, "move_coarse_z_minus"))
        with self.assertRaises(TypeError):
            device.retract_coarse_z(10, direction=-1)

    def test_retreat_bounds_never_accept_other_directions_or_invalid_steps(self):
        gate = build_default_safety_gate()
        for code in (0, 1, 2, 3, 5):
            with self.subTest(code=code):
                self.assertFalse(gate.validate("coarse_z_retract", {
                    "number_of_steps": 10, "group": 1, "direction_code": code}).ok)
        for steps in (-10, 0, 11, 1.5, True, float("inf"), float("nan")):
            with self.subTest(steps=steps):
                backend = idle_backend()
                with self.assertRaises(SafetyGateError):
                    NanonisDevice(backend=backend).retract_coarse_z(steps)
                self.assertEqual(motor_calls(backend), [])

    def test_xy_and_retreat_share_process_state_checks(self):
        for kwargs in ({"scan_running": True}, {"bias_spectroscopy_running": True},
                       {"auto_approach_running": True}, {"z_controller_on": True},
                       {"z_status_code": 5}, {"z_status_code": 99}):
            for method in ("retract_coarse_z", "move_coarse_x_plus"):
                with self.subTest(kwargs=kwargs, method=method):
                    backend = idle_backend(**kwargs)
                    with self.assertRaises(InstrumentPreconditionError):
                        getattr(NanonisDevice(backend=backend), method)(1)
                    self.assertEqual(motor_calls(backend), [])

    def test_motor_acknowledgement_does_not_override_changed_state(self):
        class ChangedState(FakeNanonisBackend):
            def Motor_StartMove(self, *args):
                super().Motor_StartMove(*args)
                self.bias_spectroscopy_running = True
                return ()
        backend = ChangedState(scan_running=False, z_controller_on=False, z_status_code=1)
        with self.assertRaises(InstrumentPreconditionError):
            NanonisDevice(backend=backend).retract_coarse_z(10)
        self.assertIn("Motor_StopMove", [name for name, _ in backend.command_log])

    def test_live_like_backend_rejected_before_any_command(self):
        class NoCommands:
            def __getattr__(self, name):
                raise AssertionError(f"unexpected hardware call: {name}")
        device = NanonisDevice(backend=NoCommands(), action_event_sink=lambda event: None)
        with self.assertRaises(SafetyGateError):
            device.retract_coarse_z(10)
        self.assertFalse(device.validate_coarse_z_retract(10, require_live_enabled=True).ok)

    def test_capability_mapping_requires_both_registered_motor_actions(self):
        gate = build_default_safety_gate()
        with self.assertRaisesRegex(ValueError, "unregistered"):
            build_nanonis_capability_registry(["Motor_StartMove"], safety_gate=SafetyGate([
                action for action in gate.list_actions() if action.name != "coarse_z_retract"]))


class CoarseRelocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore.create(self.temp.name, signal_catalog={}, requested_period_ns=0)
        self.addCleanup(self.store.close)
        self.plan = CoarseRelocationPlan("y_minus", 10, 200, 2)

    def events(self):
        return read_run_event_page(self.store.database_path)["events"]

    def test_order_and_fresh_state_evidence_never_claims_tip_repair(self):
        backend = FakeNanonisBackend(scan_running=False)
        sentinel = lambda event: None
        device = NanonisDevice(backend=backend, action_event_sink=sentinel)
        result = CoarseRelocationWorkflow(device).run(self.plan, run_store=self.store)
        self.assertEqual(motor_calls(backend), [(4, 10, 1, 1), (3, 200, 1, 1)])
        writes = [(name, args) for name, args in backend.command_log
                  if name in {"ZCtrl_OnOffSet", "Motor_StartMove", "AutoApproach_OnOffSet"}]
        self.assertEqual([name for name, _ in writes], ["ZCtrl_OnOffSet", "Motor_StartMove",
                                                      "Motor_StartMove", "AutoApproach_OnOffSet"])
        self.assertEqual(result["status"], "awaiting_postcheck")
        self.assertEqual(result["tip_quality"], "not_assessed")
        self.assertFalse(result["physical_clearance_verified"])
        self.assertIs(device.action_events.sink, sentinel)
        self.assertGreater(self.store.action_event_count(), 0)
        self.assertEqual(self.events()[-1]["event_type"], "coarse_relocation_awaiting_postcheck")

    def test_all_parameters_validate_before_first_write(self):
        for plan in (replace(self.plan, xy_steps=1001), replace(self.plan, group=0),
                     replace(self.plan, retract_steps=-10), replace(self.plan, approach_timeout_ms=1)):
            with self.subTest(plan=plan):
                backend = FakeNanonisBackend(scan_running=False)
                with self.assertRaises(SafetyGateError):
                    CoarseRelocationWorkflow(NanonisDevice(backend=backend)).run(plan, run_store=self.store)
                self.assertEqual(backend.command_log, [])
                self.assertFalse(self.events()[-1]["payload"]["movement_may_have_occurred"])
        with self.assertRaises(ValueError):
            replace(self.plan, xy_direction="z_minus")

    def test_safe_tip_state_is_not_cleared_to_start_relocation(self):
        backend = FakeNanonisBackend(scan_running=False, z_status_code=5)
        with self.assertRaisesRegex(RuntimeError, "stable"):
            CoarseRelocationWorkflow(NanonisDevice(backend=backend)).run(self.plan, run_store=self.store)
        self.assertNotIn("ZCtrl_OnOffSet", [name for name, _ in backend.command_log])

    def test_uncertain_retreat_or_xy_never_retried_or_followed_by_approach(self):
        for failed_direction, expected in ((4, [4]), (3, [4, 3])):
            with self.subTest(direction=failed_direction):
                class LostAcknowledgement(FakeNanonisBackend):
                    def Motor_StartMove(self, *args):
                        super().Motor_StartMove(*args)
                        if args[0] == failed_direction:
                            raise TimeoutError("response lost after possible movement")
                        return ()
                backend = LostAcknowledgement(scan_running=False)
                with self.assertRaises(TimeoutError):
                    CoarseRelocationWorkflow(NanonisDevice(backend=backend)).run(self.plan, run_store=self.store)
                self.assertEqual([args[0] for args in motor_calls(backend)], expected)
                self.assertNotIn(("AutoApproach_OnOffSet", (1,)), backend.command_log)
                self.assertNotIn(("ZCtrl_OnOffSet", (1,)), backend.command_log)
                self.assertEqual(self.events()[-1]["event_type"], "coarse_relocation_failed")

    def test_all_stops_attempted_even_if_motor_stop_fails(self):
        class Faults(FakeNanonisBackend):
            def Motor_StartMove(self, *args):
                super().Motor_StartMove(*args)
                raise TimeoutError("motor reply lost")
            def Motor_StopMove(self):
                super().Motor_StopMove()
                raise RuntimeError("motor stop failed")
        backend = Faults(scan_running=False)
        with self.assertRaisesRegex(TimeoutError, "reply lost"):
            CoarseRelocationWorkflow(NanonisDevice(backend=backend)).run(self.plan, run_store=self.store)
        names = [name for name, _ in backend.command_log]
        for name in ("AutoApproach_OnOffSet", "Scan_Action", "FolMe_Stop", "BiasSpectr_Stop"):
            self.assertIn(name, names)
        self.assertTrue(self.events()[-1]["payload"]["stop_errors"])

    def test_failed_approach_does_not_produce_success_event(self):
        backend = FakeNanonisBackend(scan_running=False, auto_approach_complete_on_start=False)
        device = NanonisDevice(backend=backend)
        # Use the real timeout path without waiting for wall-clock time.
        # Feedback switching starts its own bounded readback clock first.
        with patch("instrument_layer.nanonis_device.time.perf_counter", side_effect=[0.0, 0.0, 1000.0]):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                CoarseRelocationWorkflow(device).run(self.plan, run_store=self.store)
        self.assertFalse(backend.auto_approach_running)
        self.assertEqual(self.events()[-1]["payload"]["stage"], "auto_approach")

    def test_workflow_rejects_non_fake_even_with_enabled_gate(self):
        device = NanonisDevice(backend=object(), safety_gate=SafetyGate([
            replace(action, live_enabled=True) for action in build_default_safety_gate().list_actions()]))
        with self.assertRaisesRegex(RuntimeError, "fake-only"):
            CoarseRelocationWorkflow(device).run(self.plan, run_store=self.store)
        self.assertEqual(self.events(), [])


class InstrumentServiceToolsTests(unittest.TestCase):
    def test_service_catalogue_fake_run_failure_recovery_and_root_policy(self):
        with tempfile.TemporaryDirectory() as root:
            surface = SPMTools(read_roots=[root], output_root=Path(root) / "out")
            with patch("instrument_layer.backends.OfficialNanonisBackend.__init__", side_effect=AssertionError("live")):
                catalogue = surface.describe_instrument_service().data
                self.assertFalse(catalogue["connected_to_hardware"])
                motor = next(c for c in catalogue["capabilities"] if c["nanonis_method"] == "Motor_StartMove")
                self.assertEqual(motor["wrapper_bounded_actions"]["retract_coarse_z"], "coarse_z_retract")
                result = surface.run_fake_coarse_relocation("x_plus", 10, 200, 1)
                self.assertFalse(result.is_error)
                page = surface.inspect_workflow_run(result.data["database_path"]).data
                self.assertEqual(page["status"], "complete")
                self.assertIn("postchecks", page["status_scope"])
                self.assertEqual(surface.inspect_workflow_run(result.data["database_path"], page["next_event_id"]).data["events"], [])
                failed = surface.run_fake_coarse_relocation("x_plus", 10, 1001, 1)
                self.assertTrue(failed.is_error)
                self.assertEqual(surface.inspect_workflow_run(failed.data["database_path"]).data["status"], "failed")
            outside = Path(root) / "unrelated.sqlite"
            outside.touch()
            with self.assertRaises(ValueError):
                surface.inspect_workflow_run(str(outside))
