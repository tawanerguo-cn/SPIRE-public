from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from data_layer import (
    ExperimentMetadata,
    estimate_recording_storage,
    inspect_nanonis_tab_file,
    reconcile_saved_points,
    recording_root_free_bytes,
    recording_tree_size_bytes,
)
from runtime_layer import (
    load_recording_settings,
    run_experiment_recording,
    set_recording_switch,
)
from runtime_layer.recording_control import DEFAULT_RECORDING_CONFIG
from runtime_layer.recording_supervisor import (recording_lease, read_health, write_health,
    service_directory, service_status, request_service_stop, supervise_recording)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SPiRE durable I/X/Y/Z experiment recorder.")
    parser.add_argument("--config", type=Path, default=DEFAULT_RECORDING_CONFIG)
    parser.add_argument(
        "--local-config",
        type=Path,
        help="Optional override config; defaults beside the selected --config.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Show the master switch, recording root, and quota.")
    subparsers.add_parser("enable", help="Permit future recorder starts; does not start recording.")
    subparsers.add_parser("disable", help="Prevent future starts and gracefully stop the supervisor's worker.")
    subparsers.add_parser("stop-service", help="Gracefully stop the background service; enable permits a later restart.")
    supervise = subparsers.add_parser("supervise", help="Keep bounded recording workers running with retry and a watchdog.")
    supervise.add_argument("--live", action="store_true")
    supervise.add_argument("--segment-seconds", type=float, default=3600)

    estimate = subparsers.add_parser("estimate", help="Estimate conservative telemetry + Data Logger storage.")
    estimate.add_argument("--hours", type=float, default=24.0)
    estimate.add_argument("--rate-hz", type=float, default=50.0)
    estimate.add_argument(
        "--active-percent",
        type=float,
        default=20.0,
        help="Fraction of wall time with a confirmed near-sample segment.",
    )

    record = subparsers.add_parser("record", help="Start foreground durable telemetry recording.")
    record.add_argument("--live", action="store_true", help="Connect to live Nanonis; fake is the default.")
    record.add_argument("--duration-s", type=float, help="Stop after this duration; omit for Ctrl+C.")
    record.add_argument("--run-id")
    record.add_argument("--sample-id")
    record.add_argument("--sample-description")
    record.add_argument("--sample-preparation")
    record.add_argument("--operator")
    record.add_argument(
        "--sample-tag",
        action="append",
        default=[],
        metavar="KEY=VALUE",
    )
    record.add_argument("--notes")
    record.add_argument("--defer-review", action="store_true", help="Seal raw data now; generate derived summaries on demand.")
    record.add_argument("--health-file", type=Path, help=argparse.SUPPRESS)
    record.add_argument("--stop-file", type=Path, help=argparse.SUPPRESS)

    review = subparsers.add_parser("review", help="Generate an evidence-linked recording summary without connecting to hardware.")
    review.add_argument("database", type=Path)
    review.add_argument("--output", type=Path, required=True, help="New report directory; existing directories are refused.")
    review.add_argument("--read-root", type=Path, action="append", default=[], help="Explicit roots allowed for raw scan/spectrum verification.")

    verify = subparsers.add_parser("verify", help="Ingest a Nanonis tab file and reconcile saved points.")
    verify.add_argument("path", type=Path)
    verify.add_argument("--saved-points", type=int, required=True)

    archive = subparsers.add_parser("archive", help="Create or append one agent experiment archive; no instrument or recorder changes.")
    destination = archive.add_mutually_exclusive_group(required=True)
    destination.add_argument("--root", type=Path, help="Create a named experiment in the existing RunStore layout.")
    destination.add_argument("--database", type=Path, help="Append to an existing offline experiment archive.")
    archive.add_argument("--experiment-id")
    archive.add_argument("--sample-id")
    archive.add_argument("--sample-description")
    archive.add_argument("--identity-source", help="Who established sample identity; directory names are not identity.")
    archive.add_argument("--tip-state-id", required=True, help="Explicit state label, or unknown; not inferred from time proximity.")
    archive.add_argument("--purpose", required=True)
    archive.add_argument("--artifact", action="append", default=[], metavar="ROLE=PATH")
    archive.add_argument("--parent-artifact-id", action="append", default=[])
    args = parser.parse_args(argv)

    if args.command == "archive":
        from runtime_layer.experiment_recording import archive_experiment
        artifacts = []
        for value in args.artifact:
            role, separator, path = value.partition('=')
            if not separator or not role or not path or not Path(path).is_file():
                parser.error("each --artifact must be ROLE=existing_file_path")
            artifacts.append((role, path))
        try:
            result = archive_experiment(root=args.root, database_path=args.database,
                experiment_id=args.experiment_id,
                sample_id=args.sample_id, sample_description=args.sample_description,
                identity_source=args.identity_source, purpose=args.purpose,
                artifacts=artifacts, tip_state_id=args.tip_state_id,
                parent_artifact_ids=args.parent_artifact_id)
        except ValueError as error:
            parser.error(str(error))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0

    if args.command == "review":
        from runtime_layer.experiment_recording import review_experiment_recording
        result = review_experiment_recording(args.database, args.output, read_roots=args.read_root)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0

    if args.command == "enable":
        local_config = args.local_config or args.config.with_name("recording.local.toml")
        print(f"recording_enabled=true state_file={set_recording_switch(True, local_config)}")
        settings = load_recording_settings(args.config, args.local_config)
        (service_directory(settings.root) / "stop").unlink(missing_ok=True)
        return 0
    if args.command == "disable":
        local_config = args.local_config or args.config.with_name("recording.local.toml")
        print(f"recording_enabled=false state_file={set_recording_switch(False, local_config)}")
        return 0
    if args.command == "estimate":
        return _print_estimate(args.hours, args.rate_hz, args.active_percent)
    if args.command == "verify":
        return _verify(args.path, args.saved_points)

    settings = load_recording_settings(args.config, args.local_config)
    if args.command == "stop-service":
        request_service_stop(settings.root)
        print("service_stop_requested=true")
        return 0
    if args.command == "supervise":
        return supervise_recording(live=args.live, segment_seconds=args.segment_seconds,
            config_path=args.config, local_path=args.local_config)
    if args.command == "status":
        free = recording_root_free_bytes(settings.root)
        used = recording_tree_size_bytes(settings.root)
        remaining = max(0, settings.storage_quota_bytes - used)
        print(f"enabled={str(settings.enabled).lower()}")
        print("service=" + json.dumps(service_status(settings.root), ensure_ascii=False))
        print(f"root={settings.root}")
        print(f"root_exists={str(settings.root.exists()).lower()}")
        print(f"used_gib={used / 1024**3:.3f}")
        print(f"storage_quota_gib={settings.storage_quota_gib:g}")
        print(f"storage_warning_gib={settings.storage_warning_gib:g}")
        print(f"quota_remaining_gib={remaining / 1024**3:.3f}")
        print(
            "quota_warning="
            f"{str(used >= settings.storage_warning_bytes).lower()}"
        )
        print(f"free_gib={free / 1024**3:.2f}")
        print(f"signals={','.join(settings.signals)}")
        print(f"gate_mode={settings.gate_mode}")
        print(
            "action_audit_start_mode="
            f"{settings.action_audit_start_mode}"
        )
        print(
            "observe_external_state="
            f"{str(settings.observe_external_state).lower()}"
        )
        print(
            "observe_gui_actions="
            f"{str(settings.observe_gui_actions).lower()}"
        )
        print(
            "gui_action_binding_profile="
            f"{settings.gui_action_binding_profile}"
        )
        print(
            "status_connection="
            + (
                "shared_with_telemetry"
                if settings.status_port is None
                else f"dedicated_port:{settings.status_port}"
            )
        )
        print(
            "data_logger_enabled="
            f"{str(settings.data_logger_enabled).lower()}"
        )
        print(
            "coarse_xy_button_observation="
            + (
                "physical_click_direction_and_count_only"
                if settings.observe_gui_actions
                else "disabled"
            )
        )
        print("coarse_motion_position_or_step_readback=not_required")
        return 0
    if not settings.enabled:
        print(
            "recording is disabled; run `spire-recorder --config <recording.toml> enable` first",
            file=sys.stderr,
        )
        return 2

    def progress(payload):
        if args.health_file:
            if payload.get("phase") == "finalizing":
                payload["acquisition_stopped_utc_ns"] = time.time_ns()
            write_health(args.health_file, payload)

    with recording_lease(settings.root):
        if args.health_file:
            write_health(args.health_file, {"phase": "connecting"})
        try:
            result = run_experiment_recording(
                settings,
                live=args.live,
                duration_s=args.duration_s,
                run_id=args.run_id,
                experiment_metadata=ExperimentMetadata(
                    sample_id=args.sample_id,
                    description=args.sample_description,
                    preparation=args.sample_preparation,
                    operator=args.operator,
                    tags=_parse_tags(args.sample_tag),
                    notes=args.notes,
                ),
                quota_warning_callback=lambda message: print(message, file=sys.stderr),
                stop_requested=None if args.stop_file is None else args.stop_file.exists,
                progress_callback=progress if args.health_file else None,
                generate_review=not args.defer_review,
            )
        except BaseException as error:
            if args.health_file:
                write_health(args.health_file, read_health(args.health_file) |
                    {"phase": "failed", "error": f"{type(error).__name__}: {error}"})
            raise
        if args.health_file:
            write_health(args.health_file, read_health(args.health_file) |
                {"phase": "finished", "status": result.status, "samples": result.samples,
                 "gap_events": result.gap_events, "missed_deadlines": result.missed_deadlines,
                 "duration_completed": result.duration_completed,
                 "component_failures": result.component_failures,
                 "review_error": result.review_error})
    print(f"run_id={result.run_id}")
    print(f"status={result.status}")
    print(f"samples={result.samples}")
    print(f"gap_events={result.gap_events}")
    print(f"missed_deadlines={result.missed_deadlines}")
    print(f"elapsed_s={result.elapsed_s:.3f}")
    print(f"segments={result.segments}")
    print(f"action_events={result.action_events}")
    print(
        "action_audit_started="
        f"{str(result.action_audit_started).lower()}"
    )
    print(f"database={result.database_path}")
    print(f"review={result.review_path}")
    if result.review_error:
        print(f"review_error={result.review_error}", file=sys.stderr)
    return 0 if result.status in {"complete", "stopped"} and result.review_error is None else 3


