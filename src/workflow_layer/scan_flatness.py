"""Source-bound early-row replay using the existing SXM observation path."""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json

import numpy as np

from analysis_layer.scan_flatness import FlatnessSettings, screen_flatness
from analysis_layer.scan_images import process_scan_image, render_scan_observation
from knowledge_base import load_tip_conditioning_knowledge
from .scan_observation import observe_saved_scan


def assess_saved_scan_flatness(path, *, profile: str, first_lines: int | None = 12,
                               roi: list[int] | None = None, render: bool = True):
    profiles = load_tip_conditioning_knowledge()["flatness_screen_profiles"]
    if profile not in profiles:
        raise ValueError("unknown flatness profile; select an explicitly scoped profile")
    configuration = profiles[profile]
    settings = FlatnessSettings(**configuration["settings"])
    if first_lines is not None and (type(first_lines) is not int or first_lines < 4):
        raise ValueError("first_lines must be an integer >= 4 or null for all contiguous acquired lines")
    observation = observe_saved_scan(path, processing="raw")
    if observation.channel_unit != "m":
        raise ValueError("flatness requires a height channel in metres")
    bounds = [0, observation.rows, 0, observation.columns] if roi is None else list(roi)
    if (len(bounds) != 4 or any(type(v) is not int for v in bounds)
            or not 0 <= bounds[0] < bounds[1] <= observation.rows
            or not 0 <= bounds[2] < bounds[3] <= observation.columns):
        raise ValueError("roi must be top-origin [r0,r1,c0,c1] inside the full scan")
    identity = json.dumps([observation.source_sha256, profile, first_lines, bounds, configuration], sort_keys=True)
    report = {"analysis_version": "scan-flatness-v1", "assessment_id": "flatness-" + sha256(identity.encode()).hexdigest()[:24],
              "source": observation.summary(), "profile": profile, "profile_status": configuration["status"],
              "profile_scope": configuration["scope"], "requested_first_lines": first_lines,
              "requested_roi": bounds, "authorizes_live_action": False, "automatic_tip_acceptance": False,
              "live_buffer_access": False, "processing": "Raw plus independent fast-axis per-row least-squares linear subtraction; not a plane fit or smoothing."}
    if observation.backward is None:
        return report | {"verdict": "insufficient_evidence", "reason": "Retrace channel missing."}, None
    # Full-width completion defines the acquisition watermark, even if a local
    # ROI was requested. Never leap over a missing/partial row into future data.
    paired_rows = (np.isfinite(observation.raw) & np.isfinite(observation.backward)).all(axis=1)
    order = range(observation.rows-1, -1, -1) if observation.scan_direction == "up" else range(observation.rows)
    selected = []
    for row in order:
        if not paired_rows[row]:
            break
        if bounds[0] <= row < bounds[1]:
            selected.append(row)
            if first_lines is not None and len(selected) == first_lines:
                break
    report["selected_rows_acquisition_order"] = selected
    if len(selected) < 4:
        return report | {"verdict": "insufficient_evidence", "reason": "Fewer than four contiguous complete paired rows in the requested acquisition prefix."}, None
    r0, r1 = min(selected), max(selected) + 1
    c0, c1 = bounds[2:]
    if c1-c0 < 4:
        return report | {"verdict": "insufficient_evidence", "reason": "Fewer than four columns selected."}, None
    result = screen_flatness(observation.raw[r0:r1, c0:c1], observation.backward[r0:r1, c0:c1], settings=settings)
    for tile in result["tiles"]:
        a, b, c, d = tile["roi"]
        tile["roi"] = [a+r0, b+r0, c+c0, d+c0]
    result["line_boundaries"] = [r+r0 for r in result.get("line_boundaries", [])]
    report.update(result)
    report["selected_roi"] = [r0, r1, c0, c1]
    report["selected_size_nm"] = [(c1-c0)/observation.columns*observation.frame.width_m*1e9,
                                  (r1-r0)/observation.rows*observation.frame.height_m*1e9]
    report["requested_prefix_complete"] = first_lines is None or len(selected) == first_lines
    preview = None
    if render:
        # Mask BEFORE processing. No later row enters the fits or display scale.
        raw, backward = np.full_like(observation.raw, np.nan), np.full_like(observation.backward, np.nan)
        raw[r0:r1, c0:c1] = observation.raw[r0:r1, c0:c1]
        backward[r0:r1, c0:c1] = observation.backward[r0:r1, c0:c1]
        display = replace(observation, raw=raw, backward=backward, valid_mask=np.isfinite(raw),
                          processed=process_scan_image(raw, "line_linear"), processing="line_linear")
        preview = render_scan_observation(display, crop=(r0, r1, c0, c1))
    return report, preview
