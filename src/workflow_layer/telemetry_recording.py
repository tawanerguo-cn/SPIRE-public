from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
import math
from pathlib import Path
from threading import Lock
import time
from typing import Callable

from data_layer import (
    ArtifactKind,
    ArtifactLinkStatus,
    DurableRunWriter,
    ExperimentArtifact,
    ExperimentMetadata,
    RunStore,
    RunStoreActionEventSink,
    SessionArtifactObserver,
    TelemetrySample,
    inspect_nanonis_tab_file,
    latest_completed_action_id,
    recording_root_free_bytes,
    recording_tree_size_bytes,
)
from instrument_layer import (
    ActionEventEmitter,
    ActionName,
    NanonisDevice,
    SignalReference,
)
from data_layer.recording import MIN_RECORDING_FREE_BYTES
from .action_audit import (
    ActionAuditStartMode,
    ExternalStateObserver,
    LatchedActionEventSink,
)
from .recording_gate import TipRecordingGateMonitor
from .data_logger_recording import (
    DataLoggerSegmentCapture,
    DataLoggerSegmentController,
)
from .gui_action_observer import GuiActionObserver

@dataclass(frozen=True)
class TelemetryRecordingResult:
    run_id: str
    run_dir: Path
    database_path: Path
    samples: int
    gap_events: int
    missed_deadlines: int
    status: str
    elapsed_s: float
    segments: int
    action_events: int
    action_audit_started: bool
    action_events_dropped_before_start: int
    action_events_dropped_after_close: int
    action_audit_closed_reason: str | None
    artifacts: int
    artifact_events_dropped_outside_audit: int
    duration_completed: bool = False
    component_failures: dict[str, str] = field(default_factory=dict)
    review_path: str | None = None
    review_error: str | None = None