def _parse_tags(values: list[str]) -> dict[str, str] | None:
    if not values:
        return None
    tags: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise ValueError("--sample-tag must use KEY=VALUE")
        tags[key.strip()] = value.strip()
    return tags


def _print_estimate(hours: float, rate_hz: float, active_percent: float) -> int:
    estimate = estimate_recording_storage(
        hours=hours,
        samples_per_s=rate_hz,
        active_fraction=active_percent / 100.0,
    )
    print(f"hours={estimate.hours:g}")
    print(f"rate_hz={estimate.samples_per_s:g}")
    print(f"samples={estimate.sample_count}")
    print(f"active_percent={estimate.active_fraction * 100:g}")
    print(f"numeric_payload_mib={estimate.numeric_payload_bytes / 1024**2:.2f}")
    print(f"telemetry_store_gib={estimate.telemetry_store_bytes / 1024**3:.3f}")
    print(f"nanonis_file_gib={estimate.nanonis_file_bytes / 1024**3:.3f}")
    print(f"combined_gib={estimate.combined_bytes / 1024**3:.3f}")
    return 0


def _verify(path: Path, saved_points: int) -> int:
    summary = inspect_nanonis_tab_file(path)
    result = reconcile_saved_points(saved_points, summary)
    print(f"path={summary.path}")
    print(f"sha256={summary.sha256}")
    print(f"data_rows={summary.data_rows}")
    print(f"malformed_rows={summary.malformed_rows}")
    print(f"saved_points={result.saved_points}")
    print(f"complete={str(result.complete).lower()}")
    print(f"difference={result.difference}")
    if result.reason:
        print(f"reason={result.reason}")
    return 0 if result.complete else 4
if __name__ == "__main__":
    raise SystemExit(main())
