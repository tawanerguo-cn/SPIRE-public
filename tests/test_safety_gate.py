from __future__ import annotations

from pathlib import Path
import unittest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from instrument_layer import (  # noqa: E402
    FakeNanonisBackend,
    NanonisDevice,
    SafetyGateError,
    build_default_safety_gate,
)


class SafetyGateTests(unittest.TestCase):
    def test_default_control_actions_are_registered_but_not_live_enabled(self) -> None:
        gate = build_default_safety_gate()
        actions = {action.name: action for action in gate.list_actions()}

        self.assertIn("bias_set", actions)
        self.assertIn("z_setpoint_set", actions)
        self.assertIn("z_controller_set", actions)
        self.assertIn("bias_pulse", actions)
        self.assertIn("z_poke", actions)
        self.assertIn("tip_shaper", actions)
        self.assertIn("follow_me_move", actions)
        self.assertIn("bias_spectroscopy", actions)
        self.assertIn("scan_acquisition", actions)
        self.assertIn("scan_start_current_frame", actions)
        self.assertIn("data_log_configure", actions)
        self.assertIn("data_log_start", actions)
        self.assertIn("recipe_execution", actions)
        self.assertIn("coarse_xy_move", actions)
        self.assertIn("auto_approach", actions)
        self.assertIn("local_scan_acquisition", actions)
        self.assertNotIn("motor_move", actions)
        self.assertTrue(actions["follow_me_move"].live_enabled)
        self.assertTrue(actions["scan_start_current_frame"].live_enabled)
        self.assertTrue(
            all(
                not action.live_enabled
                for name, action in actions.items()
                if name not in {"follow_me_move", "scan_start_current_frame"}
            )
        )

    def test_valid_parameters_are_normalized(self) -> None:
        gate = build_default_safety_gate()

        result = gate.validate("bias_pulse", {"target_bias_v": "1.25", "duration_ms": 10})

        self.assertTrue(result.ok)
        self.assertEqual(result.violations, ())
        self.assertEqual(result.normalized_parameters, {"target_bias_v": 1.25, "duration_ms": 10.0})

    def test_out_of_range_parameters_are_rejected(self) -> None:
        gate = build_default_safety_gate()

        result = gate.validate("bias_pulse", {"target_bias_v": 10.1, "duration_ms": 10.0})

        self.assertFalse(result.ok)
        self.assertEqual(result.violations[0].code, "above_maximum")
        self.assertEqual(result.violations[0].parameter, "target_bias_v")

    def test_unknown_and_missing_parameters_are_rejected(self) -> None:
        gate = build_default_safety_gate()

        result = gate.validate("z_poke", {"unexpected": 1.0})
        codes = {violation.code for violation in result.violations}

        self.assertFalse(result.ok)
        self.assertIn("unknown_parameter", codes)
        self.assertIn("missing_required_parameter", codes)

    def test_live_execution_requires_explicit_action_enablement(self) -> None:
        gate = build_default_safety_gate()

        result = gate.validate(
            "bias_pulse",
            {"target_bias_v": 1.0, "duration_ms": 10.0},
            require_live_enabled=True,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.violations[0].code, "action_not_live_enabled")

    def test_require_raises_with_rejection_details(self) -> None:
        gate = build_default_safety_gate()

        with self.assertRaises(SafetyGateError) as context:
            gate.require("recipe_execution", {"max_steps": 2.5, "max_duration_s": 10.0})

        self.assertEqual(context.exception.result.violations[0].code, "not_integer")

    def test_device_exposes_control_action_validation(self) -> None:
        device = NanonisDevice(backend=FakeNanonisBackend())

        result = device.validate_control_action("tip_shaper", {"start_bias_v": 0.5})
        codes = {violation.code for violation in result.violations}

        self.assertFalse(result.ok)
        self.assertIn("missing_required_parameter", codes)

    def test_bias_spectroscopy_channel_bounds_are_registered(self) -> None:
        result = build_default_safety_gate().validate(
            "bias_spectroscopy",
            {
                "start_bias_v": -0.1,
                "end_bias_v": 0.1,
                "z_offset_m": 0.0,
                "number_of_sweeps": 1,
                "number_of_points": 5,
                "channel_count": 2,
                "minimum_channel_index": 0,
                "maximum_channel_index": 128,
                "maximum_slew_rate_v_per_s": 10.0,
                "integration_time_s": 0.002,
                "estimated_duration_s": 1.0,
            },
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.violations[0].parameter, "maximum_channel_index")

    def test_coarse_xy_bounds_exclude_both_z_direction_codes(self) -> None:
        gate = build_default_safety_gate()
        parameters = {"number_of_steps": 1, "group": 1}

        self.assertTrue(
            gate.validate("coarse_xy_move", {**parameters, "direction_code": 3}).ok
        )
        for forbidden in (4, 5):
            result = gate.validate(
                "coarse_xy_move",
                {**parameters, "direction_code": forbidden},
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.violations[0].parameter, "direction_code")


if __name__ == "__main__":
    unittest.main()
