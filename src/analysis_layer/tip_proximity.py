from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import math
from statistics import median

from instrument_layer import ZControllerStatus


class TipProximityState(str, Enum):
    FAR = "far"
    APPROACHING = "approaching"
    CANDIDATE = "candidate"
    NEAR_SAMPLE = "near_sample"


@dataclass(frozen=True)
class TipProximityPolicy:
    """Reviewed evidence for opening and closing a high-resolution segment."""

    minimum_effective_current_a: float
    minimum_setpoint_fraction: float
    maximum_setpoint_fraction: float
    minimum_z_limit_clearance_fraction: float
    minimum_z_limit_clearance_m: float
    arrival_hold_s: float
    arrival_minimum_samples: int
    far_noise_current_ceiling_a: float
    far_noise_window_s: float
    far_noise_minimum_samples: int
    far_noise_hold_s: float

    def __post_init__(self) -> None:
        if self.minimum_effective_current_a <= 0.0:
            raise ValueError("minimum_effective_current_a must be positive")
        if not 0.0 < self.minimum_setpoint_fraction <= 1.0:
            raise ValueError("minimum_setpoint_fraction must be in (0, 1]")
        if self.maximum_setpoint_fraction < 1.0:
            raise ValueError("maximum_setpoint_fraction must be at least 1")
        if not 0.0 <= self.minimum_z_limit_clearance_fraction < 0.5:
            raise ValueError(
                "minimum_z_limit_clearance_fraction must be in [0, 0.5)"
            )
        if self.minimum_z_limit_clearance_m < 0.0:
            raise ValueError("minimum_z_limit_clearance_m must be non-negative")
        if self.arrival_hold_s < 0.0:
            raise ValueError("arrival_hold_s must be non-negative")
        if self.arrival_minimum_samples < 1:
            raise ValueError("arrival_minimum_samples must be positive")
        if self.far_noise_current_ceiling_a <= 0.0:
            raise ValueError("far_noise_current_ceiling_a must be positive")
        if self.far_noise_window_s <= 0.0:
            raise ValueError("far_noise_window_s must be positive")
        if self.far_noise_minimum_samples < 1:
            raise ValueError("far_noise_minimum_samples must be positive")
        if self.far_noise_hold_s < 0.0:
            raise ValueError("far_noise_hold_s must be non-negative")

    def effective_current_window_a(
        self,
        current_setpoint_a: float,
    ) -> tuple[float, float]:
        setpoint_magnitude_a = abs(current_setpoint_a)
        if not math.isfinite(setpoint_magnitude_a) or setpoint_magnitude_a <= 0.0:
            return math.inf, -math.inf
        return (
            max(
                self.minimum_effective_current_a,
                self.minimum_setpoint_fraction * setpoint_magnitude_a,
            ),
            self.maximum_setpoint_fraction * setpoint_magnitude_a,
        )

    def z_limit_clearance_m(self, low_limit_m: float, high_limit_m: float) -> float:
        span_m = high_limit_m - low_limit_m
        if not math.isfinite(span_m) or span_m <= 0.0:
            return math.inf
        return max(
            self.minimum_z_limit_clearance_m,
            self.minimum_z_limit_clearance_fraction * span_m,
        )


@dataclass(frozen=True)
class TipProximityObservation:
    monotonic_s: float
    current_a: float
    current_setpoint_a: float
    z_position_m: float
    z_low_limit_m: float
    z_high_limit_m: float
    z_controller_on: bool
    z_controller_status: ZControllerStatus
    auto_approach_running: bool
    withdraw_confirmed: bool = False


@dataclass(frozen=True)
class TipProximityDecision:
    previous_state: TipProximityState
    state: TipProximityState
    reason: str
    current_window_samples: int = 0
    median_current_a: float | None = None
    median_setpoint_ratio: float | None = None

    @property
    def changed(self) -> bool:
        return self.previous_state is not self.state

    @property
    def high_resolution_required(self) -> bool:
        return self.state is TipProximityState.NEAR_SAMPLE


