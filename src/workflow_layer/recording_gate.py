from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from threading import Event, Lock, Thread
import time

from analysis_layer import (
    TipProximityClassifier,
    TipProximityObservation,
    TipProximityPolicy,
    TipProximityState,
)
from instrument_layer import NanonisDevice, ZControllerStatus


@dataclass(frozen=True)
class RecordingGateEvent:
    event_type: str
    monotonic_ns: int
    utc_ns: int
    payload: dict


@dataclass(frozen=True)
class RecordingGateSnapshot:
    """Latest status evidence, reusable by other read-only observers.

    Keeping this snapshot in the workflow layer avoids issuing duplicate Nanonis
    getters for state already sampled by the gate monitor.
    """

    monotonic_ns: int
    current_a: float
    current_setpoint_a: float
    z_position_m: float
    z_low_limit_m: float
    z_high_limit_m: float
    z_controller_on: bool
    z_controller_status: ZControllerStatus
    auto_approach_running: bool


class TipRecordingGateMonitor:
    """Poll action/state evidence without persisting far-from-sample telemetry."""

    def __init__(
        self,
        status_device: NanonisDevice,
        *,
        policy: TipProximityPolicy,
        poll_interval_s: float,
        z_limits_refresh_interval_s: float = 1.0,
    ) -> None:
        if poll_interval_s <= 0.0:
            raise ValueError("poll_interval_s must be positive")
        if z_limits_refresh_interval_s <= 0.0:
            raise ValueError("z_limits_refresh_interval_s must be positive")
        self.status_device = status_device
        self.classifier = TipProximityClassifier(policy)
        self.poll_interval_s = float(poll_interval_s)
        self.z_limits_refresh_interval_s = float(
            z_limits_refresh_interval_s
        )
        self._state = TipProximityState.FAR
        self._state_reason = "initial"
        self._high_resolution_transition_reason = "initial"
        self._high_resolution_transition_monotonic_ns = 0
        self._events: deque[RecordingGateEvent] = deque()
        self._lock = Lock()
        self._stop = Event()
        self._thread: Thread | None = None
        self._error: BaseException | None = None
        self._previous_auto_approach_running = False
        self._latest_snapshot: RecordingGateSnapshot | None = None

    @property
    def state(self) -> TipProximityState:
        with self._lock:
            return self._state

    @property
    def high_resolution_enabled(self) -> bool:
        return self.high_resolution_status()[0]

    def high_resolution_status(self) -> tuple[bool, str, int]:
        """Return an atomic gate value and the reason for its latest edge."""
        with self._lock:
            return (
                self._state is TipProximityState.NEAR_SAMPLE,
                self._high_resolution_transition_reason,
                self._high_resolution_transition_monotonic_ns,
            )

    def latest_snapshot(self) -> RecordingGateSnapshot | None:
        """Return one atomic immutable status snapshot, if sampling has begun."""

        with self._lock:
            return self._latest_snapshot

    @property
    def state_reason(self) -> str:
        with self._lock:
            return self._state_reason

    @property
    def failed(self) -> bool:
        return self._error is not None

    @property
    def error(self) -> BaseException | None:
        return self._error

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise RuntimeError("tip recording gate monitor failed") from self._error

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("tip recording gate monitor has already been started")
        self._thread = Thread(target=self._run, name="tip-recording-gate", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.poll_interval_s * 4.0))
            if self._thread.is_alive():
                raise TimeoutError("tip recording gate monitor did not stop")

    def drain_events(self) -> list[RecordingGateEvent]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    def _run(self) -> None:
        try:
            z_limits = self.status_device.get_z_limits()
            next_z_limits_refresh_s = (
                time.perf_counter() + self.z_limits_refresh_interval_s
            )
            while not self._stop.is_set():
                monotonic_ns = time.perf_counter_ns()
                utc_ns = time.time_ns()
                now_s = monotonic_ns / 1_000_000_000
                if now_s >= next_z_limits_refresh_s:
                    z_limits = self.status_device.get_z_limits()
                    next_z_limits_refresh_s = (
                        now_s + self.z_limits_refresh_interval_s
                    )
                auto_running = self.status_device.get_auto_approach_running()
                auto_approach_started = (
                    auto_running
                    and not self._previous_auto_approach_running
                )
                self._previous_auto_approach_running = auto_running
                current_a = self.status_device.get_current_a()
                z_position_m = self.status_device.get_z_position_m()
                z_controller_on = self.status_device.get_z_controller_on()
                z_controller_status = (
                    self.status_device.get_z_controller_status()
                )
                current_setpoint_a = self.status_device.get_z_setpoint_a()
                snapshot = RecordingGateSnapshot(
                    monotonic_ns=monotonic_ns,
                    current_a=current_a,
                    current_setpoint_a=current_setpoint_a,
                    z_position_m=z_position_m,
                    z_low_limit_m=z_limits.low_limit_m,
                    z_high_limit_m=z_limits.high_limit_m,
                    z_controller_on=z_controller_on,
                    z_controller_status=z_controller_status,
                    auto_approach_running=auto_running,
                )
                decision = self.classifier.update(
                    TipProximityObservation(
                        monotonic_s=now_s,
                        current_a=current_a,
                        current_setpoint_a=current_setpoint_a,
                        z_position_m=z_position_m,
                        z_low_limit_m=z_limits.low_limit_m,
                        z_high_limit_m=z_limits.high_limit_m,
                        z_controller_on=z_controller_on,
                        z_controller_status=z_controller_status,
                        auto_approach_running=auto_running,
                        withdraw_confirmed=_withdraw_confirmed(snapshot),
                    )
                )
                current_minimum_a, current_maximum_a = (
                    self.classifier.policy.effective_current_window_a(
                        current_setpoint_a
                    )
                )
                events = _events_from_evidence(
                    monotonic_ns,
                    utc_ns,
                    auto_approach_started=auto_approach_started,
                    decision=decision,
                    current_a=current_a,
                    current_setpoint_a=current_setpoint_a,
                    required_current_minimum_a=current_minimum_a,
                    required_current_maximum_a=current_maximum_a,
                    z_position_m=z_position_m,
                    z_low_limit_m=z_limits.low_limit_m,
                    z_high_limit_m=z_limits.high_limit_m,
                    required_z_clearance_m=self.classifier.policy.z_limit_clearance_m(
                        z_limits.low_limit_m,
                        z_limits.high_limit_m,
                    ),
                )
                with self._lock:
                    previously_enabled = (
                        self._state is TipProximityState.NEAR_SAMPLE
                    )
                    if decision.high_resolution_required != previously_enabled:
                        self._high_resolution_transition_reason = decision.reason
                        self._high_resolution_transition_monotonic_ns = (
                            monotonic_ns
                        )
                    self._state = decision.state
                    self._state_reason = decision.reason
                    self._latest_snapshot = snapshot
                    self._events.extend(events)
                self._stop.wait(self.poll_interval_s)
        except BaseException as exc:
            self._error = exc
            with self._lock:
                previous = self._state
                self._state = TipProximityState.FAR
                self._state_reason = "recording_gate_failed"
                self._high_resolution_transition_reason = "recording_gate_failed"
                self._high_resolution_transition_monotonic_ns = time.perf_counter_ns()
                self._events.append(
                    RecordingGateEvent(
                        event_type="recording_gate_failed",
                        monotonic_ns=time.perf_counter_ns(),
                        utc_ns=time.time_ns(),
                        payload={
                            "previous_state": previous.value,
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                        },
                    )
                )

