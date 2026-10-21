from __future__ import annotations

from dataclasses import replace

from analysis_layer import TipProximityPolicy
from data_layer import ExperimentMetadata
from instrument_layer import (
    FakeNanonisBackend,
    NanonisDevice,
    WindowsDesktopWindowLocator,
    WindowsGlobalMouseClickSource,
    load_connection_config,
)
from knowledge_base import load_gui_action_binding_profile
from workflow_layer import (
    ActionAuditStartMode,
    ApproachEndController,
    ApproachStartController,
    GuiActionObserver,
    TelemetryRecorder,
    TelemetryRecordingResult,
    TipRecordingGateMonitor,
)

from .recording_control import RecordingSettings
from .workflow_pause import WorkflowPauseController


def review_experiment_recording(database_path, output_directory, *, read_roots=()):
    """One runtime assembly shared by the recorder CLI and the agent surface."""
    from workflow_layer.recording_review import publish_recording_review, compact_recording_review
    return compact_recording_review(publish_recording_review(database_path, output_directory, read_roots=read_roots))


def archive_experiment(*, root=None, database_path=None, experiment_id=None,
        sample_id=None, sample_description=None, identity_source=None,
        tip_state_id, purpose, artifacts=(), parent_artifact_ids=()):
    """Dispatch offline creation/append without assembling a live recorder."""
    from workflow_layer.experiment_archive import (
        create_experiment_archive, register_experiment_artifacts,
    )
    if (root is None) == (database_path is None):
        raise ValueError("provide exactly one of root and database_path")
    if root is not None:
        if parent_artifact_ids:
            raise ValueError("new experiments have no parent artifacts; register sources first")
        result = create_experiment_archive(root, experiment_id=experiment_id,
            sample_id=sample_id, sample_description=sample_description,
            identity_source=identity_source, purpose=purpose)
        database_path = result['database_path']
    elif any(v is not None for v in (experiment_id, sample_id, sample_description, identity_source)):
        raise ValueError("existing sample identity is immutable; do not redefine it while appending")
    return register_experiment_artifacts(database_path, artifacts,
        tip_state_id=tip_state_id, purpose=purpose, parent_artifact_ids=parent_artifact_ids)


