"""Small agent-facing tool surface. No live backend or instrument construction."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
import uuid
import json
import os

from analysis_layer import AtomDetectorSettings
from data_layer.atom_plan import read_atom_plan, write_json_artifact
from workflow_layer.atom_campaign import rehearse_atom_plan, inspect_atom_campaign
from workflow_layer.atom_targeting import prepare_atom_plan
from workflow_layer.scan_observation import inspect_saved_scan
from workflow_layer.spectrum_observation import inspect_saved_spectrum, render_spectrum
from workflow_layer.agent_observation import render_observation, export_agent_observation
from workflow_layer.coarse_relocation import CoarseRelocationPlan, rehearse_coarse_relocation
from workflow_layer.instrument_services import describe_instrument_service, inspect_workflow_run
from workflow_layer.signal_window import inspect_signal_window
from workflow_layer.tip_reference_verification import quantify_basic_tip_quality
from workflow_layer.scan_flatness import assess_saved_scan_flatness
from .experiment_recording import review_experiment_recording


@dataclass(frozen=True)
class ToolOutput:
    data: dict
    images: tuple[bytes, ...] = ()
    is_error: bool = False


class SPMTools:
    """Explicit local file roots, exclusive derived outputs and serialized rehearsals."""

    def __init__(self, *, read_roots: list[str | Path], output_root: str | Path):
        self.read_roots = tuple(Path(path).resolve() for path in read_roots)
        if not self.read_roots or any(not path.is_dir() for path in self.read_roots):
            raise ValueError("provide existing explicit read roots")
        self.output_root = Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._execution_lock = Lock()

    def _read(self, path, *, generated=False) -> Path:
        resolved = Path(path).resolve()
        roots = (self.output_root,) if generated else self.read_roots
        if not any(resolved.is_relative_to(root) for root in roots) or not resolved.is_file():
            raise ValueError("file is outside the configured roots or does not exist")
        return resolved

    def _new_path(self, category: str, suffix="") -> Path:
        directory = self._output_directory(category)
        return directory / (uuid.uuid4().hex + suffix)

    def _output_directory(self, category: str) -> Path:
        directory = (self.output_root / category).resolve()
        if not directory.is_relative_to(self.output_root):
            raise ValueError("output directory escapes configured root")
        directory.mkdir(exist_ok=True)
        return directory

    def save_images(self, result: ToolOutput) -> list[str]:
        """Persist transport previews using the shared confined output policy."""
        paths = []
        for payload in result.images:
            destination = self._new_path("previews", ".png")
            with destination.open("xb") as stream:
                stream.write(payload)
            paths.append(str(destination))
        return paths

    @contextmanager
    def _rehearsal(self):
        if not self._execution_lock.acquire(blocking=False):
            raise RuntimeError("a fake workflow is already running")
        try:
            yield self._output_directory("runs")
        finally:
            self._execution_lock.release()

    def observe_scan(self, path: str, processing: str = "plane", mode: str = "joint",
                     threshold_sigma: float = 3.0, minimum_separation_pixels: float = 5.0,
                     smoothing_radius_pixels: int = 1, background_radius_pixels: int = 12) -> ToolOutput:
        """Observe a saved SXM. Modes: metrics, image, joint. No atom identity or move authorization."""
        if mode not in {"metrics", "image", "joint"}:
            raise ValueError("mode must be metrics, image or joint")
        observation, candidates, summary = inspect_saved_scan(self._read(path), processing=processing,
            detector_settings=AtomDetectorSettings(threshold_sigma=threshold_sigma,
                minimum_separation_pixels=minimum_separation_pixels,
                smoothing_radius_pixels=smoothing_radius_pixels, background_radius_pixels=background_radius_pixels))
        preview = render_observation(observation, candidates=candidates if mode == "joint" else ()) if mode != "metrics" else None
        bundle = export_agent_observation(observation, summary, self._new_path("observations"), preview)
        if mode == "image":
            summary = observation.summary() | {"authorizes_live_action": False}
        return ToolOutput(summary | {"mode": mode, "bundle_directory": str(bundle)}, (preview,) if preview else ())

    def view_scan_region(self, path: str, observation_id: str, row_start: int, row_stop: int,
                         column_start: int, column_stop: int, processing: str = "plane") -> ToolOutput:
        """View a crop with full-image pixel coordinates; requires the expected source observation id."""
        observation, _, _ = inspect_saved_scan(self._read(path), processing=processing)
        if observation.observation_id != observation_id:
            raise ValueError("observation changed; select from a current observation")
        crop = (row_start, row_stop, column_start, column_stop)
        preview = render_observation(observation, crop=crop)
        return ToolOutput(observation.summary() | {"crop": crop, "authorizes_live_action": False}, (preview,))

    def quantify_tip_quality(self, material: str, scan_path: str, signal_path: str | None = None,
                             background_roi: list[int] | None = None, poke_roi: list[int] | None = None,
                             conditions: str = "Saved scan; working point from native header",
                             poke_depth_m: float | None = None, calibration_path: str | None = None) -> ToolOutput:
        """Offline scan brief; optional signal report and [r0,r1,c0,c1] background/poke ROIs.

        Without background_roi, all acquired pixels are measured (features included).
        Unprovided current/poke evidence stays unknown; metrics never imply acceptance.
        """
        scan = self._read(scan_path)
        signal = self._read(signal_path) if signal_path is not None else None
        request = {'material':material, 'scan':str(scan), 'signal_window':str(signal) if signal else None,
                   'background_roi':background_roi, 'poke_roi':poke_roi,
                   'conditions':conditions, 'poke_depth_m':poke_depth_m}
        if calibration_path:
            request['calibration'] = json.loads(self._read(calibration_path).read_text(encoding='utf-8'))
        result = quantify_basic_tip_quality(request, evidence_root=os.path.commonpath([scan.parent,signal.parent]) if signal else scan.parent)
        destination = write_json_artifact(self._new_path('tip_quality', '.json'), result)
        return ToolOutput({key:result[key] for key in ('material','purpose','status','quantitative','issues','brief')} |
                          {'report_path':str(destination), 'authorizes_live_action':False})

    def assess_scan_flatness(self, path: str, profile: str, first_lines: int | None = 12,
                             roi: list[int] | None = None, mode: str = "joint") -> ToolOutput:
        """Screen saved raw/retrace terrain; first acquired lines, not first display rows.

        Explicit station profile required. Null first_lines uses all contiguous
        paired rows. ROI is top-origin [r0,r1,c0,c1]. Metrics or joint (PNG) mode.
        Verdict is an offline heuristic; no chemical cleanliness or tip acceptance.
        """
        if mode not in {"metrics", "joint"}:
            raise ValueError("mode must be metrics or joint")
        report, preview = assess_saved_scan_flatness(self._read(path), profile=profile,
            first_lines=first_lines, roi=roi, render=mode == "joint")
        destination = write_json_artifact(self._new_path("flatness", ".json"), report)
        brief = {key: value for key, value in report.items() if key != "tiles"}
        brief["tile_counts"] = {kind: sum(t["kind"] == kind for t in report.get("tiles", []))
                               for kind in ("flat", "terrace", "rough", "retrace_uncertain")}
        return ToolOutput(brief | {"report_path": str(destination)}, (preview,) if preview else ())

    def prepare_atom_plan(self, path: str, observation_id: str, selections: list[dict],
                          excluded_regions: list[list[int]] | None = None, processing: str = "plane") -> ToolOutput:
        """Save explicit full-grid selections (row,column,selected_by,rationale) as a fake-only plan."""
        observation, _, _ = inspect_saved_scan(self._read(path), processing=processing)
        if observation.observation_id != observation_id:
            raise ValueError("observation changed; cannot reuse old pixel annotations")
        plan = prepare_atom_plan(observation, selections, excluded_regions=excluded_regions or ())
        destination = write_json_artifact(self._new_path("plans", ".json"), plan.to_dict())
        preview = render_observation(observation, targets=plan.targets)
        return ToolOutput(plan.to_dict() | {"plan_path": str(destination)}, (preview,))

    def inspect_spectrum(self, path: str, channel: str, axis_channel: str = "Bias calc (V)",
                         library_path: str | None = None, conditions: dict[str, str] | None = None,
                         mode: str = "joint", maximum_distance: float = 0.1,
                         minimum_margin: float = 0.02) -> ToolOutput:
        """Inspect an explicit saved channel and bias axis. Unsupported/ambiguous matches stay unknown."""
        if mode not in {"metrics", "image", "joint"}:
            raise ValueError("mode must be metrics, image or joint")
        summary, spectrum = inspect_saved_spectrum(self._read(path), channel=channel, axis_channel=axis_channel,
            library_path=self._read(library_path) if library_path else None, conditions=conditions,
            maximum_distance=maximum_distance, minimum_margin=minimum_margin)
        destination = write_json_artifact(self._new_path("spectra", ".json"), summary)
        images = (render_spectrum(spectrum, channel=channel, axis_channel=axis_channel),) if mode != "metrics" else ()
        if mode == "image":
            summary = {key: summary[key] for key in ("source_path", "source_sha256", "selected_channel", "axis_channel", "notes")}
        return ToolOutput(summary | {"report_path": str(destination), "mode": mode, "authorizes_live_action": False}, images)

    def run_fake_atom_plan(self, plan_path: str, recipe: dict | None = None,
                           maximum_atoms: int = 20, maximum_duration_s: float = 300.0) -> ToolOutput:
        """Rehearse saved targets with FakeNanonisBackend; persist raw spectra and events in RunStore."""
        plan = read_atom_plan(self._read(plan_path, generated=True))
        self._read(plan.source_path)
        with self._rehearsal() as root:
            result = rehearse_atom_plan(plan, root, recipe_values=recipe,
                          maximum_atoms=maximum_atoms, maximum_duration_s=maximum_duration_s)
        # Full acquisitions stay in the durable artifact, avoiding context bloat.
        if "atom_map" in result:
            atom_map = result.pop("atom_map")
            result["atoms"] = [{k: a[k] for k in ("candidate_id", "x_m", "y_m", "classification",
                              "classification_confidence", "classification_reason")} for a in atom_map["atoms"]]
            result["atom_map_path"] = str(Path(result["run_directory"]) / "atom_map.json")
        return ToolOutput(result, is_error=result.get("status") == "failed")

    def inspect_atom_run(self, database_path: str, after_event_id: int = 0,
                         include_spectra: bool = False) -> ToolOutput:
        """Read durable campaign progress and optionally raw spectra, including failed/partial runs."""
        return ToolOutput(inspect_atom_campaign(self._read(database_path, generated=True),
                          after_event_id=after_event_id, include_spectra=include_spectra))

    def inspect_recording(self, database_path: str) -> ToolOutput:
        """Summarize a saved or active recorder DB; annotate repeated process states and verify allowed artifacts."""
        return ToolOutput(review_experiment_recording(self._read(database_path), self._new_path("recording_reviews"), read_roots=self.read_roots))

    def describe_instrument_service(self) -> ToolOutput:
        """Read the shared capability, SafetyGate and tool registries; never connect hardware."""
        return ToolOutput(describe_instrument_service())

    def inspect_signal_window(self, database_path: str, start_seq: int, stop_seq: int,
                              minimum_duration_s: float, maximum_sample_gap_s: float,
                              expected_current_a: float | None = None,
                              current_full_scale_a: float | None = None,
                              z_limits_m: list[float] | None = None,
                              analysis_limits: dict[str, float] | None = None) -> ToolOutput:
        """Measure committed I/Z rows. Missing criteria produce metrics only, never action permission."""
        result = inspect_signal_window(self._read(database_path), start_seq, stop_seq,
            minimum_duration_s=minimum_duration_s, maximum_sample_gap_s=maximum_sample_gap_s,
            expected_current_a=expected_current_a, current_full_scale_a=current_full_scale_a,
            z_limits_m=z_limits_m, analysis_limits=analysis_limits)
        path = write_json_artifact(self._new_path("signal_windows", ".json"), result)
        return ToolOutput(result | {"report_path": str(path)})

    def run_fake_coarse_relocation(self, xy_direction: str, retract_steps: int, xy_steps: int,
                                  group: int, approach_timeout_ms: int = 300000) -> ToolOutput:
        """Rehearse retreat -> XY -> approach. Explicit inputs; no pulse or live option."""
        plan = CoarseRelocationPlan(xy_direction, retract_steps, xy_steps, group, approach_timeout_ms)
        with self._rehearsal() as root:
            result = rehearse_coarse_relocation(plan, root)
        return ToolOutput(result, is_error=result["status"] == "failed")

    def inspect_workflow_run(self, database_path: str, after_event_id: int = 0) -> ToolOutput:
        """Read committed workflow events, including failed stages. Never replay actions."""
        return ToolOutput(inspect_workflow_run(self._read(database_path, generated=True),
                                              after_event_id=after_event_id))
