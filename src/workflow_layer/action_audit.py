from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import math
from threading import Event, Lock, Thread
import time
from typing import Callable

from instrument_layer import (
    ActionActor,
    ActionEventEmitter,
    ActionIntent,
    ActionName,
    ActionPhase,
    InstrumentActionEvent,
    NanonisDevice,
    NanonisCommandError,
    ZControllerStatus,
)
from .recording_gate import RecordingGateSnapshot


class ActionAuditStartMode(str, Enum):
    RECORDER_START = "recorder_start"
    TIP_NEAR_SAMPLE = "tip_near_sample"
    APPROACH_EVIDENCE = "approach_evidence"


class ApproachStartEvidenceSource(str, Enum):
    CCD_VISION = "ccd_vision"
    OPERATOR_CONFIRMATION = "operator_confirmation"
    AUTO_APPROACH_FALLBACK = "auto_approach_fallback"


class ApproachEndEvidenceSource(str, Enum):
    CCD_VISION = "ccd_vision"
    OPERATOR_CONFIRMATION = "operator_confirmation"


@dataclass(frozen=True)
class ApproachStartEvidence:
    source: ApproachStartEvidenceSource
    evidence_id: str
    confidence: float | None = None
    image_artifact: str | None = None
    detector_version: str | None = None
    details: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.evidence_id.strip():
            raise ValueError("approach-start evidence_id must not be empty")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("approach-start confidence must be in [0, 1]")
        if (
            self.source is ApproachStartEvidenceSource.CCD_VISION
            and self.image_artifact is None
        ):
            raise ValueError(
                "CCD approach-start evidence requires an image artifact reference"
            )


class ApproachStartController:
    """Typed entry point for CCD, operator, or explicit fallback evidence."""

    def __init__(self, event_emitter: ActionEventEmitter | None = None) -> None:
        self.action_events = event_emitter or ActionEventEmitter()

    def confirm(self, evidence: ApproachStartEvidence) -> None:
        actor = (
            ActionActor.OPERATOR
            if evidence.source
            is ApproachStartEvidenceSource.OPERATOR_CONFIRMATION
            else ActionActor.SPIRE
        )
        self.action_events.record_instantaneous(
            ActionName.COARSE_APPROACH_SESSION_START,
            arguments=asdict(evidence),
            result={"action_audit_open": True},
            source="ApproachStartController.confirm",
            actor=actor,
            intent=ActionIntent.START_AUDIT,
        )


@dataclass(frozen=True)
class ApproachEndEvidence:
    source: ApproachEndEvidenceSource
    evidence_id: str
    image_artifact: str | None = None
    details: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.evidence_id.strip():
            raise ValueError("approach-end evidence_id must not be empty")
        if (
            self.source is ApproachEndEvidenceSource.CCD_VISION
            and self.image_artifact is None
        ):
            raise ValueError(
                "CCD approach-end evidence requires an image artifact reference"
            )


class ApproachEndController:
    """Typed manual/vision fallback for ending the latched action timeline."""

    def __init__(self, event_emitter: ActionEventEmitter | None = None) -> None:
        self.action_events = event_emitter or ActionEventEmitter()

    def confirm(self, evidence: ApproachEndEvidence) -> None:
        actor = (
            ActionActor.OPERATOR
            if evidence.source
            is ApproachEndEvidenceSource.OPERATOR_CONFIRMATION
            else ActionActor.SPIRE
        )
        self.action_events.record_instantaneous(
            ActionName.COARSE_RETRACT_SESSION_END,
            arguments=asdict(evidence),
            result={"action_audit_open": False},
            source="ApproachEndController.confirm",
            actor=actor,
            intent=ActionIntent.END_AUDIT,
        )


