from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from instrument_layer import (  # noqa: E402
    ActionAuditRequiredError,
    FeedbackPokeRecipe,
    BiasSpectroscopyRecipe,
    BiasSpectroscopyTiming,
    FakeNanonisBackend,
    InstrumentPreconditionError,
    InstrumentPostconditionError,
    NanonisDevice,
    SafetyGateError,
    TipShaperRecipe,
)


def spectroscopy_recipe() -> BiasSpectroscopyRecipe:
    return BiasSpectroscopyRecipe(
        start_bias_v=-0.1,
        end_bias_v=0.1,
        number_of_sweeps=1,
        number_of_points=5,
        channel_indexes=(0, 1),
        timing=BiasSpectroscopyTiming(0.01, 0.0, 0.01, 10.0, 0.001, 0.002, 0.01, 0.01),
    )


def tip_shaper_recipe() -> TipShaperRecipe:
    return TipShaperRecipe(
        switch_off_delay_s=0.1,
        change_bias=True,
        bias_v=0.5,
        tip_lift_m=0.5e-9,
        lift_time_1_s=0.1,
        bias_lift_v=-0.5,
        bias_settling_time_s=0.1,
        lift_height_m=-0.5e-9,
        lift_time_2_s=0.1,
        end_wait_time_s=0.1,
        restore_feedback=True,
    )