class TipProximityClassifier:
    """Stateful, hysteretic proof of arrival; it never operates the instrument."""

    def __init__(self, policy: TipProximityPolicy) -> None:
        self.policy = policy
        self.state = TipProximityState.FAR
        self._candidate_since_s: float | None = None
        self._current_window: deque[tuple[float, float, float]] = deque()
        self._far_noise_window: deque[tuple[float, float]] = deque()
        self._far_noise_observed_since_s: float | None = None
        self._far_noise_since_s: float | None = None
        self._last_monotonic_s: float | None = None

    def update(self, observation: TipProximityObservation) -> TipProximityDecision:
        if self._last_monotonic_s is not None and observation.monotonic_s < self._last_monotonic_s:
            raise ValueError("tip-proximity observations must use a non-decreasing monotonic clock")
        self._last_monotonic_s = observation.monotonic_s
        previous = self.state

        if self.state is TipProximityState.NEAR_SAMPLE:
            (
                noise_close_ready,
                noise_window_samples,
                noise_median_current_a,
            ) = self._update_far_noise_evidence(observation)
            if noise_close_ready:
                self._reset_evidence()
                self._reset_far_noise_evidence()
                self.state = TipProximityState.FAR
                return TipProximityDecision(
                    previous,
                    self.state,
                    "withdrawn_sustained_current_noise",
                    noise_window_samples,
                    noise_median_current_a,
                    None,
                )
            if observation.withdraw_confirmed:
                reason = "latched_z_withdraw"
            elif observation.auto_approach_running:
                reason = "latched_reapproaching"
            elif not observation.z_controller_on:
                reason = "latched_feedback_open"
            else:
                reason = "latched_until_sustained_current_noise"
            return TipProximityDecision(previous, self.state, reason)

        self._reset_far_noise_evidence()
        if self._feedback_ready(observation):
            if not self._z_inside_limits(observation):
                self._reset_evidence()
                self.state = TipProximityState.CANDIDATE
                return TipProximityDecision(
                    previous,
                    self.state,
                    "z_control_on_z_at_limit",
                )
            if not self._valid_current_observation(observation):
                self._reset_evidence()
                self.state = TipProximityState.CANDIDATE
                return TipProximityDecision(
                    previous,
                    self.state,
                    "z_control_on_current_invalid",
                )

            self._append_current_observation(observation)
            window_samples, median_current_a, median_setpoint_ratio = (
                self._current_window_metrics()
            )
            if not self._current_window_ready(observation.monotonic_s):
                self.state = TipProximityState.CANDIDATE
                return TipProximityDecision(
                    previous,
                    self.state,
                    "current_setpoint_window_accumulating",
                    window_samples,
                    median_current_a,
                    median_setpoint_ratio,
                )

            if (
                median_current_a >= self.policy.minimum_effective_current_a
                and self.policy.minimum_setpoint_fraction
                <= median_setpoint_ratio
                <= self.policy.maximum_setpoint_fraction
            ):
                self.state = TipProximityState.NEAR_SAMPLE
                return TipProximityDecision(
                    previous,
                    self.state,
                    "z_control_on_current_converged_to_setpoint_z_inside_limits",
                    window_samples,
                    median_current_a,
                    median_setpoint_ratio,
                )

            self.state = TipProximityState.CANDIDATE
            if (
                median_current_a < self.policy.minimum_effective_current_a
                or median_setpoint_ratio < self.policy.minimum_setpoint_fraction
            ):
                reason = "z_control_on_current_below_threshold"
            elif median_setpoint_ratio > self.policy.maximum_setpoint_fraction:
                reason = "z_control_on_current_above_setpoint_window"
            else:
                reason = "arrival_evidence_invalid"
            return TipProximityDecision(
                previous,
                self.state,
                reason,
                window_samples,
                median_current_a,
                median_setpoint_ratio,
            )

        if observation.auto_approach_running:
            self._candidate_since_s = None
            self.state = TipProximityState.APPROACHING
            return TipProximityDecision(previous, self.state, "auto_approach_running")

        self._candidate_since_s = None
        self.state = TipProximityState.FAR
        return TipProximityDecision(previous, self.state, "entry_evidence_absent")

    @staticmethod
    def _valid_current_observation(observation: TipProximityObservation) -> bool:
        return (
            math.isfinite(observation.current_a)
            and math.isfinite(observation.current_setpoint_a)
            and abs(observation.current_setpoint_a) > 0.0
        )

    def _append_current_observation(
        self,
        observation: TipProximityObservation,
    ) -> None:
        if self._candidate_since_s is None:
            self._candidate_since_s = observation.monotonic_s
        current_magnitude_a = abs(observation.current_a)
        setpoint_ratio = current_magnitude_a / abs(observation.current_setpoint_a)
        self._current_window.append(
            (observation.monotonic_s, current_magnitude_a, setpoint_ratio)
        )
        cutoff_s = observation.monotonic_s - self.policy.arrival_hold_s
        while self._current_window and self._current_window[0][0] < cutoff_s:
            self._current_window.popleft()

    def _current_window_ready(self, monotonic_s: float) -> bool:
        return (
            self._candidate_since_s is not None
            and monotonic_s - self._candidate_since_s
            >= self.policy.arrival_hold_s
            and len(self._current_window) >= self.policy.arrival_minimum_samples
        )

    def _current_window_metrics(self) -> tuple[int, float, float]:
        return (
            len(self._current_window),
            median(sample[1] for sample in self._current_window),
            median(sample[2] for sample in self._current_window),
        )

    def _update_far_noise_evidence(
        self,
        observation: TipProximityObservation,
    ) -> tuple[bool, int, float | None]:
        if (
            observation.z_controller_on
            or not observation.withdraw_confirmed
            or not math.isfinite(observation.current_a)
        ):
            self._reset_far_noise_evidence()
            return False, 0, None
        if self._far_noise_observed_since_s is None:
            self._far_noise_observed_since_s = observation.monotonic_s
        self._far_noise_window.append(
            (observation.monotonic_s, abs(observation.current_a))
        )
        cutoff_s = observation.monotonic_s - self.policy.far_noise_window_s
        while self._far_noise_window and self._far_noise_window[0][0] < cutoff_s:
            self._far_noise_window.popleft()
        samples = len(self._far_noise_window)
        if (
            observation.monotonic_s - self._far_noise_observed_since_s
            < self.policy.far_noise_window_s
            or samples < self.policy.far_noise_minimum_samples
        ):
            return False, samples, None

        median_current_a = median(sample[1] for sample in self._far_noise_window)
        if median_current_a > self.policy.far_noise_current_ceiling_a:
            self._far_noise_since_s = None
            return False, samples, median_current_a
        if self._far_noise_since_s is None:
            self._far_noise_since_s = observation.monotonic_s
        return (
            observation.monotonic_s - self._far_noise_since_s
            >= self.policy.far_noise_hold_s,
            samples,
            median_current_a,
        )

    def _z_inside_limits(self, observation: TipProximityObservation) -> bool:
        values = (
            observation.z_position_m,
            observation.z_low_limit_m,
            observation.z_high_limit_m,
        )
        if not all(math.isfinite(value) for value in values):
            return False
        clearance_m = self.policy.z_limit_clearance_m(
            observation.z_low_limit_m,
            observation.z_high_limit_m,
        )
        return (
            observation.z_low_limit_m + clearance_m
            < observation.z_position_m
            < observation.z_high_limit_m - clearance_m
        )

    @staticmethod
    def _feedback_ready(observation: TipProximityObservation) -> bool:
        return (
            observation.z_controller_on
            and observation.z_controller_status is ZControllerStatus.ON
        )

    def _reset_evidence(self) -> None:
        self._candidate_since_s = None
        self._current_window.clear()

    def _reset_far_noise_evidence(self) -> None:
        self._far_noise_window.clear()
        self._far_noise_observed_since_s = None
        self._far_noise_since_s = None