class LatchedActionEventSink:
    """Buffer until the start boundary, then stay active across IXYZ segments."""

    def __init__(
        self,
        sink,
        *,
        start_mode: ActionAuditStartMode,
        maximum_buffered_events: int = 10_000,
    ) -> None:
        if maximum_buffered_events < 1:
            raise ValueError("maximum_buffered_events must be positive")
        self._sink = sink
        self.start_mode = start_mode
        self._enabled = start_mode is ActionAuditStartMode.RECORDER_START
        self._ever_opened = self._enabled
        self._closed = False
        self._lock = Lock()
        self._dropped_before_open = 0
        self._dropped_after_close = 0
        self._buffer: list[InstrumentActionEvent] = []
        self._maximum_buffered_events = int(maximum_buffered_events)
        self._opened_reason = (
            "recorder_start" if self._enabled else None
        )
        self._closed_reason: str | None = None

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def ever_opened(self) -> bool:
        with self._lock:
            return self._ever_opened

    @property
    def dropped_before_open(self) -> int:
        with self._lock:
            return self._dropped_before_open

    @property
    def dropped_after_close(self) -> int:
        with self._lock:
            return self._dropped_after_close

    @property
    def opened_reason(self) -> str | None:
        with self._lock:
            return self._opened_reason

    @property
    def closed_reason(self) -> str | None:
        with self._lock:
            return self._closed_reason

    def open(self, *, cutoff_monotonic_ns: int, reason: str) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("action event sink is already closed")
            if self._enabled:
                return
            accepted = [
                event
                for event in self._buffer
                if event.monotonic_ns >= cutoff_monotonic_ns
            ]
            self._dropped_before_open += len(self._buffer) - len(accepted)
            self._buffer.clear()
            self._enabled = True
            self._ever_opened = True
            self._opened_reason = reason
            for event in accepted:
                self._sink(event)

    def close(self, *, reason: str) -> None:
        with self._lock:
            if self._closed:
                return
            if not self._enabled:
                self._dropped_before_open += len(self._buffer)
                self._buffer.clear()
            self._enabled = False
            self._closed = True
            self._closed_reason = reason

    def __call__(self, event: InstrumentActionEvent) -> None:
        with self._lock:
            if self._closed:
                self._dropped_after_close += 1
                return
            if (
                not self._enabled
                and self.start_mode is ActionAuditStartMode.APPROACH_EVIDENCE
                and _opens_action_audit(event)
            ):
                accepted = [
                    buffered
                    for buffered in self._buffer
                    if buffered.monotonic_ns >= event.monotonic_ns
                ]
                self._dropped_before_open += (
                    len(self._buffer) - len(accepted)
                )
                self._buffer.clear()
                self._enabled = True
                self._ever_opened = True
                self._opened_reason = "approach_evidence"
                for buffered in accepted:
                    self._sink(buffered)
            if not self._enabled:
                if len(self._buffer) >= self._maximum_buffered_events:
                    raise BufferError(
                        "action-event pre-start buffer is full"
                    )
                self._buffer.append(event)
                return
            self._sink(event)
            if (
                event.action_name
                is ActionName.COARSE_RETRACT_SESSION_END
                and event.phase in {
                    ActionPhase.AFTER,
                    ActionPhase.OBSERVED,
                }
            ):
                self._enabled = False
                self._closed = True
                self._closed_reason = (
                    f"typed_{event.source}"
                )


@dataclass(frozen=True)
class ObservedInstrumentState:
    bias_v: float
    z_setpoint_a: float
    z_position_m: float
    z_controller_on: bool
    z_controller_status: ZControllerStatus
    follow_me_x_m: float
    follow_me_y_m: float
    scan_running: bool
    bias_spectroscopy_running: bool
    auto_approach_running: bool


@dataclass
class _PendingPositionObservation:
    before: dict[str, float]
    after: dict[str, float]
    changed_fields: tuple[str, ...]
    last_changed_s: float