def _withdraw_confirmed(snapshot: RecordingGateSnapshot) -> bool:
    if snapshot.z_controller_status in {
        ZControllerStatus.SAFE_TIP,
        ZControllerStatus.WITHDRAWING,
    }:
        return True
    if snapshot.z_controller_status is not ZControllerStatus.OFF or snapshot.z_controller_on:
        return False
    span_m = abs(snapshot.z_high_limit_m - snapshot.z_low_limit_m)
    tolerance_m = max(1e-12, span_m * 1e-4)
    distance_m = abs(snapshot.z_position_m - snapshot.z_high_limit_m)
    return math.isfinite(distance_m) and distance_m <= tolerance_m


def _events_from_evidence(
    monotonic_ns: int,
    utc_ns: int,
    *,
    auto_approach_started: bool,
    decision,
    current_a: float,
    current_setpoint_a: float,
    required_current_minimum_a: float,
    required_current_maximum_a: float,
    z_position_m: float,
    z_low_limit_m: float,
    z_high_limit_m: float,
    required_z_clearance_m: float,
) -> list[RecordingGateEvent]:
    events: list[RecordingGateEvent] = []
    if auto_approach_started:
        events.append(RecordingGateEvent("auto_approach_started", monotonic_ns, utc_ns, {}))
    if decision.changed:
        events.append(
            RecordingGateEvent(
                "tip_proximity_state_changed",
                monotonic_ns,
                utc_ns,
                {
                    "previous_state": decision.previous_state.value,
                    "state": decision.state.value,
                    "reason": decision.reason,
                    "current_a": current_a,
                    "current_setpoint_a": current_setpoint_a,
                    "required_current_minimum_a": required_current_minimum_a,
                    "required_current_maximum_a": required_current_maximum_a,
                    "current_window_samples": decision.current_window_samples,
                    "median_current_a": decision.median_current_a,
                    "median_setpoint_ratio": decision.median_setpoint_ratio,
                    "z_position_m": z_position_m,
                    "z_low_limit_m": z_low_limit_m,
                    "z_high_limit_m": z_high_limit_m,
                    "required_z_clearance_m": required_z_clearance_m,
                },
            )
        )
    return events
