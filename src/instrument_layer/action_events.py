from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from threading import Lock
import time
from typing import Any, Callable, Protocol, TypeVar
import uuid

from .models import to_jsonable


class ActionName(str, Enum):
    COARSE_APPROACH_SESSION_START = "coarse_approach_session_start"
    COARSE_RETRACT_SESSION_END = "coarse_retract_session_end"
    INSTRUMENT_STATE_SNAPSHOT = "instrument_state_snapshot"
    BIAS_SET = "bias_set"
    Z_SETPOINT_SET = "z_setpoint_set"
    WITHDRAW = "withdraw"
    FOLLOW_ME_MOVE = "follow_me_move"
    FOLLOW_ME_STOP = "follow_me_stop"
    SCAN_START = "scan_start"
    SCAN_PAUSE = "scan_pause"
    SCAN_RESUME = "scan_resume"
    SCAN_FRAME_SET = "scan_frame_set"
    SCAN_SPEED_SET = "scan_speed_set"
    DRIFT_COMPENSATION_SET = "drift_compensation_set"
    DRIFT_COMPENSATION_STOP = "drift_compensation_stop"
    SCAN_ACQUIRE = "scan_acquire"
    LOCAL_SCAN_ACQUIRE = "local_scan_acquire"
    SCAN_STOP = "scan_stop"
    SCAN_STATE_CHANGE = "scan_state_change"
    BIAS_SPECTROSCOPY_ACQUIRE = "bias_spectroscopy_acquire"
    BIAS_SPECTROSCOPY_STOP = "bias_spectroscopy_stop"
    BIAS_SPECTROSCOPY_LOCK_IN_CONFIGURE = "bias_spectroscopy_lock_in_configure"
    BIAS_SPECTROSCOPY_STATE_CHANGE = "bias_spectroscopy_state_change"
    BIAS_PULSE = "bias_pulse"
    Z_POKE = "z_poke"
    TIP_SHAPER = "tip_shaper"
    TIP_SHAPER_PARAMETERS = "tip_shaper_parameters"
    Z_POSITION_CHANGE = "z_position_change"
    Z_CONTROLLER_STATE_CHANGE = "z_controller_state_change"
    Z_CONTROLLER_SET = "z_controller_set"
    Z_HOME = "z_home"
    AUTO_APPROACH_STATE_CHANGE = "auto_approach_state_change"
    AUTO_APPROACH_START = "auto_approach_start"
    AUTO_APPROACH_STOP = "auto_approach_stop"
    ATOM_TRACKING_STOP = "atom_tracking_stop"
    COARSE_XY_MOVE = "coarse_xy_move"
    COARSE_Z_RETRACT = "coarse_z_retract"
    MOTOR_STOP = "motor_stop"
    ALL_MOTION_STOP = "all_motion_stop"
    DATA_LOG_CONFIGURE = "data_log_configure"
    DATA_LOG_START = "data_log_start"
    DATA_LOG_STOP = "data_log_stop"
    WORKFLOW_PAUSE = "workflow_pause"
    WORKFLOW_RESUME = "workflow_resume"


class ActionPhase(str, Enum):
    BEFORE = "before"
    AFTER = "after"
    FAILED = "failed"
    OBSERVED = "observed"


class ActionActor(str, Enum):
    SPIRE = "spire"
    OPERATOR = "operator"
    UNKNOWN = "unknown"


class ActionIntent(str, Enum):
    EXECUTE = "execute"
    CONFIGURE = "configure"
    PROTECTIVE = "protective"
    PAUSE = "pause"
    RESUME = "resume"
    START_AUDIT = "start_audit"
    END_AUDIT = "end_audit"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ActionPayload:
    """Canonical JSON payload with a digest even when the inline value is capped."""

    sha256: str
    encoded_bytes: int
    value: Any | None
    truncated: bool


@dataclass(frozen=True)
class InstrumentActionEvent:
    action_id: str
    action_name: ActionName
    phase: ActionPhase
    actor: ActionActor
    intent: ActionIntent
    source: str
    monotonic_ns: int
    utc_ns: int
    arguments: ActionPayload
    result: ActionPayload | None = None
    error_type: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not self.action_id:
            raise ValueError("action_id must not be empty")
        if not self.source:
            raise ValueError("action event source must not be empty")
        if self.monotonic_ns < 0 or self.utc_ns < 0:
            raise ValueError("action event timestamps must be non-negative")
        if self.phase is ActionPhase.FAILED and not self.error_type:
            raise ValueError("failed action events require an error_type")
        if self.phase is not ActionPhase.FAILED and (
            self.error_type is not None or self.error_message is not None
        ):
            raise ValueError("only failed action events may include error details")
        if self.phase is ActionPhase.OBSERVED and (
            self.actor is not ActionActor.UNKNOWN
            or self.intent is not ActionIntent.UNKNOWN
        ):
            raise ValueError(
                "observed state changes must preserve actor=unknown and intent=unknown"
            )


class ActionEventSink(Protocol):
    def __call__(self, event: InstrumentActionEvent) -> None: ...


class ActionAuditRequiredError(RuntimeError):
    """A live non-protective action was refused because no durable audit sink exists."""


T = TypeVar("T")


