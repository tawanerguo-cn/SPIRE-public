"""Offline rehearsal assembly around the existing single atom spectroscopy loop."""
from __future__ import annotations

from dataclasses import asdict
import time

from analysis_layer import AtomCandidate, LocalMaximumAtomDetector, ReferenceSpectrumClassifier
from data_layer import RunStore, inspect_experiment_artifact
from data_layer.atom_plan import AtomTargetPlan, write_json_artifact
from data_layer.recording import read_run_event_page
from instrument_layer import (BiasSpectroscopyRecipe, BiasSpectroscopyTiming, FakeNanonisBackend,
                              NanonisDevice, ScanFrameData)
from instrument_layer.models import to_jsonable
from .atom_targeting import validate_atom_plan
from .atom_spectroscopy import AtomSpectroscopyWorkflow, AtomSpectroscopyWorkflowConfig


def rehearsal_recipe(values: dict | None = None) -> BiasSpectroscopyRecipe:
    """Clearly synthetic demonstration settings, not a lab spectroscopy protocol."""
    if values is None:
        return BiasSpectroscopyRecipe(-0.01, 0.01, 1, 101, (0, 1),
              BiasSpectroscopyTiming(0.01, 0.0, 0.01, 1.0, 0.001, 0.002, 0.01, 0.01))
    return BiasSpectroscopyRecipe.from_mapping(values)


def inspect_atom_campaign(database_path, *, after_event_id=0, include_spectra=False) -> dict:
    page = read_run_event_page(database_path, after_event_id=after_event_id)
    output = []
    for event in page["events"]:
        if not event["event_type"].startswith("atom_"):
            continue
        payload = event["payload"]
        if not include_spectra:
            if "acquisition" in payload:
                data = payload["acquisition"]["data"]
                payload = {"target_id": payload["target_id"], "channels": data["channel_names"],
                           "points": data["columns"], "raw_spectrum": "available with include_spectra=true"}
            elif "record" in payload:
                payload = {"record": {k: v for k, v in payload["record"].items() if k not in {"acquisition", "move"}},
                           "classification_evidence": payload["classification_evidence"]}
            elif "atom_map" in payload:
                payload = {"completed_targets": len(payload["atom_map"]["atoms"])}
        output.append(event | {"payload": payload})
    return page | {"events": output, "authorizes_live_action": False,
                   "recovery_policy": "Historical evidence only. No automatic resume or repeat acquisition."}


def rehearse_atom_plan(plan: AtomTargetPlan, output_root, *, recipe_values=None,
                       maximum_atoms=20, maximum_duration_s=300.0) -> dict:
    observation = validate_atom_plan(plan)
    recipe = rehearsal_recipe(recipe_values)
    frame = observation.frame
    backend = FakeNanonisBackend(scan_running=False,
              scan_frame=tuple(asdict(frame).values()),
              follow_me_position=(frame.center_x_m, frame.center_y_m))
    device = NanonisDevice(backend=backend)
    store = RunStore.create(output_root, signal_catalog={}, requested_period_ns=0)
    result = {"run_id": store.run_id, "run_directory": str(store.run_dir.resolve()),
              "plan_id": plan.plan_id, "mode": "fake_only", "authorizes_live_action": False,
              "spectrum_origin": "FakeNanonisBackend synthetic signals; no real atom identity evidence"}
    try:
        artifact = write_json_artifact(store.run_dir / "plan.json", plan.to_dict())
        store.append_artifact(inspect_experiment_artifact(artifact, observed_monotonic_ns=time.perf_counter_ns(),
                              observed_utc_ns=time.time_ns(), metadata={"plan_id": plan.plan_id}))
        store.append_artifact(inspect_experiment_artifact(plan.source_path,
                              observed_monotonic_ns=time.perf_counter_ns(), observed_utc_ns=time.time_ns(),
                              metadata={"observation_id": plan.observation_id, "role": "historical_scan_not_live_position"}))
        candidates = tuple(AtomCandidate(t.target_id, t.row, t.column, t.x_m, t.y_m,
                            float(observation.processed[t.row, t.column]), 0.0,
                            position_uncertainty_m=t.pixel_uncertainty_m) for t in plan.targets)
        workflow = AtomSpectroscopyWorkflow(device, LocalMaximumAtomDetector(),
                   ReferenceSpectrumClassifier(channel_name="Current (A)", axis_channel_name="Bias (V)",
                                               maximum_distance=0.1), recipe,
                   AtomSpectroscopyWorkflowConfig(maximum_atoms, maximum_duration_s))
        atom_map = workflow.run(ScanFrameData(0, plan.channel, "forward", plan.rows, plan.columns,
                                             observation.processed, int(observation.scan_direction == "up")),
                               frame=plan.frame, targets=candidates, run_store=store, source=plan.to_dict(),
                               check_ready=lambda: validate_atom_plan(plan))
        result |= {"status": "complete", "atom_map": to_jsonable(atom_map)}
        artifact = write_json_artifact(store.run_dir / "atom_map.json", result)
        store.append_artifact(inspect_experiment_artifact(artifact, observed_monotonic_ns=time.perf_counter_ns(),
                              observed_utc_ns=time.time_ns(), metadata={"plan_id": plan.plan_id}))
    except Exception as error:
        result |= {"status": "failed", "error_type": type(error).__name__, "error": str(error),
                   "recovery": "Read run.sqlite events; acquired spectra are committed before classification. Do not blindly rerun."}
        store.append_event("atom_rehearsal_failed", result, monotonic_ns=time.perf_counter_ns(), utc_ns=time.time_ns())
    except BaseException:
        store.finalize("failed", stopped_utc_ns=time.time_ns())
        raise
    finally:
        try:
            if "status" in result:
                store.finalize(result["status"], stopped_utc_ns=time.time_ns())
        finally:
            store.close()
    return result