class ExternalStateObserver:
    """Record read-back changes without assigning a GUI user or an intent."""

    def __init__(
        self,
        status_device: NanonisDevice,
        event_emitter: ActionEventEmitter,
        *,
        poll_interval_s: float = 0.1,
        bias_tolerance_v: float = 1e-9,
        setpoint_tolerance_a: float = 1e-15,
        position_tolerance_m: float = 1e-12,
        position_settle_s: float = 0.3,
        observe_tip_shaper_parameters: bool = False,
        gate_snapshot_provider: (
            Callable[[], RecordingGateSnapshot | None] | None
        ) = None,
    ) -> None:
        if poll_interval_s <= 0.0:
            raise ValueError("poll_interval_s must be positive")
        if min(
            bias_tolerance_v,
            setpoint_tolerance_a,
            position_tolerance_m,
        ) <= 0.0:
            raise ValueError("external-state tolerances must be positive")
        if position_settle_s <= 0.0:
            raise ValueError("position_settle_s must be positive")
        self.status_device = status_device
        self.event_emitter = event_emitter
        self.poll_interval_s = float(poll_interval_s)
        self.bias_tolerance_v = float(bias_tolerance_v)
        self.setpoint_tolerance_a = float(setpoint_tolerance_a)
        self.position_tolerance_m = float(position_tolerance_m)
        self.position_settle_s = float(position_settle_s)
        self.gate_snapshot_provider = gate_snapshot_provider
        self.observe_tip_shaper_parameters = observe_tip_shaper_parameters
        self._tip_shaper_snapshot = None
        self._tip_shaper_next_poll_s = 0.0
        self._tip_shaper_unavailable = False
        self._tip_shaper_capture_active = False
        self._stop = Event()
        self._snapshot_requested = Event()
        self._thread: Thread | None = None
        self._error: BaseException | None = None
        self._baseline: ObservedInstrumentState | None = None
        self._pending_positions: dict[
            ActionName,
            _PendingPositionObservation,
        ] = {}

    @property
    def failed(self) -> bool:
        return self._error is not None

    @property
    def error(self) -> BaseException | None:
        return self._error

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise RuntimeError("external instrument state observer failed") from self._error

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("external state observer has already been started")
        self._thread = Thread(
            target=self._run,
            name="external-instrument-state-observer",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, raise_on_failure: bool = True) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.poll_interval_s * 4.0))
            if self._thread.is_alive():
                raise TimeoutError("external state observer did not stop")
        self._flush_pending_positions(force=True)
        if raise_on_failure:
            self.raise_if_failed()

    def request_snapshot(self) -> None:
        """Ask the observer thread to emit a baseline without blocking telemetry."""

        self._snapshot_requested.set()

    def sample_once(self) -> ObservedInstrumentState:
        bias_v = self.status_device.get_bias_v()
        follow_me = self.status_device.get_follow_me_position(
            wait_for_newest_data=False
        )
        shared = (
            None
            if self.gate_snapshot_provider is None
            else self.gate_snapshot_provider()
        )
        if shared is None:
            z_setpoint_a = self.status_device.get_z_setpoint_a()
            z_position_m = self.status_device.get_z_position_m()
            z_controller_on = self.status_device.get_z_controller_on()
            z_controller_status = (
                self.status_device.get_z_controller_status()
            )
            auto_approach_running = (
                self.status_device.get_auto_approach_running()
            )
        else:
            z_setpoint_a = shared.current_setpoint_a
            z_position_m = shared.z_position_m
            z_controller_on = shared.z_controller_on
            z_controller_status = shared.z_controller_status
            auto_approach_running = shared.auto_approach_running
        return ObservedInstrumentState(
            bias_v=bias_v,
            z_setpoint_a=z_setpoint_a,
            z_position_m=z_position_m,
            z_controller_on=z_controller_on,
            z_controller_status=z_controller_status,
            follow_me_x_m=follow_me.x_m,
            follow_me_y_m=follow_me.y_m,
            scan_running=self.status_device.get_scan_status().running,
            bias_spectroscopy_running=(
                self.status_device.get_bias_spectroscopy_status()
            ),
            auto_approach_running=auto_approach_running,
        )

    def emit_snapshot(self) -> ObservedInstrumentState:
        """Persist a reconstructable state baseline at an audit boundary."""

        current = self.sample_once()
        self._emit_snapshot_from_state(current)
        return current

    def _emit_snapshot_from_state(
        self,
        current: ObservedInstrumentState,
    ) -> None:
        state = asdict(current)
        self.event_emitter.observe(
            ActionName.INSTRUMENT_STATE_SNAPSHOT,
            before={},
            after=state,
            changed_fields=tuple(state),
            source="ExternalStateObserver.emit_snapshot",
        )

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                current = self.sample_once()
                previous = self._baseline
                if previous is None:
                    self._baseline = current
                    if current.auto_approach_running:
                        self._observe(
                            ActionName.AUTO_APPROACH_STATE_CHANGE,
                            {"auto_approach_running": None},
                            {"auto_approach_running": True},
                            ("auto_approach_running",),
                        )
                else:
                    self._emit_changes(previous, current)
                    self._baseline = current
                if self._snapshot_requested.is_set():
                    self._snapshot_requested.clear()
                    self._emit_snapshot_from_state(current)
                    self._observe_tip_shaper_parameters(force=True)
                else:
                    self._observe_tip_shaper_parameters()
                self._flush_pending_positions()
                self._stop.wait(self.poll_interval_s)
        except BaseException as exc:
            self._error = exc

    def _observe_tip_shaper_parameters(self, *, force=False) -> None:
        if not self.observe_tip_shaper_parameters:
            return
        if force:
            self._tip_shaper_capture_active = True
        if not self._tip_shaper_capture_active:
            return
        now = time.perf_counter()
        if not force and (self._tip_shaper_unavailable or now < self._tip_shaper_next_poll_s):
            return
        if not self._tip_shaper_unavailable:
            try:
                properties = self.status_device.get_tip_shaper_properties()
                self._tip_shaper_snapshot = {"available": True, "parameters": asdict(properties),
                    "observed_utc_ns": time.time_ns(), "scope": "configuration_not_execution"}
            except NanonisCommandError as error:
                self._tip_shaper_snapshot = {"available": False, "parameters": None,
                    "observed_utc_ns": time.time_ns(), "error": str(error)}
                self._tip_shaper_unavailable = True
            # Transport errors remain observer failures; never hide an uncertain
            # or desynchronized connection as a merely unsupported optional API.
            self._tip_shaper_next_poll_s = now + 5.0
        self.event_emitter.observe(ActionName.TIP_SHAPER_PARAMETERS, before={},
            after=self._tip_shaper_snapshot, changed_fields=tuple(self._tip_shaper_snapshot),
            source="ExternalStateObserver.get_tip_shaper_properties")

    def _emit_changes(
        self,
        previous: ObservedInstrumentState,
        current: ObservedInstrumentState,
    ) -> None:
        if not math.isclose(
            previous.bias_v,
            current.bias_v,
            rel_tol=0.0,
            abs_tol=self.bias_tolerance_v,
        ):
            self._observe(
                ActionName.BIAS_SET,
                {"bias_v": previous.bias_v},
                {"bias_v": current.bias_v},
                ("bias_v",),
            )
        if not math.isclose(
            previous.z_setpoint_a,
            current.z_setpoint_a,
            rel_tol=0.0,
            abs_tol=self.setpoint_tolerance_a,
        ):
            self._observe(
                ActionName.Z_SETPOINT_SET,
                {"z_setpoint_a": previous.z_setpoint_a},
                {"z_setpoint_a": current.z_setpoint_a},
                ("z_setpoint_a",),
            )
        if (
            previous.z_controller_on != current.z_controller_on
            or previous.z_controller_status is not current.z_controller_status
        ):
            action_name = (
                ActionName.WITHDRAW
                if current.z_controller_status
                in {ZControllerStatus.SAFE_TIP, ZControllerStatus.WITHDRAWING}
                else ActionName.Z_CONTROLLER_STATE_CHANGE
            )
            self._observe(
                action_name,
                {
                    "z_controller_on": previous.z_controller_on,
                    "z_controller_status": previous.z_controller_status,
                },
                {
                    "z_controller_on": current.z_controller_on,
                    "z_controller_status": current.z_controller_status,
                },
                ("z_controller_on", "z_controller_status"),
            )
        if (
            not current.z_controller_on
            and not math.isclose(
                previous.z_position_m,
                current.z_position_m,
                rel_tol=0.0,
                abs_tol=self.position_tolerance_m,
            )
        ):
            self._queue_position_observation(
                ActionName.Z_POSITION_CHANGE,
                {"z_position_m": previous.z_position_m},
                {"z_position_m": current.z_position_m},
                ("z_position_m",),
            )
        follow_me_changed = (
            not math.isclose(
                previous.follow_me_x_m,
                current.follow_me_x_m,
                rel_tol=0.0,
                abs_tol=self.position_tolerance_m,
            )
            or not math.isclose(
                previous.follow_me_y_m,
                current.follow_me_y_m,
                rel_tol=0.0,
                abs_tol=self.position_tolerance_m,
            )
        )
        if current.scan_running and not previous.scan_running:
            # Preserve a settled/pending manual move that happened before the
            # scan, then ignore XY read-back once the scanner owns the path.
            self._flush_pending_position(ActionName.FOLLOW_ME_MOVE)
        if (
            not previous.scan_running
            and not current.scan_running
            and follow_me_changed
        ):
            self._queue_position_observation(
                ActionName.FOLLOW_ME_MOVE,
                {
                    "x_m": previous.follow_me_x_m,
                    "y_m": previous.follow_me_y_m,
                },
                {
                    "x_m": current.follow_me_x_m,
                    "y_m": current.follow_me_y_m,
                },
                ("follow_me_x_m", "follow_me_y_m"),
            )
        if previous.scan_running != current.scan_running:
            self._observe(
                ActionName.SCAN_STATE_CHANGE,
                {"scan_running": previous.scan_running},
                {"scan_running": current.scan_running},
                ("scan_running",),
            )
        if (
            previous.bias_spectroscopy_running
            != current.bias_spectroscopy_running
        ):
            self._observe(
                ActionName.BIAS_SPECTROSCOPY_STATE_CHANGE,
                {
                    "bias_spectroscopy_running": (
                        previous.bias_spectroscopy_running
                    )
                },
                {
                    "bias_spectroscopy_running": (
                        current.bias_spectroscopy_running
                    )
                },
                ("bias_spectroscopy_running",),
            )
        if previous.auto_approach_running != current.auto_approach_running:
            self._observe(
                ActionName.AUTO_APPROACH_STATE_CHANGE,
                {
                    "auto_approach_running": (
                        previous.auto_approach_running
                    )
                },
                {
                    "auto_approach_running": (
                        current.auto_approach_running
                    )
                },
                ("auto_approach_running",),
            )

    def _queue_position_observation(
        self,
        action_name: ActionName,
        before: dict[str, float],
        after: dict[str, float],
        changed_fields: tuple[str, ...],
    ) -> None:
        now_s = time.perf_counter()
        pending = self._pending_positions.get(action_name)
        self._pending_positions[action_name] = _PendingPositionObservation(
            before=before if pending is None else pending.before,
            after=after,
            changed_fields=changed_fields,
            last_changed_s=now_s,
        )

    def _flush_pending_positions(self, *, force: bool = False) -> None:
        now_s = time.perf_counter()
        ready = [
            action_name
            for action_name, pending in self._pending_positions.items()
            if force
            or now_s - pending.last_changed_s >= self.position_settle_s
        ]
        for action_name in ready:
            self._flush_pending_position(action_name)

    def _flush_pending_position(self, action_name: ActionName) -> None:
        pending = self._pending_positions.pop(action_name, None)
        if pending is None:
            return
        self._observe(
            action_name,
            pending.before,
            pending.after,
            pending.changed_fields,
        )

    def _observe(
        self,
        action_name: ActionName,
        before,
        after,
        changed_fields: tuple[str, ...],
    ) -> None:
        self.event_emitter.observe(
            action_name,
            before=before,
            after=after,
            changed_fields=changed_fields,
            source="ExternalStateObserver",
        )


def _opens_action_audit(event: InstrumentActionEvent) -> bool:
    if event.action_name is ActionName.COARSE_APPROACH_SESSION_START:
        return True
    if event.action_name is not ActionName.AUTO_APPROACH_STATE_CHANGE:
        return False
    payload = event.arguments.value
    if not isinstance(payload, dict):
        return False
    after = payload.get("after")
    return (
        isinstance(after, dict)
        and after.get("auto_approach_running") is True
    )