class WorkflowInstrumentInterfaceTests(unittest.TestCase):
    def test_read_interfaces_are_typed(self) -> None:
        device = NanonisDevice(backend=FakeNanonisBackend())

        self.assertAlmostEqual(device.get_scan_speed().forward_m_per_s, 100e-9)
        self.assertTrue(device.get_follow_me_speed().custom_speed)
        self.assertEqual(device.get_bias_spectroscopy_properties().number_of_points, 5)
        self.assertEqual(device.get_bias_spectroscopy_channels()[1], ("Bias (V)", "Current (A)"))
        self.assertAlmostEqual(device.get_tip_shaper_properties().tip_lift_m, 0.5e-9)
        self.assertAlmostEqual(device.get_scan_xy_position().x_m, -4.8174e-7)
        self.assertAlmostEqual(device.get_atom_tracking_properties().frequency_hz, 1000.0)

    def test_scan_acquisition_and_atom_tracking_stop_are_fake_testable(self) -> None:
        backend = FakeNanonisBackend(scan_running=False, atom_tracking_status={0: False, 1: True, 2: False})
        device = NanonisDevice(backend=backend)

        scan = device.acquire_scan(0, timeout_ms=1000)
        self.assertFalse(scan.timed_out)
        self.assertEqual((scan.frame_data.rows, scan.frame_data.columns), (2, 2))
        self.assertTrue(device.get_atom_tracking_status(1))

        device.stop_atom_tracking(1)

        self.assertFalse(device.get_atom_tracking_status(1))
        self.assertIn(("AtomTrack_CtrlSet", (1, 0)), backend.command_log)
        self.assertFalse(device.get_atom_tracking_status(0))
        self.assertAlmostEqual(device.get_atom_tracking_properties().frequency_hz, 1000.0)

    def test_start_scan_uses_current_configuration_without_writing_it(self) -> None:
        backend = FakeNanonisBackend(scan_running=False, z_controller_on=True)
        device = NanonisDevice(backend=backend)

        result = device.start_scan(direction_up=False)

        self.assertFalse(result.status_before.running)
        self.assertTrue(result.status_after.running)
        self.assertEqual(result.direction, "down")
        self.assertEqual(result.configured_frame, device.get_scan_frame())
        self.assertIn(("Scan_Action", (0, 0)), backend.command_log)
        write_names = {
            name
            for name, _args in backend.command_log
            if name in {"Scan_FrameSet", "Scan_BufferSet", "Scan_SpeedSet"}
        }
        self.assertEqual(write_names, set())

    def test_start_scan_requires_feedback_and_an_idle_scan(self) -> None:
        controller_off = FakeNanonisBackend(scan_running=False, z_controller_on=False)
        with self.assertRaisesRegex(InstrumentPreconditionError, "Z controller"):
            NanonisDevice(backend=controller_off).start_scan()
        self.assertNotIn("Scan_Action", [name for name, _args in controller_off.command_log])

        already_running = FakeNanonisBackend(scan_running=True, z_controller_on=True)
        with self.assertRaisesRegex(InstrumentPreconditionError, "already running"):
            NanonisDevice(backend=already_running).start_scan()

    def test_start_scan_waits_for_async_status_without_resending_action(self):
        backend = FakeNanonisBackend(scan_running=False, z_controller_on=True)
        with patch.object(backend, 'Scan_StatusGet', side_effect=[(0,), (0,), (0,), (1,)]):
            result = NanonisDevice(backend=backend).start_scan(timeout_ms=1000)
        self.assertTrue(result.status_after.running)
        self.assertEqual([args for name, args in backend.command_log if name == 'Scan_Action'], [(0, 1)])

    def test_scan_start_timeout_stops_once_without_retry(self):
        backend = FakeNanonisBackend(scan_running=False, z_controller_on=True)
        with patch.object(backend, 'Scan_StatusGet', return_value=(0,)):
            with self.assertRaisesRegex(InstrumentPostconditionError, 'timeout'):
                NanonisDevice(backend=backend).start_scan(timeout_ms=10)
        self.assertEqual([args for name, args in backend.command_log if name == 'Scan_Action'], [(0, 1), (1, 1)])

    def test_scan_start_cancel_after_command_stops_without_retry(self):
        backend = FakeNanonisBackend(scan_running=False, z_controller_on=True)
        with patch.object(backend, 'Scan_StatusGet', return_value=(0,)):
            with self.assertRaisesRegex(InstrumentPostconditionError, 'cancelled'):
                NanonisDevice(backend=backend, stop_requested=lambda:backend.scan_running).start_scan()
        self.assertEqual([args for name, args in backend.command_log if name == 'Scan_Action'], [(0, 1), (1, 1)])

    def test_scan_acquisition_and_atom_tracking_stop_are_bounded_or_protective(self) -> None:
        backend = FakeNanonisBackend(scan_running=False, frame_data=[[1.0, 2.0], [3.0, 4.0]])
        device = NanonisDevice(backend=backend)

        result = device.acquire_scan(channel_index=1, timeout_ms=1000)
        device.stop_atom_tracking(2)

        self.assertFalse(result.timed_out)
        self.assertEqual(result.frame_data.data, [[1.0, 2.0], [3.0, 4.0]])
        self.assertEqual(
            [name for name, _args in backend.command_log if name in {"Scan_Action", "Scan_WaitEndOfScan"}],
            ["Scan_Action", "Scan_WaitEndOfScan"],
        )
        self.assertIn(("AtomTrack_CtrlSet", (2, 0)), backend.command_log)

    def test_follow_me_move_is_limited_to_current_scan_frame(self) -> None:
        backend = FakeNanonisBackend(scan_running=False)
        device = NanonisDevice(backend=backend)
        center_x, center_y = backend.scan_frame[:2]

        result = device.move_tip_xy(center_x + 10e-9, center_y - 10e-9)

        self.assertAlmostEqual(result.position_after.x_m, center_x + 10e-9)
        self.assertAlmostEqual(result.position_after.y_m, center_y - 10e-9)
        self.assertIn("FolMe_XYPosSet", [name for name, _args in backend.command_log])

        with self.assertRaises(SafetyGateError):
            device.move_tip_xy(center_x + 100e-9, center_y)

    def test_bias_spectroscopy_configures_official_commands_and_returns_data(self) -> None:
        backend = FakeNanonisBackend(scan_running=False)
        device = NanonisDevice(backend=backend)

        result = device.acquire_bias_spectrum(spectroscopy_recipe())

        self.assertEqual(result.channel_names, ("Bias (V)", "Current (A)"))
        self.assertEqual((result.rows, result.columns), (2, 5))
        self.assertEqual(result.data[0], (-0.1, -0.05, 0.0, 0.05000000000000002, 0.1))
        names = [name for name, _args in backend.command_log]
        control_names = [
            name
            for name in names
            if name
            in {
                "BiasSpectr_PropsSet",
                "BiasSpectr_LimitsSet",
                "BiasSpectr_ChsSet",
                "BiasSpectr_TimingSet",
                "BiasSpectr_Start",
            }
        ]
        self.assertEqual(
            control_names,
            [
                "BiasSpectr_PropsSet",
                "BiasSpectr_LimitsSet",
                "BiasSpectr_ChsSet",
                "BiasSpectr_TimingSet",
                "BiasSpectr_Start",
            ],
        )
        props_args = next(args for name, args in backend.command_log if name == "BiasSpectr_PropsSet")
        self.assertEqual(props_args[-2:], (2, 2), "autosave and save dialog must both be disabled")

    def test_automatic_tip_conditioning_primitives_run_on_fake(self) -> None:
        backend = FakeNanonisBackend(scan_running=False)
        device = NanonisDevice(backend=backend)

        device.apply_bias_pulse(0.5, 10.0)
        device.run_tip_shaper(tip_shaper_recipe())
        backend.z_controller_on = False
        from instrument_layer import ZPokeRecipe

        result = device.run_z_poke(ZPokeRecipe(delta_z_m=-0.5e-9, duration_ms=0.1))

        self.assertAlmostEqual(result.tip_state_after.z_position_m, 128e-9)
        names = [name for name, _args in backend.command_log]
        self.assertIn("Bias_Pulse", names)
        self.assertIn("TipShaper_PropsSet", names)
        self.assertIn("TipShaper_Start", names)
        self.assertIn("ZCtrl_ZPosSet", names)

    def test_feedback_poke_levels_turnaround_bias_and_restores_feedback(self) -> None:
        backend = FakeNanonisBackend(scan_running=False, bias_v=-0.074)
        device = NanonisDevice(backend=backend)

        result = device.run_feedback_poke(
            FeedbackPokeRecipe(
                approach_depth_m=1e-9,
            )
        )

        props = next(args for name, args in backend.command_log if name == "TipShaper_PropsSet")
        self.assertEqual(props[1], 2)
        self.assertAlmostEqual(props[2], -0.074)
        self.assertAlmostEqual(props[5], -0.074)
        self.assertAlmostEqual(props[3], -1e-9)
        self.assertAlmostEqual(props[7], 1e-9)
        self.assertEqual(props[-1], 1)
        self.assertEqual(result.action, "feedback_poke")
        self.assertTrue(result.tip_state_after.z_controller_on)
        self.assertTrue(all(value == -0.074 for value in backend.tip_shaper_bias_trace))
        self.assertAlmostEqual(backend.bias_v, -0.074)

    def test_coarse_xy_has_four_explicit_directions_and_no_generic_z_entry(self) -> None:
        backend = FakeNanonisBackend(
            scan_running=False,
            auto_approach_running=False,
            z_controller_on=False,
            z_status_code=1,
        )
        device = NanonisDevice(backend=backend)

        results = (
            device.move_coarse_x_plus(10),
            device.move_coarse_x_minus(11, group=2),
            device.move_coarse_y_plus(12, group=3),
            device.move_coarse_y_minus(13, group=4),
        )

        commands = [args for name, args in backend.command_log if name == "Motor_StartMove"]
        self.assertEqual(
            commands,
            [(0, 10, 0, 1), (1, 11, 1, 1), (2, 12, 2, 1), (3, 13, 3, 1)],
        )
        self.assertEqual({result.nanonis_direction_code for result in results}, {0, 1, 2, 3})
        self.assertTrue(all(result.waited_until_finished for result in results))
        self.assertFalse(hasattr(device, "move_coarse_z_plus"))
        self.assertFalse(hasattr(device, "move_coarse_z_minus"))

        position = device.get_motor_position(group=2)
        self.assertEqual(position.group, 2)
        self.assertIn(("Motor_PosGet", (1, 500)), backend.command_log)

    def test_controller_write_has_readback_and_auto_approach_has_no_raw_start_setter(self) -> None:
        backend = FakeNanonisBackend(
            scan_running=False,
            auto_approach_running=True,
        )
        device = NanonisDevice(backend=backend)

        self.assertFalse(device.set_z_controller_on(False))
        self.assertTrue(device.set_z_controller_on(True))
        device.stop_auto_approach()

        self.assertIn(("ZCtrl_OnOffSet", (0,)), backend.command_log)
        self.assertIn(("ZCtrl_OnOffSet", (1,)), backend.command_log)
        self.assertIn(("AutoApproach_OnOffSet", (0,)), backend.command_log)
        self.assertFalse(device.get_auto_approach_running())
        self.assertFalse(hasattr(device, "start_auto_approach"))
        with self.assertRaises(TypeError):
            device.set_z_controller_on(1)

    def test_auto_approach_is_one_bounded_waiting_transaction(self) -> None:
        backend = FakeNanonisBackend(
            scan_running=False,
            z_controller_on=False,
            z_status_code=1,
        )
        device = NanonisDevice(backend=backend)

        result = device.run_auto_approach(timeout_ms=1000, poll_interval_ms=10)

        self.assertIn(("AutoApproach_OnOffSet", (1,)), backend.command_log)
        self.assertFalse(device.get_auto_approach_running())
        self.assertTrue(result.tip_state_after.z_controller_on)
        self.assertEqual(result.bounded_parameters["timeout_ms"], 1000.0)

    def test_local_scan_is_contained_and_restores_original_frame(self) -> None:
        original = (10e-9, -20e-9, 100e-9, 80e-9, 30.0)
        backend = FakeNanonisBackend(scan_running=False, scan_frame=original)
        device = NanonisDevice(backend=backend)

        result = device.acquire_local_scan(
            0,
            center_x_fraction=0.25,
            center_y_fraction=-0.25,
            width_fraction=0.25,
            height_fraction=0.5,
            timeout_ms=1000,
        )

        frame_sets = [args for name, args in backend.command_log if name == "Scan_FrameSet"]
        self.assertEqual(len(frame_sets), 2)
        self.assertEqual(tuple(backend.scan_frame), original)
        self.assertAlmostEqual(result.configured_frame.width_m, 25e-9)
        self.assertAlmostEqual(result.configured_frame.height_m, 40e-9)
        self.assertEqual(
            result.restored_frame,
            device.get_scan_frame(),
        )

        with self.assertRaises(InstrumentPreconditionError):
            device.acquire_local_scan(
                0,
                center_x_fraction=0.9,
                width_fraction=0.25,
                timeout_ms=1000,
            )
        self.assertEqual(len([name for name, _args in backend.command_log if name == "Scan_FrameSet"]), 2)

    def test_motor_position_surfaces_the_local_module_error(self) -> None:
        class InaccessibleMotorBackend(FakeNanonisBackend):
            def Motor_PosGet(self, Group: int, Timeout: int):
                self._record("Motor_PosGet", Group, Timeout)
                return (
                    "Cannot access Motor Control Module",
                    b"",
                    [],
                )

        from instrument_layer import NanonisCommandError

        device = NanonisDevice(backend=InaccessibleMotorBackend())
        with self.assertRaises(NanonisCommandError):
            device.get_motor_position()

    def test_coarse_xy_rejects_engaged_tip_or_excessive_steps_before_motor_call(self) -> None:
        backend = FakeNanonisBackend(scan_running=False, z_controller_on=True)
        device = NanonisDevice(backend=backend)

        with self.assertRaises(InstrumentPreconditionError):
            device.move_coarse_x_plus(1)
        with self.assertRaises(SafetyGateError):
            device.move_coarse_y_minus(1001)

        self.assertNotIn("Motor_StartMove", [name for name, _args in backend.command_log])

    def test_write_wrapper_rejects_non_fake_backend_until_live_enabled(self) -> None:
        class LiveLikeBackend:
            def Scan_StatusGet(self):
                return (0,)

            def Scan_FrameGet(self):
                return (0.0, 0.0, 10e-9, 10e-9, 0.0)

            def ZCtrl_StatusGet(self):
                return (2,)

            def ZCtrl_OnOffGet(self):
                return (1,)

            def ZCtrl_ZPosGet(self):
                return (0.0,)

            def ZCtrl_LimitsEnabledGet(self):
                return (1,)

            def ZCtrl_LimitsGet(self):
                return (1e-6, -1e-6)

            def ZCtrl_TipLiftGet(self):
                return (1e-9,)

            def Current_Get(self):
                return (1e-12,)

            def close(self):
                pass

        device = NanonisDevice(backend=LiveLikeBackend())

        with self.assertRaises(ActionAuditRequiredError):
            device.move_tip_xy(0.0, 0.0)


if __name__ == "__main__":
    unittest.main()
