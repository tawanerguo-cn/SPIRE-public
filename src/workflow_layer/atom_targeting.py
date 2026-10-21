"""Bind manual, visual or numerical selections to one saved observation."""
from __future__ import annotations

import math

from analysis_layer import pixel_center_to_xy
from data_layer.atom_plan import AtomTarget, AtomTargetPlan
from data_layer.scan_observation import ScanObservation
from .scan_observation import observe_saved_scan


def prepare_atom_plan(
    observation: ScanObservation, selections: list[dict], *, excluded_regions=(),
) -> AtomTargetPlan:
    regions = tuple(tuple(region) for region in excluded_regions)
    for region in regions:
        if len(region) != 4 or any(type(v) is not int for v in region):
            raise ValueError("excluded regions require four integer pixel coordinates")
        r0, r1, c0, c1 = region
        if not (0 <= r0 < r1 <= observation.rows and 0 <= c0 < c1 <= observation.columns):
            raise ValueError("excluded region is outside scan")
    if not selections:
        raise ValueError("select at least one target explicitly")
    targets = []
    pixels = set()
    for index, selection in enumerate(selections, 1):
        if set(selection) != {"row", "column", "selected_by", "rationale"}:
            raise ValueError("selection requires row, column, selected_by and rationale")
        row, column = selection["row"], selection["column"]
        if type(row) is not int or type(column) is not int:
            raise ValueError("target pixels must be integers in the full observation grid")
        if not (0 < row < observation.rows - 1 and 0 < column < observation.columns - 1):
            raise ValueError("target requires an acquired 3x3 neighbourhood inside scan")
        if not observation.valid_mask[row-1:row+2, column-1:column+2].all():
            raise ValueError("target overlaps unacquired data")
        if any(r0 <= row < r1 and c0 <= column < c1 for r0, r1, c0, c1 in regions):
            raise ValueError("target is in an excluded region")
        if (row, column) in pixels:
            raise ValueError("duplicate target pixel")
        pixels.add((row, column))
        if not all(isinstance(selection[k], str) and selection[k].strip() for k in ("selected_by", "rationale")):
            raise ValueError("selection provenance must be explicit")
        x_m, y_m = pixel_center_to_xy(observation.frame, observation.rows, observation.columns, row, column)
        targets.append(AtomTarget(
            f"target-{index:04d}", row, column, x_m, y_m,
            0.5 * math.hypot(observation.frame.width_m / observation.columns,
                             observation.frame.height_m / observation.rows),
            selection["selected_by"], selection["rationale"],
        ))
    return AtomTargetPlan(
        observation.observation_id, str(observation.source_path), observation.source_sha256,
        observation.channel_name, observation.processing, observation.frame,
        observation.rows, observation.columns, tuple(targets), regions,
    )


def validate_atom_plan(plan: AtomTargetPlan) -> ScanObservation:
    if plan.execution_mode != "fake_only" or plan.schema_version != 1:
        raise ValueError("only offline/fake plans are implemented")
    observation = observe_saved_scan(plan.source_path, channel=plan.channel, processing=plan.processing)
    if observation.observation_id != plan.observation_id or observation.source_sha256 != plan.source_sha256:
        raise ValueError("source scan changed since target selection")
    rebuilt = prepare_atom_plan(observation, [
        {"row": t.row, "column": t.column, "selected_by": t.selected_by, "rationale": t.rationale}
        for t in plan.targets
    ], excluded_regions=plan.excluded_regions)
    if rebuilt != plan:
        raise ValueError("plan geometry or target evidence does not match source scan")
    return observation
