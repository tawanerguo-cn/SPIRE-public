from __future__ import annotations

from pathlib import Path
import importlib.util
import unittest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from instrument_layer import (  # noqa: E402
    CapabilityAccess,
    CapabilityImplementation,
    CapabilitySafety,
    FakeNanonisBackend,
    NanonisDevice,
    build_nanonis_capability_registry,
    find_capability,
    summarize_capabilities,
)


class CapabilityRegistryTests(unittest.TestCase):
    def test_registry_marks_existing_wrappers_as_implemented(self) -> None:
        capabilities = build_nanonis_capability_registry(
            [
                "Bias_Get",
                "Signals_ValsGet",
                "Scan_Action",
                "ZCtrl_LimitsGet",
                "SpectrumAnlzr_DataGet",
                "AutoApproach_OnOffGet",
                "AutoApproach_OnOffSet",
                "Scan_FrameSet",
            ]
        )

        bias_get = find_capability(capabilities, "Bias_Get")
        signals_vals = find_capability(capabilities, "Signals_ValsGet")
        scan_action = find_capability(capabilities, "Scan_Action")
        z_limits = find_capability(capabilities, "ZCtrl_LimitsGet")
        spectrum_data = find_capability(capabilities, "SpectrumAnlzr_DataGet")
        auto_approach = find_capability(capabilities, "AutoApproach_OnOffGet")
        auto_approach_set = find_capability(capabilities, "AutoApproach_OnOffSet")
        scan_frame_set = find_capability(capabilities, "Scan_FrameSet")

        self.assertIsNotNone(bias_get)
        self.assertEqual(bias_get.implementation, CapabilityImplementation.IMPLEMENTED)
        self.assertEqual(bias_get.access, CapabilityAccess.READ_ONLY)
        self.assertEqual(bias_get.safety, CapabilitySafety.READ_ONLY)
        self.assertEqual(bias_get.wrapper_methods, ("get_bias_v",))

        self.assertIsNotNone(signals_vals)
        self.assertIn("read_signals", signals_vals.wrapper_methods)

        self.assertIsNotNone(scan_action)
        self.assertEqual(scan_action.access, CapabilityAccess.INSTRUMENT_CONTROL)
        self.assertEqual(scan_action.safety, CapabilitySafety.BOUNDED_ACTION)
        self.assertIn("start_scan", scan_action.wrapper_methods)
        self.assertEqual(scan_action.bounded_action, "scan_start_current_frame")
        self.assertEqual(z_limits.implementation, CapabilityImplementation.IMPLEMENTED)
        self.assertEqual(z_limits.access, CapabilityAccess.READ_ONLY)
        self.assertIn("get_tip_state_snapshot", z_limits.wrapper_methods)
        self.assertEqual(spectrum_data.implementation, CapabilityImplementation.IMPLEMENTED)
        self.assertEqual(spectrum_data.wrapper_methods, ("get_spectrum_analyzer_data",))
        self.assertEqual(auto_approach.wrapper_methods, ("get_auto_approach_running",))
        self.assertEqual(auto_approach.safety, CapabilitySafety.READ_ONLY)
        self.assertIn("run_auto_approach", auto_approach_set.wrapper_methods)
        self.assertEqual(auto_approach_set.bounded_action, "auto_approach")
        self.assertEqual(scan_frame_set.wrapper_methods, ("acquire_local_scan", "set_scan_frame"))
        self.assertEqual(scan_frame_set.wrapper_bounded_actions["set_scan_frame"], "scan_frame_set")
        self.assertEqual(scan_frame_set.bounded_action, "local_scan_acquisition")

    def test_registry_marks_typed_bias_set_and_motor_actions_per_wrapper(self) -> None:
        capabilities = build_nanonis_capability_registry(
            [
                "Bias_Set",
                "ZCtrl_SetpntSet",
                "Scan_Save",
                "Pattern_ExpPause",
                "Motor_StartMove",
            ]
        )

        bias_set = find_capability(capabilities, "Bias_Set")
        setpoint_set = find_capability(capabilities, "ZCtrl_SetpntSet")
        scan_save = find_capability(capabilities, "Scan_Save")
        pattern_pause = find_capability(capabilities, "Pattern_ExpPause")
        motor_move = find_capability(capabilities, "Motor_StartMove")

        self.assertEqual(bias_set.implementation, CapabilityImplementation.IMPLEMENTED)
        self.assertEqual(bias_set.access, CapabilityAccess.INSTRUMENT_CONTROL)
        self.assertEqual(bias_set.safety, CapabilitySafety.BOUNDED_ACTION)
        self.assertEqual(bias_set.bounded_action, "bias_set")
        self.assertEqual(
            setpoint_set.implementation,
            CapabilityImplementation.IMPLEMENTED,
        )
        self.assertEqual(
            setpoint_set.bounded_action,
            "z_setpoint_set",
        )

        self.assertEqual(scan_save.access, CapabilityAccess.DATA_WRITE)
        self.assertEqual(scan_save.safety, CapabilitySafety.NOT_EXPOSED_DATA_WRITE)

        self.assertEqual(pattern_pause.access, CapabilityAccess.INSTRUMENT_CONTROL)
        self.assertEqual(motor_move.implementation, CapabilityImplementation.IMPLEMENTED)
        self.assertEqual(motor_move.access, CapabilityAccess.INSTRUMENT_CONTROL)
        self.assertEqual(motor_move.safety, CapabilitySafety.BOUNDED_ACTION)
        self.assertIsNone(motor_move.bounded_action)
        self.assertEqual(motor_move.wrapper_bounded_actions["move_coarse_x_plus"], "coarse_xy_move")
        self.assertEqual(motor_move.wrapper_bounded_actions["retract_coarse_z"], "coarse_z_retract")
        self.assertEqual(
            motor_move.wrapper_methods,
            (
                "move_coarse_x_plus",
                "move_coarse_x_minus",
                "move_coarse_y_plus",
                "move_coarse_y_minus",
                "retract_coarse_z",
            ),
        )
        self.assertIn("Z- (5) has no wrapper", motor_move.notes)

    def test_data_logger_capabilities_use_full_indexes_and_bounded_writes(self) -> None:
        capabilities = build_nanonis_capability_registry(
            [
                "DataLog_ChsGet",
                "DataLog_ChsSet",
                "DataLog_PropsGet",
                "DataLog_PropsSet",
                "DataLog_StatusGet",
                "DataLog_Start",
                "DataLog_Stop",
            ]
        )

        self.assertEqual(find_capability(capabilities, "DataLog_ChsGet").safety, CapabilitySafety.READ_ONLY)
        self.assertEqual(
            find_capability(capabilities, "DataLog_StatusGet").wrapper_methods,
            ("get_data_log_status",),
        )
        self.assertEqual(
            find_capability(capabilities, "DataLog_ChsSet").bounded_action,
            "data_log_configure",
        )
        self.assertEqual(find_capability(capabilities, "DataLog_Start").access, CapabilityAccess.DATA_WRITE)
        self.assertEqual(find_capability(capabilities, "DataLog_Start").bounded_action, "data_log_start")
        self.assertEqual(find_capability(capabilities, "DataLog_Stop").safety, CapabilitySafety.PROTECTIVE_STOP)

    def test_registry_connects_future_actions_to_safety_bounds(self) -> None:
        capabilities = build_nanonis_capability_registry(["Bias_Pulse", "TipShaper_Start", "ZCtrl_ZPosSet"])

        bias_pulse = find_capability(capabilities, "Bias_Pulse")
        tip_shaper = find_capability(capabilities, "TipShaper_Start")
        z_pos_set = find_capability(capabilities, "ZCtrl_ZPosSet")

        self.assertEqual(bias_pulse.safety, CapabilitySafety.BOUNDED_ACTION)
        self.assertEqual(bias_pulse.bounded_action, "bias_pulse")
        self.assertEqual(tip_shaper.bounded_action, "tip_shaper")
        self.assertEqual(z_pos_set.bounded_action, "z_poke")

    def test_workflow_interfaces_are_registered_with_access_and_safety(self) -> None:
        capabilities = build_nanonis_capability_registry(
            [
                "Scan_SpeedGet",
                "Scan_XYPosGet",
                "FolMe_XYPosSet",
                "BiasSpectr_Start",
                "Bias_Pulse",
                "TipShaper_Start",
                "ZCtrl_ZPosSet",
                "Scan_WaitEndOfScan",
                "AtomTrack_StatusGet",
                "AtomTrack_PropsGet",
                "AtomTrack_CtrlSet",
            ]
        )

        self.assertEqual(find_capability(capabilities, "Scan_SpeedGet").access, CapabilityAccess.READ_ONLY)
        self.assertEqual(find_capability(capabilities, "Scan_XYPosGet").access, CapabilityAccess.READ_ONLY)
        self.assertEqual(find_capability(capabilities, "AtomTrack_StatusGet").safety, CapabilitySafety.READ_ONLY)
        self.assertEqual(find_capability(capabilities, "AtomTrack_PropsGet").safety, CapabilitySafety.READ_ONLY)
        self.assertEqual(
            find_capability(capabilities, "AtomTrack_CtrlSet").safety,
            CapabilitySafety.PROTECTIVE_STOP,
        )
        for method, action in (
            ("FolMe_XYPosSet", "follow_me_move"),
            ("BiasSpectr_Start", "bias_spectroscopy"),
            ("Bias_Pulse", "bias_pulse"),
            ("TipShaper_Start", "tip_shaper"),
            ("ZCtrl_ZPosSet", "z_poke"),
            ("Scan_WaitEndOfScan", "scan_acquisition"),
        ):
            capability = find_capability(capabilities, method)
            self.assertEqual(capability.implementation, CapabilityImplementation.IMPLEMENTED)
            self.assertEqual(capability.safety, CapabilitySafety.BOUNDED_ACTION)
            self.assertEqual(capability.bounded_action, action)

    def test_registry_filters_or_includes_public_helpers(self) -> None:
        hidden = build_nanonis_capability_registry(["Bias_Get", "send"], include_helpers=False)
        included = build_nanonis_capability_registry(["Bias_Get", "send"], include_helpers=True)

        self.assertIsNone(find_capability(hidden, "send"))
        helper = find_capability(included, "send")
        self.assertEqual(helper.implementation, CapabilityImplementation.INTERNAL_HELPER)
        self.assertEqual(helper.access, CapabilityAccess.INTERNAL)

    def test_summary_counts_status_dimensions(self) -> None:
        capabilities = build_nanonis_capability_registry(["Bias_Get", "Bias_Set", "Scan_Save"])

        summary = summarize_capabilities(capabilities)

        self.assertEqual(summary.total, 3)
        self.assertEqual(summary.by_implementation["implemented"], 2)
        self.assertEqual(summary.by_access["read_only"], 1)
        self.assertEqual(summary.by_access["instrument_control"], 1)
        self.assertEqual(summary.by_access["data_write"], 1)

    def test_device_exposes_installed_nanonis_capabilities(self) -> None:
        if importlib.util.find_spec("nanonis_spm") is None:
            self.skipTest("nanonis_spm is not installed")
        device = NanonisDevice(backend=FakeNanonisBackend())

        capabilities = device.get_capabilities()

        self.assertGreaterEqual(len(capabilities), 600)
        self.assertIsNotNone(find_capability(capabilities, "Bias_Get"))


if __name__ == "__main__":
    unittest.main()