def run_experiment_recording(
    settings: RecordingSettings,
    *,
    live: bool,
    duration_s: float | None,
    run_id: str | None,
    experiment_metadata: ExperimentMetadata | None = None,
    approach_start_controller: ApproachStartController | None = None,
    approach_end_controller: ApproachEndController | None = None,
    workflow_pause_controller: WorkflowPauseController | None = None,
    quota_warning_callback=None,
    stop_requested=None,
    progress_callback=None,
    generate_review: bool = True,
) -> TelemetryRecordingResult:
    """Assemble devices and workflows without exposing layer internals to the CLI."""

    if not settings.enabled:
        raise RuntimeError("recording master switch is disabled")

    if live:
        if settings.instrument_profile.status != "reviewed":
            raise RuntimeError(
                "live recording requires a reviewed instrument profile; "
                "draft profiles are limited to offline and fake rehearsals"
            )
        connection = load_connection_config()
        if settings.status_port == connection.port:
            raise ValueError(
                "recording status_port must differ from the telemetry port"
            )
        if settings.data_logger_enabled and settings.status_port is None:
            raise ValueError(
                "live Data Logger requires a commissioned status_port "
                "separate from telemetry"
            )
        telemetry_device = NanonisDevice(config=connection, live=True)
        status_connection = (
            connection
            if settings.status_port is None
            else replace(connection, port=settings.status_port)
        )
        status_device = (
            (
                telemetry_device
                if settings.status_port is None
                else NanonisDevice(config=status_connection, live=True)
            )
            if settings.gate_mode == "near_sample"
            else None
        )
        external_state_device = (
            (
                status_device
                if status_device is not None
                else (
                    telemetry_device
                    if settings.status_port is None
                    else NanonisDevice(
                        config=status_connection,
                        live=True,
                    )
                )
            )
            if settings.observe_external_state
            else None
        )
        data_logger_device = (
            status_device or external_state_device or telemetry_device
            if settings.data_logger_enabled
            else None
        )
    else:
        telemetry_device = NanonisDevice(backend=_recording_fake_backend())
        status_device = (
            NanonisDevice(
                backend=FakeNanonisBackend(
                    current_a=-48e-12,
                    z_setpoint_a=48e-12,
                )
            )
            if settings.gate_mode == "near_sample"
            else None
        )
        external_state_device = (
            NanonisDevice(backend=FakeNanonisBackend())
            if settings.observe_external_state
            else None
        )
        data_logger_device = (
            telemetry_device if settings.data_logger_enabled else None
        )

    try:
        # The current Nanonis session is runtime state, not a portable lab fact.
        # A copied station profile must never redirect recording to an old PC's
        # session directory (or prevent startup because that directory is absent).
        artifact_session_path = telemetry_device.get_session_path() if live else None
        if live and not artifact_session_path:
            raise RuntimeError("Nanonis did not report an active session path")
        if live and settings.observe_gui_actions:
            gui_profile = load_gui_action_binding_profile(
                settings.gui_action_binding_profile
            )
            gui_action_observer = GuiActionObserver(
                WindowsGlobalMouseClickSource(),
                WindowsDesktopWindowLocator(
                    allowed_windows={
                        (binding.window_title, binding.window_class)
                        for binding in gui_profile.bindings
                    }
                ),
                gui_profile,
            )
        else:
            gui_action_observer = None
        gate = (
            TipRecordingGateMonitor(
                status_device,
                policy=TipProximityPolicy(
                    minimum_effective_current_a=settings.minimum_effective_current_a,
                    minimum_setpoint_fraction=settings.minimum_setpoint_fraction,
                    maximum_setpoint_fraction=settings.maximum_setpoint_fraction,
                    minimum_z_limit_clearance_fraction=(
                        settings.minimum_z_limit_clearance_fraction
                    ),
                    minimum_z_limit_clearance_m=settings.minimum_z_limit_clearance_m,
                    arrival_hold_s=settings.arrival_hold_s,
                    arrival_minimum_samples=settings.arrival_minimum_samples,
                    far_noise_current_ceiling_a=(
                        settings.far_noise_current_ceiling_a
                    ),
                    far_noise_window_s=settings.far_noise_window_s,
                    far_noise_minimum_samples=settings.far_noise_minimum_samples,
                    far_noise_hold_s=settings.far_noise_hold_s,
                ),
                poll_interval_s=settings.gate_poll_interval_s,
            )
            if status_device is not None
            else None
        )
        recorder = TelemetryRecorder(
            telemetry_device,
            settings.root,
            signal_names=settings.signals,
            storage_quota_bytes=settings.storage_quota_bytes,
            storage_warning_bytes=settings.storage_warning_bytes,
            quota_warning_callback=quota_warning_callback,
            gate_monitor=gate,
            data_logger_device=data_logger_device,
            experiment_metadata=experiment_metadata,
            external_state_device=external_state_device,
            external_state_poll_interval_s=(
                settings.external_state_poll_interval_s
            ),
            observe_tip_shaper_parameters=(not live or settings.status_port is not None),
            gui_action_observer=gui_action_observer,
            artifact_session_path=artifact_session_path,
            action_audit_start_mode=ActionAuditStartMode(
                settings.action_audit_start_mode
            ),
            workflow_action_emitters=(
                tuple(
                    emitter
                    for emitter in (
                        None
                        if approach_start_controller is None
                        else approach_start_controller.action_events,
                        None
                        if workflow_pause_controller is None
                        else workflow_pause_controller.action_events,
                        None
                        if approach_end_controller is None
                        else approach_end_controller.action_events,
                    )
                    if emitter is not None
                )
            ),
        )
        return recorder.record(duration_s=duration_s, run_id=run_id,
            stop_requested=stop_requested, progress_callback=progress_callback,
            generate_review=generate_review)
    finally:
        devices = {
            id(device): device
            for device in (
                telemetry_device,
                status_device,
                external_state_device,
                data_logger_device,
            )
            if device is not None
        }
        for device in devices.values():
            device.close()


def _recording_fake_backend() -> FakeNanonisBackend:
    names = [f"Signal {index}" for index in range(88)]
    names[0], names[28], names[29], names[30] = (
        "Current (A)",
        "X (m)",
        "Y (m)",
        "Z (m)",
    )
    names[86], names[87] = "LI Demod 1 X (A)", "LI Demod 1 Y (A)"
    return FakeNanonisBackend(
        signal_names=names,
        signal_values={
            0: -48e-12,
            28: -481.74e-9,
            29: -442.61e-9,
            30: 128e-9,
            86: 2e-12,
            87: -3e-12,
        },
    )
