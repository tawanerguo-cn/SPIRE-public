from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analysis_layer import (  # noqa: E402
    TipProximityClassifier,
    TipProximityObservation,
    TipProximityPolicy,
    TipProximityState,
)
from instrument_layer import ZControllerStatus  # noqa: E402


def observation(
    time_s: float,
    *,
    current_a: float = -100e-12,
    setpoint_a: float = 100e-12,
    z_position_m: float = 0.0,
    z_low_limit_m: float = -128e-9,
    z_high_limit_m: float = 128e-9,
    feedback: bool = True,
    status: ZControllerStatus = ZControllerStatus.ON,
    approaching: bool = False,
    withdraw: bool = False,
) -> TipProximityObservation:
    return TipProximityObservation(
        monotonic_s=time_s,
        current_a=current_a,
        current_setpoint_a=setpoint_a,
        z_position_m=z_position_m,
        z_low_limit_m=z_low_limit_m,
        z_high_limit_m=z_high_limit_m,
        z_controller_on=feedback,
        z_controller_status=status,
        auto_approach_running=approaching,
        withdraw_confirmed=withdraw,
    )


class TipProximityClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = TipProximityClassifier(
            TipProximityPolicy(
                minimum_effective_current_a=1.5e-12,
                minimum_setpoint_fraction=0.7,
                maximum_setpoint_fraction=1.5,
                minimum_z_limit_clearance_fraction=1e-4,
                minimum_z_limit_clearance_m=1e-12,
                arrival_hold_s=2.0,
                arrival_minimum_samples=3,
                far_noise_current_ceiling_a=0.75e-12,
                far_noise_window_s=1.0,
                far_noise_minimum_samples=3,
                far_noise_hold_s=0.0,
            )
        )

    def test_arrival_requires_sustained_control_and_effective_current(self) -> None:
        first = self.classifier.update(observation(0.0))
        second = self.classifier.update(observation(1.0))
        arrived = self.classifier.update(observation(2.0))

        self.assertEqual(first.state, TipProximityState.CANDIDATE)
        self.assertEqual(second.state, TipProximityState.CANDIDATE)
        self.assertEqual(arrived.state, TipProximityState.NEAR_SAMPLE)
        self.assertTrue(arrived.high_resolution_required)

    def test_control_off_does_not_prove_arrival(self) -> None:
        self.classifier.update(observation(0.0))
        result = self.classifier.update(observation(0.5, feedback=False))

        self.assertEqual(result.state, TipProximityState.FAR)

    def test_control_on_without_effective_current_does_not_record(self) -> None:
        self.classifier.update(
            observation(0.0, current_a=-0.06e-12, setpoint_a=100e-12)
        )
        self.classifier.update(
            observation(1.0, current_a=-0.07e-12, setpoint_a=100e-12)
        )
        result = self.classifier.update(
            observation(2.0, current_a=0.05e-12, setpoint_a=100e-12)
        )

        self.assertEqual(result.state, TipProximityState.CANDIDATE)
        self.assertFalse(result.high_resolution_required)
        self.assertEqual(result.reason, "z_control_on_current_below_threshold")

    def test_narrow_two_pa_spike_does_not_move_window_median_to_setpoint(self) -> None:
        self.classifier.update(
            observation(0.0, current_a=0.3e-12, setpoint_a=2e-12)
        )
        self.classifier.update(
            observation(1.0, current_a=-2e-12, setpoint_a=2e-12)
        )
        result = self.classifier.update(
            observation(2.0, current_a=-0.4e-12, setpoint_a=2e-12)
        )

        self.assertEqual(result.state, TipProximityState.CANDIDATE)
        self.assertEqual(result.reason, "z_control_on_current_below_threshold")
        self.assertAlmostEqual(result.median_current_a, 0.4e-12)
        self.assertAlmostEqual(result.median_setpoint_ratio, 0.2)

    def test_sustained_current_above_setpoint_window_does_not_record(self) -> None:
        self.classifier.update(
            observation(0.0, current_a=200e-12, setpoint_a=100e-12)
        )
        self.classifier.update(
            observation(1.0, current_a=190e-12, setpoint_a=100e-12)
        )
        result = self.classifier.update(
            observation(2.0, current_a=180e-12, setpoint_a=100e-12)
        )

        self.assertEqual(result.state, TipProximityState.CANDIDATE)
        self.assertEqual(result.reason, "z_control_on_current_above_setpoint_window")

    def test_effective_current_at_z_limit_does_not_record(self) -> None:
        result = self.classifier.update(
            observation(0.0, z_position_m=-128e-9)
        )

        self.assertEqual(result.state, TipProximityState.CANDIDATE)
        self.assertFalse(result.high_resolution_required)
        self.assertEqual(result.reason, "z_control_on_z_at_limit")

    def test_minimum_current_floor_applies_to_two_pa_setpoint(self) -> None:
        below = self.classifier.update(
            observation(0.0, current_a=1.4e-12, setpoint_a=2e-12)
        )
        self.classifier.update(
            observation(1.0, current_a=1.4e-12, setpoint_a=2e-12)
        )
        below_ready = self.classifier.update(
            observation(2.0, current_a=1.4e-12, setpoint_a=2e-12)
        )
        self.classifier.update(
            observation(3.0, current_a=1.5e-12, setpoint_a=2e-12)
        )
        arrived = self.classifier.update(
            observation(4.0, current_a=1.5e-12, setpoint_a=2e-12)
        )

        self.assertEqual(below.reason, "current_setpoint_window_accumulating")
        self.assertEqual(below_ready.reason, "z_control_on_current_below_threshold")
        self.assertEqual(arrived.state, TipProximityState.NEAR_SAMPLE)

    def test_feedback_open_after_arrival_is_latched_for_constant_height(self) -> None:
        self.classifier.update(observation(0.0))
        self.classifier.update(observation(1.0))
        self.classifier.update(observation(2.0))

        result = self.classifier.update(observation(3.0, feedback=False))

        self.assertEqual(result.state, TipProximityState.NEAR_SAMPLE)
        self.assertEqual(result.reason, "latched_feedback_open")

    def test_z_withdraw_waits_for_noise_window(self) -> None:
        self.classifier.update(observation(0.0))
        self.classifier.update(observation(1.0))
        self.classifier.update(observation(2.0))
        withdrawn = self.classifier.update(observation(2.1, withdraw=True))

        self.assertEqual(withdrawn.state, TipProximityState.NEAR_SAMPLE)
        self.assertEqual(withdrawn.reason, "latched_z_withdraw")

    def test_latched_segment_stays_open_without_fresh_entry_evidence(self) -> None:
        self.classifier.update(observation(0.0))
        self.classifier.update(observation(1.0))
        self.classifier.update(observation(2.0))
        pending = self.classifier.update(
            observation(3.0, feedback=False, status=ZControllerStatus.OFF)
        )
        still_recording = self.classifier.update(
            observation(8.0, feedback=False, status=ZControllerStatus.OFF)
        )

        self.assertEqual(pending.state, TipProximityState.NEAR_SAMPLE)
        self.assertEqual(still_recording.state, TipProximityState.NEAR_SAMPLE)

    def test_sustained_noise_closes_latched_segment(self) -> None:
        classifier = TipProximityClassifier(
            replace(
                self.classifier.policy,
                arrival_hold_s=0.0,
                arrival_minimum_samples=1,
            )
        )
        self.assertEqual(
            classifier.update(observation(0.0)).state,
            TipProximityState.NEAR_SAMPLE,
        )
        for time_s in (1.0, 1.5):
            pending = classifier.update(
                observation(
                    time_s,
                    current_a=0.3e-12,
                    setpoint_a=2e-12,
                    feedback=False,
                    status=ZControllerStatus.OFF,
                    withdraw=True,
                )
            )
            self.assertEqual(pending.state, TipProximityState.NEAR_SAMPLE)

        closed = classifier.update(
            observation(
                2.0,
                current_a=0.3e-12,
                setpoint_a=2e-12,
                feedback=False,
                status=ZControllerStatus.OFF,
                withdraw=True,
            )
        )

        self.assertEqual(closed.state, TipProximityState.FAR)
        self.assertEqual(closed.reason, "withdrawn_sustained_current_noise")
        self.assertAlmostEqual(closed.median_current_a, 0.3e-12)

    def test_narrow_noise_spike_does_not_prevent_sustained_noise_close(self) -> None:
        classifier = TipProximityClassifier(
            replace(
                self.classifier.policy,
                arrival_hold_s=0.0,
                arrival_minimum_samples=1,
            )
        )
        classifier.update(observation(0.0))
        classifier.update(
            observation(
                1.0,
                current_a=0.3e-12,
                feedback=False,
                status=ZControllerStatus.OFF,
                withdraw=True,
            )
        )
        classifier.update(
            observation(
                1.5,
                current_a=0.4e-12,
                feedback=False,
                status=ZControllerStatus.OFF,
                withdraw=True,
            )
        )
        closed = classifier.update(
            observation(
                2.0,
                current_a=-2e-12,
                feedback=False,
                status=ZControllerStatus.OFF,
                withdraw=True,
            )
        )

        self.assertEqual(closed.state, TipProximityState.FAR)
        self.assertEqual(closed.reason, "withdrawn_sustained_current_noise")

    def test_recovered_tunnel_current_cancels_noise_close_countdown(self) -> None:
        classifier = TipProximityClassifier(
            replace(
                self.classifier.policy,
                arrival_hold_s=0.0,
                arrival_minimum_samples=1,
            )
        )
        classifier.update(observation(0.0))
        classifier.update(
            observation(
                1.0,
                current_a=0.3e-12,
                setpoint_a=2e-12,
                feedback=False,
                status=ZControllerStatus.OFF,
                withdraw=True,
            )
        )
        for time_s in (1.5, 2.0, 3.0):
            result = classifier.update(
                observation(time_s, current_a=2e-12, setpoint_a=2e-12)
            )

        self.assertEqual(result.state, TipProximityState.NEAR_SAMPLE)

    def test_auto_approach_with_control_off_is_approaching(self) -> None:
        result = self.classifier.update(
            observation(
                0.0,
                approaching=True,
                feedback=False,
                status=ZControllerStatus.OFF,
            )
        )

        self.assertEqual(result.state, TipProximityState.APPROACHING)

    def test_control_on_with_effective_current_can_open_without_action_event(self) -> None:
        classifier = TipProximityClassifier(
            TipProximityPolicy(
                minimum_effective_current_a=1.5e-12,
                minimum_setpoint_fraction=0.7,
                maximum_setpoint_fraction=1.5,
                minimum_z_limit_clearance_fraction=1e-4,
                minimum_z_limit_clearance_m=1e-12,
                arrival_hold_s=0.0,
                arrival_minimum_samples=1,
                far_noise_current_ceiling_a=0.75e-12,
                far_noise_window_s=1.0,
                far_noise_minimum_samples=3,
                far_noise_hold_s=0.0,
            )
        )
        result = classifier.update(observation(0.0))

        self.assertEqual(result.state, TipProximityState.NEAR_SAMPLE)

if __name__ == "__main__":
    unittest.main()