class ActionEventEmitter:
    """Create correlated before/after/failed events around one typed action."""

    def __init__(
        self,
        sink: ActionEventSink | None = None,
        *,
        monotonic_clock_ns: Callable[[], int] = time.perf_counter_ns,
        utc_clock_ns: Callable[[], int] = time.time_ns,
        inline_payload_limit_bytes: int = 64 * 1024,
    ) -> None:
        if inline_payload_limit_bytes < 1024:
            raise ValueError("inline_payload_limit_bytes must be at least 1024")
        self._sink = sink
        self._lock = Lock()
        self._monotonic_clock_ns = monotonic_clock_ns
        self._utc_clock_ns = utc_clock_ns
        self.inline_payload_limit_bytes = int(inline_payload_limit_bytes)

    @property
    def sink(self) -> ActionEventSink | None:
        with self._lock:
            return self._sink

    def set_sink(self, sink: ActionEventSink | None) -> ActionEventSink | None:
        with self._lock:
            previous = self._sink
            self._sink = sink
        return previous

    def execute(
        self,
        action_name: ActionName,
        *,
        arguments: Any,
        operation: Callable[[], T],
        source: str,
        actor: ActionActor = ActionActor.SPIRE,
        intent: ActionIntent = ActionIntent.EXECUTE,
        require_sink: bool = False,
        fail_open_on_sink_error: bool = False,
    ) -> T:
        sink = self.sink
        if sink is None:
            if require_sink:
                raise ActionAuditRequiredError(
                    f"{action_name.value} requires a durable action-event sink"
                )
            return operation()

        action_id = uuid.uuid4().hex
        argument_payload = build_action_payload(
            arguments,
            inline_limit_bytes=self.inline_payload_limit_bytes,
        )
        try:
            sink(
                self._event(
                    action_id,
                    action_name,
                    ActionPhase.BEFORE,
                    actor,
                    intent,
                    source,
                    argument_payload,
                )
            )
        except Exception:
            if not fail_open_on_sink_error:
                raise
            sink = None
        try:
            result = operation()
        except BaseException as exc:
            if sink is not None:
                try:
                    sink(
                        self._event(
                            action_id,
                            action_name,
                            ActionPhase.FAILED,
                            actor,
                            intent,
                            source,
                            argument_payload,
                            result=(build_action_payload(exc.acquired_evidence,
                                inline_limit_bytes=self.inline_payload_limit_bytes)
                                if hasattr(exc, "acquired_evidence") else None),
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                        )
                    )
                except Exception:
                    if not fail_open_on_sink_error:
                        raise
            raise
        if sink is not None:
            try:
                sink(
                    self._event(
                        action_id,
                        action_name,
                        ActionPhase.AFTER,
                        actor,
                        intent,
                        source,
                        argument_payload,
                        result=build_action_payload(
                            result,
                            inline_limit_bytes=self.inline_payload_limit_bytes,
                        ),
                    )
                )
            except Exception:
                if not fail_open_on_sink_error:
                    raise
        return result

    def record_instantaneous(
        self,
        action_name: ActionName,
        *,
        arguments: Any,
        result: Any,
        source: str,
        actor: ActionActor = ActionActor.SPIRE,
        intent: ActionIntent = ActionIntent.EXECUTE,
        require_sink: bool = False,
    ) -> None:
        self.execute(
            action_name,
            arguments=arguments,
            operation=lambda: result,
            source=source,
            actor=actor,
            intent=intent,
            require_sink=require_sink,
        )

    def observe(
        self,
        action_name: ActionName,
        *,
        before: Any,
        after: Any,
        changed_fields: tuple[str, ...],
        source: str,
    ) -> InstrumentActionEvent | None:
        sink = self.sink
        if sink is None:
            return None
        arguments = build_action_payload(
            {
                "before": before,
                "after": after,
                "changed_fields": changed_fields,
            },
            inline_limit_bytes=self.inline_payload_limit_bytes,
        )
        event = self._event(
            uuid.uuid4().hex,
            action_name,
            ActionPhase.OBSERVED,
            ActionActor.UNKNOWN,
            ActionIntent.UNKNOWN,
            source,
            arguments,
        )
        sink(event)
        return event

    def _event(
        self,
        action_id: str,
        action_name: ActionName,
        phase: ActionPhase,
        actor: ActionActor,
        intent: ActionIntent,
        source: str,
        arguments: ActionPayload,
        *,
        result: ActionPayload | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> InstrumentActionEvent:
        return InstrumentActionEvent(
            action_id=action_id,
            action_name=action_name,
            phase=phase,
            actor=actor,
            intent=intent,
            source=source,
            monotonic_ns=int(self._monotonic_clock_ns()),
            utc_ns=int(self._utc_clock_ns()),
            arguments=arguments,
            result=result,
            error_type=error_type,
            error_message=error_message,
        )


def build_action_payload(
    value: Any,
    *,
    inline_limit_bytes: int = 64 * 1024,
) -> ActionPayload:
    jsonable = to_jsonable(value)
    encoded = json.dumps(
        jsonable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return ActionPayload(
        sha256=hashlib.sha256(encoded).hexdigest(),
        encoded_bytes=len(encoded),
        value=jsonable if len(encoded) <= inline_limit_bytes else None,
        truncated=len(encoded) > inline_limit_bytes,
    )