class TelemetryRecorder:
    """Read-only I/X/Y/Z and optional lock-in X/Y mirror in one grouped read."""

    def __init__(
        self,
        device: NanonisDevice,
        root: str | Path,
        *,
        signal_names: tuple[str, ...],
        storage_quota_bytes: int,
        storage_warning_bytes: int,
        quota_check_interval_s: float = 60.0,
        quota_warning_callback: Callable[[str], None] | None = None,
        gate_monitor: TipRecordingGateMonitor | None = None,
        data_logger_device: NanonisDevice | None = None,
        experiment_metadata: ExperimentMetadata | None = None,
        action_devices: Sequence[NanonisDevice] = (),
        workflow_action_emitters: Sequence[ActionEventEmitter] = (),
        external_state_device: NanonisDevice | None = None,
        external_state_poll_interval_s: float = 0.1,
        observe_tip_shaper_parameters: bool = False,
        gui_action_observer: GuiActionObserver | None = None,
        artifact_session_path: str | Path | None = None,
        artifact_poll_interval_s: float = 0.5,
        artifact_stable_polls: int = 2,
        artifact_recursive: bool = False,
        action_audit_start_mode: ActionAuditStartMode = (
            ActionAuditStartMode.TIP_NEAR_SAMPLE
        ),
        # Windows Python <=3.12 monotonic_ns uses a ~15.6 ms tick. Use QPC
        # consistently with action events, gate snapshots and artifact events.
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        utc_clock_ns: Callable[[], int] = time.time_ns,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.device = device
        self.observe_tip_shaper_parameters = observe_tip_shaper_parameters
        self.root = Path(root)
        self.signal_names = tuple(signal_names)
        if len(self.signal_names) not in (4, 6) or len(set(self.signal_names)) != len(self.signal_names):
            raise ValueError("required signals must be I, X, Y, Z, optionally followed by lock-in X, Y (A)")
        self.storage_quota_bytes = int(storage_quota_bytes)
        self.storage_warning_bytes = int(storage_warning_bytes)
        self.quota_check_interval_ns = round(float(quota_check_interval_s) * 1_000_000_000)
        self.quota_warning_callback = quota_warning_callback
        if self.storage_quota_bytes <= 0:
            raise ValueError("storage_quota_bytes must be positive")
        if not 0 < self.storage_warning_bytes < self.storage_quota_bytes:
            raise ValueError(
                "storage_warning_bytes must be positive and below storage_quota_bytes"
            )
        if self.quota_check_interval_ns <= 0:
            raise ValueError("quota_check_interval_s must be positive")
        self.gate_monitor = gate_monitor
        self.data_logger_device = data_logger_device
        self.experiment_metadata = experiment_metadata or ExperimentMetadata()
        self.action_devices = tuple(action_devices)
        self.workflow_action_emitters = tuple(workflow_action_emitters)
        self.external_state_device = external_state_device
        self.external_state_poll_interval_s = float(
            external_state_poll_interval_s
        )
        if self.external_state_poll_interval_s <= 0.0:
            raise ValueError("external_state_poll_interval_s must be positive")
        self.gui_action_observer = gui_action_observer
        self.artifact_session_path = (
            None
            if artifact_session_path is None
            else Path(artifact_session_path).expanduser().resolve()
        )
        self.artifact_poll_interval_s = float(artifact_poll_interval_s)
        self.artifact_stable_polls = int(artifact_stable_polls)
        self.artifact_recursive = bool(artifact_recursive)
        if self.artifact_poll_interval_s <= 0.0:
            raise ValueError("artifact_poll_interval_s must be positive")
        if self.artifact_stable_polls < 2:
            raise ValueError("artifact_stable_polls must be at least 2")
        self.action_audit_start_mode = ActionAuditStartMode(
            action_audit_start_mode
        )
        self._clock_ns = clock_ns
        self._utc_clock_ns = utc_clock_ns
        self._sleep = sleep

    def record(
        self,
        *,
        duration_s: float | None,
        run_id: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        generate_review: bool = True,
    ) -> TelemetryRecordingResult:
        if duration_s is not None and duration_s <= 0.0:
            raise ValueError("duration_s must be positive or None")
        references = self._resolve_required_signals()
        period_s = self.device.get_acquisition_period_s()
        if not math.isfinite(period_s) or period_s <= 0.0:
            raise ValueError(f"invalid Nanonis acquisition period: {period_s!r}")
        period_ns = max(1, round(period_s * 1_000_000_000))
        self._require_quota_available()
        if (
            self.artifact_session_path is not None
            and not self.artifact_session_path.is_dir()
        ):
            raise FileNotFoundError(
                "artifact session path is not a directory: "
                f"{self.artifact_session_path}"
            )
        if any(
            emitter.sink is not None
            for emitter in self._unique_action_emitters(None)
        ):
            raise RuntimeError(
                "action emitter is already bound to another audit sink"
            )
        catalog = {
            str(reference.name): int(reference.index)
            for reference in references
            if reference.name is not None and reference.index is not None
        }
        store = RunStore.create(
            self.root,
            signal_catalog=catalog,
            requested_period_ns=period_ns,
            run_id=run_id,
            experiment_metadata=self.experiment_metadata,
        )

        started_ns = self._clock_ns()
        started_utc_ns = self._utc_clock_ns()
        # Sampling deadlines begin when a segment is ready to acquire, after
        # observer startup and durable audit setup. Keep run setup timestamps.
        next_due_ns: int | None = None
        last_finished_ns: int | None = None
        gap_events = 0
        missed_deadlines = 0
        status = "stopped"
        duration_completed = False
        component_failures: dict[str, BaseException] = {}
        primary_exception: Exception | None = None
        seq = 0
        segment_id = -1 if self.gate_monitor is not None else 0
        segment_count = 0 if self.gate_monitor is not None else 1
        gate_was_open = self.gate_monitor is None
        next_quota_check_ns = started_ns
        quota_warning_emitted = False
        next_progress_ns = started_ns
        writer = DurableRunWriter(store)
        durable_action_sink = RunStoreActionEventSink(writer)
        scoped_action_sink = LatchedActionEventSink(
            durable_action_sink,
            start_mode=self.action_audit_start_mode,
        )
        artifact_events_dropped_outside_audit = [0]
        data_logger_captures: dict[Path, DataLoggerSegmentCapture] = {}
        data_logger_capture_lock = Lock()
        observer_emitter = ActionEventEmitter()

        def persist_artifact(artifact: ExperimentArtifact) -> None:
            if artifact.kind is ArtifactKind.DATA_LOGGER_RAW:
                source_path = artifact.source_path.resolve()
                with data_logger_capture_lock:
                    capture = data_logger_captures.pop(
                        source_path,
                        None,
                    )
                if capture is not None:
                    summary = inspect_nanonis_tab_file(source_path)
                    reconciliation = writer.enqueue_nanonis_file(
                        summary,
                        saved_points=capture.saved_points,
                    )
                    if not reconciliation.complete:
                        with data_logger_capture_lock:
                            component_failures.setdefault(
                                "data_logger_reconciliation",
                                RuntimeError(
                                    "Data Logger saved-points mismatch: "
                                    f"{reconciliation.reason}; "
                                    f"saved={reconciliation.saved_points}, "
                                    f"rows={reconciliation.ingested_rows}"
                                ),
                            )
                    return
            if scoped_action_sink.enabled:
                if artifact.kind is ArtifactKind.SCAN_RAW:
                    producer_action_id = latest_completed_action_id(
                        store.database_path,
                        run_id=store.run_id,
                        action_names=(ActionName.SCAN_START.value,),
                        before_utc_ns=artifact.observed_utc_ns,
                    )
                    if producer_action_id is not None:
                        artifact = replace(
                            artifact,
                            link_status=ArtifactLinkStatus.LINKED,
                            producer_action_id=producer_action_id,
                            metadata={
                                **(artifact.metadata or {}),
                                "association": (
                                    "linked: latest completed typed scan_start "
                                    "precedes the saved scan artifact"
                                ),
                                "action_evidence": "typed_scan_start",
                            },
                        )
                        writer.enqueue_artifact(artifact)
                        return
                artifact_action_name = {
                    ArtifactKind.SCAN_RAW: ActionName.SCAN_ACQUIRE,
                    ArtifactKind.SPECTRUM_RAW: (
                        ActionName.BIAS_SPECTROSCOPY_ACQUIRE
                    ),
                }.get(artifact.kind)
                if artifact_action_name is not None:
                    observed_event = observer_emitter.observe(
                        artifact_action_name,
                        before={"result_file_present": False},
                        after={
                            "result_file_present": True,
                            "artifact_id": artifact.artifact_id,
                            "artifact_kind": artifact.kind.value,
                            "source_path": str(artifact.source_path),
                            "size_bytes": artifact.size_bytes,
                            "sha256": artifact.sha256,
                            "source_mtime_ns": artifact.source_mtime_ns,
                        },
                        changed_fields=("result_file_present",),
                        source="SessionArtifactObserver",
                    )
                    if observed_event is not None:
                        artifact = replace(
                            artifact,
                            link_status=ArtifactLinkStatus.LINKED,
                            producer_action_id=observed_event.action_id,
                            metadata={
                                **(artifact.metadata or {}),
                                "association": (
                                    "linked: typed file classification proves "
                                    "a completed acquisition result"
                                ),
                                "action_evidence": "saved_result_file",
                            },
                        )
                writer.enqueue_artifact(artifact)
            else:
                artifact_events_dropped_outside_audit[0] += 1

        artifact_observer = (
            SessionArtifactObserver(
                self.artifact_session_path,
                persist_artifact,
                poll_interval_s=self.artifact_poll_interval_s,
                stable_polls=self.artifact_stable_polls,
                recursive=self.artifact_recursive,
            )
            if self.artifact_session_path is not None
            else None
        )
        external_state_observer = (
            ExternalStateObserver(
                self.external_state_device,
                observer_emitter,
                observe_tip_shaper_parameters=self.observe_tip_shaper_parameters,
                poll_interval_s=self.external_state_poll_interval_s,
                gate_snapshot_provider=(
                    None
                    if self.gate_monitor is None
                    else self.gate_monitor.latest_snapshot
                ),
            )
            if self.external_state_device is not None
            else None
        )
        data_logger = (
            DataLoggerSegmentController(
                self.data_logger_device,
                signals=self.signal_names,
                run_id=store.run_id,
            )
            if self.data_logger_device is not None
            else None
        )

        def retain_data_logger_capture(
            capture: DataLoggerSegmentCapture | None,
        ) -> None:
            if capture is None:
                return
            if capture.saved_file_path is None:
                component_failures.setdefault(
                    "data_logger",
                    RuntimeError(
                        "Data Logger stopped without a saved file path"
                    ),
                )
                return
            with data_logger_capture_lock:
                data_logger_captures[
                    capture.saved_file_path.resolve()
                ] = capture
        emitters = self._unique_action_emitters(observer_emitter)
        previous_sinks = self._bind_action_emitters(
            emitters,
            scoped_action_sink,
        )
        if (
            self.action_audit_start_mode
            is ActionAuditStartMode.TIP_NEAR_SAMPLE
            and self.gate_monitor is None
        ):
            scoped_action_sink.open(
                cutoff_monotonic_ns=started_ns,
                reason="always_on_telemetry",
            )
        try:
            if self.gui_action_observer is not None:
                try:
                    self.gui_action_observer.start()
                except Exception as exc:
                    component_failures.setdefault(
                        "gui_action_observer",
                        exc,
                    )
                    try:
                        self.gui_action_observer.stop(
                            raise_on_failure=False
                        )
                    except Exception:
                        pass
            if external_state_observer is not None:
                if scoped_action_sink.enabled:
                    external_state_observer.request_snapshot()
                external_state_observer.start()
            if artifact_observer is not None:
                artifact_observer.start()
            if self.gate_monitor is not None:
                self.gate_monitor.start()
            writer.enqueue_event(
                "recorder_started",
                {
                    "period_ns": period_ns,
                    "signals": catalog,
                    "gate_mode": "tip_near_sample" if self.gate_monitor is not None else "always",
                    "storage_quota_bytes": self.storage_quota_bytes,
                    "storage_warning_bytes": self.storage_warning_bytes,
                    "gui_action_observation": (
                        self.gui_action_observer is not None
                    ),
                    "tip_shaper_parameter_observation": (
                        self.observe_tip_shaper_parameters and external_state_observer is not None
                    ),
                    "timing_clock": (
                        {
                            "name": "perf_counter_ns",
                            "implementation": time.get_clock_info("perf_counter").implementation,
                            "resolution_s": time.get_clock_info("perf_counter").resolution,
                        }
                        if self._clock_ns is time.perf_counter_ns
                        else {"name": "injected"}
                    ),
                },
                monotonic_ns=started_ns,
                utc_ns=started_utc_ns,
            )
            if data_logger is not None and self.gate_monitor is None:
                try:
                    data_logger.start_segment(segment_id)
                except Exception as exc:
                    component_failures.setdefault("data_logger", exc)
            while True:
                now_ns = self._clock_ns()
                if progress_callback is not None and now_ns >= next_progress_ns:
                    progress_callback({
                        "phase": "recording", "run_id": store.run_id,
                        "database_path": str(store.database_path),
                        "gate_open": gate_was_open, "samples_enqueued": seq,
                        "samples_committed": writer.samples_committed,
                        "gap_events": gap_events, "missed_deadlines": missed_deadlines,
                        "writer_queue_depth": writer.queue_depth,
                        "writer_failed": writer.failed,
                        "component_failures": {key: str(error) for key, error in component_failures.items()},
                    })
                    next_progress_ns = now_ns + 5_000_000_000
                if duration_s is not None and now_ns - started_ns >= duration_s * 1_000_000_000:
                    duration_completed = True
                    status = (
                        "recording_compromised"
                        if component_failures
                        else "complete"
                    )
                    break
                if stop_requested is not None and stop_requested():
                    status = (
                        "recording_compromised"
                        if component_failures
                        else "stopped"
                    )
                    break
                if now_ns >= next_quota_check_ns:
                    used_bytes = recording_tree_size_bytes(self.root)
                    free_bytes = recording_root_free_bytes(self.root)
                    if free_bytes < MIN_RECORDING_FREE_BYTES:
                        writer.enqueue_event("storage_free_space_low", {"free_bytes": free_bytes},
                            monotonic_ns=now_ns, utc_ns=self._utc_clock_ns())
                        status = "storage_quota_reached"
                        break
                    if used_bytes >= self.storage_quota_bytes:
                        writer.enqueue_event(
                            "storage_quota_reached",
                            {
                                "used_bytes": used_bytes,
                                "storage_quota_bytes": self.storage_quota_bytes,
                            },
                            monotonic_ns=now_ns,
                            utc_ns=self._utc_clock_ns(),
                        )
                        status = (
                            "recording_compromised"
                            if component_failures
                            else "storage_quota_reached"
                        )
                        break
                    if (
                        used_bytes >= self.storage_warning_bytes
                        and not quota_warning_emitted
                    ):
                        payload = {
                            "used_bytes": used_bytes,
                            "storage_warning_bytes": self.storage_warning_bytes,
                            "storage_quota_bytes": self.storage_quota_bytes,
                        }
                        writer.enqueue_event(
                            "storage_quota_warning",
                            payload,
                            monotonic_ns=now_ns,
                            utc_ns=self._utc_clock_ns(),
                        )
                        quota_warning_emitted = True
                        self._notify_quota_warning(writer, payload, now_ns)
                    next_quota_check_ns = now_ns + self.quota_check_interval_ns
                if self.gate_monitor is None:
                    gate_is_open = True
                    gate_transition_reason = "always"
                else:
                    (
                        gate_is_open,
                        gate_transition_reason,
                        gate_transition_monotonic_ns,
                    ) = self.gate_monitor.high_resolution_status()
                if self.gate_monitor is not None:
                    for event in self.gate_monitor.drain_events():
                        writer.enqueue_event(
                            event.event_type,
                            event.payload,
                            monotonic_ns=event.monotonic_ns,
                            utc_ns=event.utc_ns,
                        )
                    if self.gate_monitor.failed:
                        component_failures["recording_gate"] = (
                            self.gate_monitor.error
                            or RuntimeError("recording gate failed")
                        )
                        status = "recording_compromised"
                        break
                if (
                    external_state_observer is not None
                    and external_state_observer.failed
                ):
                    component_failures["external_state_observer"] = (
                        external_state_observer.error
                        or RuntimeError("external state observer failed")
                    )
                    status = "recording_compromised"
                    external_state_observer = None
                if artifact_observer is not None and artifact_observer.failed:
                    component_failures["artifact_observer"] = (
                        artifact_observer.error
                        or RuntimeError("artifact observer failed")
                    )
                    status = "recording_compromised"
                    artifact_observer = None
                if (
                    self.gui_action_observer is not None
                    and self.gui_action_observer.failed
                ):
                    component_failures.setdefault(
                        "gui_action_observer",
                        self.gui_action_observer.error
                        or RuntimeError("GUI action observer failed"),
                    )
                if gate_is_open != gate_was_open:
                    last_finished_ns = None
                    next_due_ns = None
                    if gate_is_open:
                        if (
                            self.action_audit_start_mode
                            is ActionAuditStartMode.TIP_NEAR_SAMPLE
                            and not scoped_action_sink.ever_opened
                        ):
                            scoped_action_sink.open(
                                cutoff_monotonic_ns=(
                                    gate_transition_monotonic_ns
                                ),
                                reason=(
                                    "first_confirmed_tip_near_sample"
                                ),
                            )
                        if external_state_observer is not None:
                            external_state_observer.request_snapshot()
                        segment_id += 1
                        segment_count += 1
                        if data_logger is not None:
                            try:
                                data_logger.start_segment(segment_id)
                            except Exception as exc:
                                component_failures.setdefault(
                                    "data_logger",
                                    exc,
                                )
                        writer.enqueue_event(
                            "high_resolution_segment_started",
                            {
                                "segment_id": segment_id,
                                "reason": gate_transition_reason,
                            },
                            monotonic_ns=now_ns,
                            utc_ns=self._utc_clock_ns(),
                        )
                    else:
                        if data_logger is not None:
                            try:
                                retain_data_logger_capture(
                                    data_logger.stop_segment()
                                )
                            except Exception as exc:
                                component_failures.setdefault(
                                    "data_logger",
                                    exc,
                                )
                        writer.enqueue_event(
                            "high_resolution_segment_stopped",
                            {
                                "segment_id": segment_id,
                                "reason": gate_transition_reason,
                            },
                            monotonic_ns=now_ns,
                            utc_ns=self._utc_clock_ns(),
                        )
                        writer.enqueue_checkpoint()
                    gate_was_open = gate_is_open
                if not gate_is_open:
                    self._sleep(
                        0.05
                        if self.gate_monitor is None
                        else min(0.05, self.gate_monitor.poll_interval_s)
                    )
                    continue
                # Quota checks and segment/audit startup above may have taken
                # time. Never sleep from the stale loop-entry timestamp.
                now_ns = self._clock_ns()
                if duration_s is not None and now_ns - started_ns >= duration_s * 1_000_000_000:
                    duration_completed = True
                    status = "recording_compromised" if component_failures else "complete"
                    break
                if next_due_ns is None:
                    next_due_ns = now_ns
                    writer.enqueue_event(
                        "telemetry_segment_ready",
                        {"segment_id": segment_id, "run_elapsed_ns": now_ns - started_ns},
                        monotonic_ns=now_ns,
                        utc_ns=self._utc_clock_ns(),
                    )
                if now_ns < next_due_ns:
                    self._sleep((next_due_ns - now_ns) / 1_000_000_000)

                call_started_ns = self._clock_ns()
                if duration_s is not None and call_started_ns - started_ns >= duration_s * 1_000_000_000:
                    duration_completed = True
                    status = "recording_compromised" if component_failures else "complete"
                    break
                # This loop already follows Util.AcqPeriodGet. On the commissioned controller,
                # Wait_for_newest_data=1 halves the observed rate, so use a paced latest-value read.
                values = self.device.read_resolved_signals(references, wait_newest=False)
                call_finished_ns = self._clock_ns()
                utc_ns = self._utc_clock_ns()
                flags: list[str] = []
                interval_ns = (
                    None
                    if last_finished_ns is None
                    else call_finished_ns - last_finished_ns
                )
                deadline_misses = max(
                    0,
                    (call_finished_ns - next_due_ns) // period_ns,
                )
                if deadline_misses:
                    flags.append("missed_deadline")
                    missed_deadlines += int(deadline_misses)
                if (
                    deadline_misses
                    or (
                        interval_ns is not None
                        and interval_ns > period_ns * 1.5
                    )
                ):
                    flags.append("late")
                    gap_events += 1
                    writer.enqueue_event(
                        "telemetry_gap",
                        {
                            "previous_finished_monotonic_ns": last_finished_ns,
                            "current_finished_monotonic_ns": call_finished_ns,
                            "requested_period_ns": period_ns,
                            "scheduled_monotonic_ns": next_due_ns,
                            "estimated_missed_deadlines": int(
                                deadline_misses
                            ),
                        },
                        monotonic_ns=call_finished_ns,
                        utc_ns=utc_ns,
                    )
                    component_failures.setdefault(
                        "telemetry_cadence",
                        RuntimeError(
                            "one or more telemetry acquisition deadlines "
                            "were not observed on time"
                        ),
                    )

                numeric = [value.value for value in values]
                errors = [value.error for value in values if value.error is not None]
                if len(numeric) != len(references) or errors or any(value is None or not math.isfinite(value) for value in numeric):
                    writer.enqueue_event(
                        "invalid_telemetry_sample",
                        {"errors": errors, "values": numeric, "signal_names": self.signal_names},
                        monotonic_ns=call_finished_ns,
                        utc_ns=utc_ns,
                    )
                    status = "recording_compromised"
                    break

                writer.enqueue_sample(
                    TelemetrySample(
                        seq=seq,
                        segment_id=segment_id,
                        monotonic_ns=call_finished_ns,
                        utc_ns=utc_ns,
                        call_started_monotonic_ns=call_started_ns,
                        call_finished_monotonic_ns=call_finished_ns,
                        scheduled_monotonic_ns=next_due_ns,
                        current_a=float(numeric[0]),
                        x_m=float(numeric[1]),
                        y_m=float(numeric[2]),
                        z_m=float(numeric[3]),
                        lock_in_x_a=float(numeric[4]) if len(numeric) == 6 else None,
                        lock_in_y_a=float(numeric[5]) if len(numeric) == 6 else None,
                        quality_flags=tuple(flags),
                    )
                )
                seq += 1
                last_finished_ns = call_finished_ns
                # A latest-value API cannot recover intermediate acquisition
                # ticks. Skip expired deadlines instead of issuing catch-up
                # reads that would inflate the apparent sample rate.
                next_due_ns += (int(deadline_misses) + 1) * period_ns

                if writer.at_high_watermark:
                    writer.enqueue_event(
                        "writer_high_watermark",
                        {
                            "queue_depth": writer.queue_depth,
                            "queue_capacity": writer.queue_capacity,
                        },
                        monotonic_ns=call_finished_ns,
                        utc_ns=utc_ns,
                    )
                    status = "recording_compromised"
                    break
                writer.raise_if_failed()

        except KeyboardInterrupt:
            status = (
                "recording_compromised"
                if component_failures
                else "stopped"
            )
        except Exception as exc:
            primary_exception = exc
            status = "failed"
        if progress_callback is not None:
            try:
                progress_callback({"phase": "finalizing", "run_id": store.run_id,
                    "database_path": str(store.database_path), "status": status,
                    "samples_enqueued": seq, "samples_committed": writer.samples_committed})
            except Exception as exc:
                component_failures.setdefault("progress_callback", exc)
        if self.gate_monitor is not None:
            try:
                self.gate_monitor.stop()
            except Exception as exc:
                component_failures.setdefault("recording_gate", exc)
            try:
                for event in self.gate_monitor.drain_events():
                    if not writer.failed:
                        writer.enqueue_event(
                            event.event_type,
                            event.payload,
                            monotonic_ns=event.monotonic_ns,
                            utc_ns=event.utc_ns,
                        )
            except Exception as exc:
                component_failures.setdefault("recording_gate_events", exc)
        if data_logger is not None and data_logger.running:
            try:
                retain_data_logger_capture(data_logger.stop_segment())
            except Exception as exc:
                component_failures.setdefault("data_logger", exc)
        if self.gui_action_observer is not None:
            try:
                self.gui_action_observer.stop(raise_on_failure=False)
            except Exception as exc:
                component_failures.setdefault("gui_action_observer", exc)
            if self.gui_action_observer.error is not None:
                component_failures.setdefault(
                    "gui_action_observer",
                    self.gui_action_observer.error,
                )
            if not writer.failed:
                writer.enqueue_event(
                    "gui_action_observer_summary",
                    {
                        "binding_profile_id": (
                            self.gui_action_observer.profile.profile_id
                        ),
                        "matched_clicks": (
                            self.gui_action_observer.matched_clicks
                        ),
                        "binding_counts": (
                            self.gui_action_observer.binding_counts
                        ),
                        "ignored_clicks": (
                            self.gui_action_observer.ignored_clicks
                        ),
                        "ambiguous_clicks": (
                            self.gui_action_observer.ambiguous_clicks
                        ),
                        "dropped_input_events": (
                            self.gui_action_observer.dropped_input_events
                        ),
                    },
                    monotonic_ns=self._clock_ns(),
                    utc_ns=self._utc_clock_ns(),
                )
        if external_state_observer is not None:
            try:
                external_state_observer.stop(raise_on_failure=False)
            except Exception as exc:
                component_failures.setdefault(
                    "external_state_observer",
                    exc,
                )
            if external_state_observer.error is not None:
                component_failures.setdefault(
                    "external_state_observer",
                    external_state_observer.error,
                )
        if artifact_observer is not None:
            try:
                artifact_observer.stop(raise_on_failure=False)
            except Exception as exc:
                component_failures.setdefault("artifact_observer", exc)
            if artifact_observer.error is not None:
                component_failures.setdefault(
                    "artifact_observer",
                    artifact_observer.error,
                )
        with data_logger_capture_lock:
            unresolved_data_logger_paths = tuple(data_logger_captures)
        if unresolved_data_logger_paths:
            component_failures.setdefault(
                "data_logger_ingest",
                RuntimeError(
                    "Data Logger files were not observed before finalization: "
                    + ", ".join(
                        str(path)
                        for path in unresolved_data_logger_paths
                    )
                ),
            )
        if component_failures and status not in {"failed"}:
            status = "recording_compromised"
        if scoped_action_sink.enabled:
            scoped_action_sink.close(reason="recorder_stopped")
        elif not scoped_action_sink.ever_opened:
            scoped_action_sink.close(
                reason="recorder_stopped_before_tip_near_sample"
            )
        try:
            self._restore_action_emitters(emitters, previous_sinks)
        except Exception as exc:
            component_failures.setdefault("action_emitter_restore", exc)
            status = "failed"
        try:
            if writer.failed:
                raise RuntimeError("durable writer failed before finalization")
            if primary_exception is not None:
                writer.enqueue_event(
                    "recorder_failed",
                    {
                        "error_type": type(primary_exception).__name__,
                        "error_message": str(primary_exception),
                    },
                    monotonic_ns=self._clock_ns(),
                    utc_ns=self._utc_clock_ns(),
                )
            telemetry_continued_components = {
                "external_state_observer",
                "artifact_observer",
                "gui_action_observer",
                "data_logger",
                "data_logger_ingest",
                "data_logger_reconciliation",
                "telemetry_cadence",
            }
            for component_name, failure in component_failures.items():
                writer.enqueue_event(
                    "recording_component_failed",
                    {
                        "component": component_name,
                        "error_type": type(failure).__name__,
                        "error_message": str(failure),
                        "telemetry_continued": (
                            component_name in telemetry_continued_components
                            and primary_exception is None
                        ),
                    },
                    monotonic_ns=self._clock_ns(),
                    utc_ns=self._utc_clock_ns(),
                )
            writer.enqueue_event(
                "action_audit_scope",
                {
                    "start_mode": self.action_audit_start_mode.value,
                    "started": scoped_action_sink.ever_opened,
                    "opened_reason": scoped_action_sink.opened_reason,
                    "closed_reason": scoped_action_sink.closed_reason,
                    "dropped_before_open": (
                        scoped_action_sink.dropped_before_open
                    ),
                    "dropped_after_close": (
                        scoped_action_sink.dropped_after_close
                    ),
                    "artifact_events_dropped_outside_audit": (
                        artifact_events_dropped_outside_audit[0]
                    ),
                },
                monotonic_ns=self._clock_ns(),
                utc_ns=self._utc_clock_ns(),
            )
            writer.enqueue_event(
                "recorder_stopped",
                {
                    "status": status,
                    "samples": seq,
                    "gap_events": gap_events,
                    "missed_deadlines": missed_deadlines,
                    "writer_maximum_queue_depth": (
                        writer.maximum_queue_depth
                    ),
                    "segments": segment_count,
                },
                monotonic_ns=self._clock_ns(),
                utc_ns=self._utc_clock_ns(),
            )
            writer.close()
        except Exception as exc:
            component_failures.setdefault("durable_writer", exc)
            status = "failed"
            if not writer.failed:
                try:
                    writer.close()
                except Exception as close_exc:
                    component_failures.setdefault(
                        "durable_writer_close",
                        close_exc,
                    )
        try:
            action_event_count = store.action_event_count()
            artifact_count = store.artifact_count()
            store.finalize(status, stopped_utc_ns=self._utc_clock_ns())
            elapsed_s = (
                self._clock_ns() - started_ns
            ) / 1_000_000_000
            result = TelemetryRecordingResult(
                run_id=store.run_id,
                run_dir=store.run_dir,
                database_path=store.database_path,
                samples=seq,
                gap_events=gap_events,
                missed_deadlines=missed_deadlines,
                status=status,
                duration_completed=duration_completed,
                component_failures={name: str(error) for name, error in component_failures.items()},
                elapsed_s=elapsed_s,
                segments=segment_count,
                action_events=action_event_count,
                action_audit_started=scoped_action_sink.ever_opened,
                action_events_dropped_before_start=(
                    scoped_action_sink.dropped_before_open
                ),
                action_events_dropped_after_close=(
                    scoped_action_sink.dropped_after_close
                ),
                action_audit_closed_reason=scoped_action_sink.closed_reason,
                artifacts=artifact_count,
                artifact_events_dropped_outside_audit=(
                    artifact_events_dropped_outside_audit[0]
                ),
            )
        finally:
            store.close()
        if not generate_review:
            from data_layer.atom_plan import write_json_artifact
            write_json_artifact(store.run_dir / "recording_review_status.json",
                {"summary_path": None, "error": None, "status": "deferred",
                 "reason": "continuous recording; generate with the review command"})
            if primary_exception is not None:
                raise primary_exception
            return result
        # Derived presentation runs after the durable writer and acquisition have
        # stopped. It must not delay sampling or conceal the primary run failure.
        from .recording_review import publish_recording_review
        try:
            review = publish_recording_review(store.database_path, store.run_dir / "recording_review",
                read_roots=() if self.artifact_session_path is None else (self.artifact_session_path,))
            result = replace(result, review_path=review["summary_path"])
        except Exception as error:
            result = replace(result, review_error=f"{type(error).__name__}: {error}")
        from data_layer.atom_plan import write_json_artifact
        try:
            write_json_artifact(store.run_dir / "recording_review_status.json",
                {"summary_path": result.review_path, "error": result.review_error})
        except Exception as error:
            result = replace(result, review_error=result.review_error or f"Cannot persist review status: {error}")
        if primary_exception is not None:
            raise primary_exception
        return result

    def _unique_action_emitters(
        self,
        observer_emitter: ActionEventEmitter | None,
    ) -> tuple[ActionEventEmitter, ...]:
        candidates = [
            self.device.action_events,
            *(device.action_events for device in self.action_devices),
            *self.workflow_action_emitters,
        ]
        if self.data_logger_device is not None:
            candidates.append(self.data_logger_device.action_events)
        if observer_emitter is not None:
            candidates.append(observer_emitter)
        if self.gui_action_observer is not None:
            candidates.append(self.gui_action_observer.action_events)
        unique: list[ActionEventEmitter] = []
        seen: set[int] = set()
        for emitter in candidates:
            identity = id(emitter)
            if identity not in seen:
                seen.add(identity)
                unique.append(emitter)
        return tuple(unique)

    @staticmethod
    def _bind_action_emitters(
        emitters: Sequence[ActionEventEmitter],
        sink,
    ) -> tuple:
        previous_sinks = tuple(emitter.sink for emitter in emitters)
        if any(previous is not None for previous in previous_sinks):
            raise RuntimeError(
                "action emitter is already bound to another audit sink"
            )
        for emitter in emitters:
            emitter.set_sink(sink)
        return previous_sinks

    @staticmethod
    def _restore_action_emitters(
        emitters: Sequence[ActionEventEmitter],
        previous_sinks: Sequence,
    ) -> None:
        for emitter, previous in zip(emitters, previous_sinks, strict=True):
            emitter.set_sink(previous)

    def _resolve_required_signals(self) -> list[SignalReference]:
        references = self.device.resolve_signals(self.signal_names)
        missing = [reference for reference in references if reference.error or reference.index is None]
        if missing:
            details = ", ".join(f"{item.requested!r}: {item.error}" for item in missing)
            raise ValueError(f"required telemetry signals are unavailable: {details}")
        if len({reference.index for reference in references}) != len(references):
            raise ValueError("required telemetry signals resolved to duplicate indexes")
        return references

    def _require_quota_available(self) -> None:
        used_bytes = recording_tree_size_bytes(self.root)
        if used_bytes >= self.storage_quota_bytes:
            raise OSError(
                f"recording root already uses {used_bytes} bytes, at or above the "
                f"{self.storage_quota_bytes} byte SPiRE quota"
            )

    def _notify_quota_warning(
        self,
        writer: DurableRunWriter,
        payload: dict[str, int],
        monotonic_ns: int,
    ) -> None:
        if self.quota_warning_callback is None:
            return
        used_gib = payload["used_bytes"] / 1024**3
        quota_gib = payload["storage_quota_bytes"] / 1024**3
        try:
            self.quota_warning_callback(
                f"SPiRE recording storage warning: {used_gib:.2f} GiB used "
                f"of {quota_gib:.2f} GiB"
            )
        except Exception as exc:
            writer.enqueue_event(
                "storage_quota_warning_callback_failed",
                {
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
                monotonic_ns=monotonic_ns,
                utc_ns=self._utc_clock_ns(),
            )
