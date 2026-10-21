"""Assemble saved-run evidence, deterministic annotations and exclusive report artifacts."""
from pathlib import Path
from contextlib import closing
import time

from analysis_layer.recording_review import (summarize_recording, render_recording_summary,
                                            saved_recording_file_quality, tip_shaper_observation_context)
from data_layer.recording import read_recording_snapshot
from data_layer.recording_signals import iter_recording_signals
from analysis_layer.tip_shaper_recognition import recognize_tip_shaper
from knowledge_base.recording_pattern import load_tip_shaper_recording_pattern
from data_layer import read_nanonis_dat, read_nanonis_sxm
from data_layer.atom_plan import write_json_artifact


def inspect_recording(database_path, *, read_roots=()):
    started = time.perf_counter()
    snapshot = read_recording_snapshot(database_path)
    review = summarize_recording(snapshot)
    parameter_snapshots = []
    for action in snapshot["actions"]:
        if action["action_name"] == "tip_shaper_parameters" and not action.get("arguments", {}).get("truncated"):
            state = (action.get("arguments", {}).get("value") or {}).get("after", {})
            parameter_snapshots.append(state | {"monotonic_ns": action["monotonic_ns"],
                                               "action_event_id": action["action_event_id"]})
    watermark = snapshot["watermarks"]["signal_samples"]
    with closing(iter_recording_signals(database_path,
                 maximum_seq=-1 if watermark is None else watermark)) as signals:
        recognition = recognize_tip_shaper(signals, load_tip_shaper_recording_pattern(),
            parameter_snapshots=parameter_snapshots,
            observation_context=tip_shaper_observation_context(snapshot["actions"], review["episodes"]))
    # Keep machine inference separate from recorded commands and operator facts.
    review["tip_shaper_recognition"] = recognition
    for item in recognition["detections"]:
        context = [action for action in snapshot["actions"]
                   if item["start_monotonic_ns"] - 500_000_000 <= action["monotonic_ns"] <= item["end_monotonic_ns"] + 500_000_000]
        item["nearby_action_event_ids"] = [action["action_event_id"] for action in context]
        review["timeline"].append({"kind": "tip_shaper_inferred", "utc_ns": item["start_utc_ns"],
            "priority": "normal", "inference_id": item["id"], "detection": item})
    review["timeline"].sort(key=lambda item: item["utc_ns"])
    if not recognition["complete"]:
        review["attention"].append({"kind": "tip_shaper_recognition_budget_exceeded",
                                   "details": {"examined_samples": recognition["examined_samples"]}})
    roots = tuple(Path(root).resolve() for root in read_roots)
    remaining_bytes = 64 * 1024**2
    artifacts = []
    for artifact in snapshot["artifacts"]:
        item = {key: artifact[key] for key in ("artifact_id", "artifact_kind", "source_path", "sha256", "observed_utc_ns")}
        item["check"] = "not_checked"
        source = Path(artifact["source_path"]).resolve()
        try:
            if not any(source.is_relative_to(root) for root in roots):
                item["check"] = "outside_read_roots"
            elif artifact["artifact_kind"] not in {"scan_raw", "spectrum_raw"}:
                item["check"] = "not_scan_or_spectrum"
            elif not source.is_file():
                item["check"] = "missing"
            elif source.stat().st_size > min(remaining_bytes, 32 * 1024**2):
                item["check"] = "file_check_budget_exceeded"
            else:
                remaining_bytes -= source.stat().st_size
                saved = read_nanonis_sxm(source) if artifact["artifact_kind"] == "scan_raw" else read_nanonis_dat(source)
                item["current_sha256"] = saved.sha256
                if saved.sha256 != artifact["sha256"]:
                    item["check"] = "source_changed"
                else:
                    item["check"] = "verified"
                    item["quality"] = saved_recording_file_quality(saved, run_started_utc_ns=snapshot["run"]["started_utc_ns"])
        except (OSError, ValueError, KeyError) as error:
            item.update(check="unreadable", error=str(error))
        artifacts.append(item)
        quality = item.get("quality", {})
        if (item["check"] not in {"verified", "not_scan_or_spectrum"} or quality.get("all_channels_finite") is False
                or quality.get("acquisition_predates_run") is True):
            review["attention"].append({"kind": "artifact_quality", "artifact_id": item["artifact_id"],
                                        "source_path": item["source_path"], "check": item["check"], "quality": quality})
    review["artifacts"] = artifacts
    review["generation_elapsed_s"] = time.perf_counter() - started
    return review


def publish_recording_review(database_path, directory, *, read_roots=()):
    review = inspect_recording(database_path, read_roots=read_roots)
    destination = Path(directory).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    annotations = review.pop("action_annotations")
    write_json_artifact(destination / "annotations.json", {"analysis_version": review["analysis_version"],
        "run_id": review["run_id"], "source": review["source"], "annotations": annotations})
    review["annotation_path"] = str(destination / "annotations.json")
    write_json_artifact(destination / "summary.json", review)
    with (destination / "summary.md").open("x", encoding="utf-8") as stream:
        stream.write(render_recording_summary(review))
    return review | {"summary_path": str(destination / "summary.json"), "report_path": str(destination / "summary.md")}


def compact_recording_review(review, *, maximum_items=100):
    """Bound the MCP response, retain all attention and counts in the full artifact."""
    value = {key: val for key,val in review.items() if key != "action_annotations"}
    value["background_groups"] = [{key: val for key,val in group.items() if key != "action_event_ids"}
                                  for group in review["background_groups"]]
    value["artifacts"] = [{"artifact_id": item["artifact_id"], "source_path": item["source_path"],
                            "check": item["check"], "all_channels_finite": item.get("quality", {}).get("all_channels_finite"),
                            "acquisition_predates_run": item.get("quality", {}).get("acquisition_predates_run"),
                            "finite_points": {name: f"{q['finite_count']}/{q['point_count']}" for name,q in item.get("quality", {}).get("channels", {}).items()}}
                          for item in review["artifacts"]]
    value["attention"] = [{key: val for key,val in item.items() if key != "quality"} |
                           ({"all_channels_finite": item["quality"].get("all_channels_finite"),
                             "acquisition_predates_run": item["quality"].get("acquisition_predates_run")} if "quality" in item else {})
                           for item in review["attention"]]
    value["response_truncation"] = {}
    detection_count = len(value.get("tip_shaper_recognition", {}).get("detections", []))
    if detection_count > maximum_items:
        value["tip_shaper_recognition"] = value["tip_shaper_recognition"] | {
            "detections": value["tip_shaper_recognition"]["detections"][:maximum_items]}
        value["response_truncation"]["tip_shaper_detections"] = {"shown": maximum_items, "total": detection_count}
    for field in ("timeline", "attention", "artifacts", "episodes", "background_groups", "segments"):
        count = len(value[field])
        if count > maximum_items:
            value[field] = value[field][:maximum_items]
            value["response_truncation"][field] = {"shown": maximum_items, "total": count, "full_data": review.get("summary_path")}
    return value
