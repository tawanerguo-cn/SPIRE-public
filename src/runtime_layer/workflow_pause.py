from __future__ import annotations

from threading import Lock

from instrument_layer import (
    ActionEventEmitter,
    ActionIntent,
    ActionName,
)


class WorkflowPausedError(RuntimeError):
    """A runtime workflow action was rejected while the pause gate was active."""


class WorkflowPauseController:
    """Audited runtime pause state; continuous telemetry is intentionally unaffected."""

    def __init__(self, event_emitter: ActionEventEmitter | None = None) -> None:
        self.action_events = event_emitter or ActionEventEmitter()
        self._paused = False
        self._reason: str | None = None
        self._lock = Lock()

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def pause(self, reason: str) -> None:
        reason = reason.strip()
        if not reason:
            raise ValueError("workflow pause reason must not be empty")

        def apply_pause() -> dict[str, object]:
            with self._lock:
                if self._paused:
                    raise WorkflowPausedError("workflow is already paused")
                self._paused = True
                self._reason = reason
            return {"paused": True, "reason": reason}

        self.action_events.execute(
            ActionName.WORKFLOW_PAUSE,
            arguments={"reason": reason},
            operation=apply_pause,
            source="WorkflowPauseController.pause",
            intent=ActionIntent.PAUSE,
        )

    def resume(self, reason: str) -> None:
        reason = reason.strip()
        if not reason:
            raise ValueError("workflow resume reason must not be empty")

        def apply_resume() -> dict[str, object]:
            with self._lock:
                if not self._paused:
                    raise RuntimeError("workflow is not paused")
                previous_reason = self._reason
                self._paused = False
                self._reason = None
            return {
                "paused": False,
                "reason": reason,
                "previous_pause_reason": previous_reason,
            }

        self.action_events.execute(
            ActionName.WORKFLOW_RESUME,
            arguments={"reason": reason},
            operation=apply_resume,
            source="WorkflowPauseController.resume",
            intent=ActionIntent.RESUME,
        )

    def require_running(self) -> None:
        with self._lock:
            if self._paused:
                raise WorkflowPausedError(
                    f"workflow actions are paused: {self._reason}"
                )
