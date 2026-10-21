"""Offline, explainable terrain screening. No chemical or tip-state inference."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import math

import numpy as np


@dataclass(frozen=True)
class FlatnessSettings:
    """Explicit analysis settings, never hardware bounds or acceptance criteria."""

    tile_columns: int
    tile_rows: int
    residual_limit_pm: float
    retrace_limit_pm: float
    step_minimum_pm: float
    minimum_flat_fraction: float

    def __post_init__(self):
        for value in (self.tile_columns, self.tile_rows):
            if type(value) is not int or value < 4:
                raise ValueError("tile dimensions must be integers >= 4")
        for value in (self.residual_limit_pm, self.retrace_limit_pm, self.step_minimum_pm):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("height limits must be finite and positive")
        for value in (self.minimum_flat_fraction,):
            if isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError("area fractions must be finite and in (0, 1]")


def _rms(values):
    return float(np.sqrt(np.mean(np.square(values))))


def _fit_residual(z, extra=None):
    y, x = np.indices(z.shape, dtype=float)
    columns = [x.ravel(), y.ravel(), np.ones(z.size)]
    if extra is not None:
        columns.append(extra.ravel())
    design = np.column_stack(columns)
    coefficients = np.linalg.lstsq(design, z.ravel(), rcond=None)[0]
    return _rms(z.ravel() - design @ coefficients), coefficients


def _terrain_tile(z, settings):
    """Compare a plane with a plane plus one coherent sharp step.

    A step is permitted only when adjacent lines contain a same-sign sharp
    edge, both terraces have support, and removing the step explains >= 80%
    of residual power. Broad mounds are not silently removed as tilt/steps.
    """
    residual, _ = _fit_residual(z)
    if residual <= settings.residual_limit_pm:
        return residual, False
    best = residual
    step = False
    for transpose in (False, True):
        data = z.T if transpose else z
        if data.shape[1] < 8:
            continue
        gradient = np.diff(data, axis=1)
        centered = gradient - np.median(gradient, axis=1, keepdims=True)
        edge = np.argmax(np.abs(centered), axis=1)
        strength = centered[np.arange(len(edge)), edge]
        strong = np.abs(strength) > settings.residual_limit_pm
        if (strong.sum() < max(3, len(edge)//2)
                or abs(np.mean(np.sign(strength[strong]))) < .8):
            continue
        # A connected boundary may cross a tile edge. Fit its trajectory using
        # lines where it is visible, instead of requiring a step in every row.
        rows = np.arange(len(edge))
        trajectory = np.polyfit(rows[strong], edge[strong] + .5, 1)
        if np.max(np.abs(edge[strong] + .5 - np.polyval(trajectory, rows[strong]))) > 2:
            continue
        distance = np.arange(data.shape[1])[None, :] - np.polyval(trajectory, rows)[:, None]
        # Subpixel/finite-width steps are common in wide survey scans. These
        # templates change the model, never smooth or overwrite the raw data.
        for width in (0, .5, 1, 2):
            indicator = (distance > 0).astype(float) if width == 0 else (1 + np.tanh(distance / width)) / 2
            if min(np.mean(indicator < .15), np.mean(indicator > .85)) < .15:
                continue
            candidate, coefficients = _fit_residual(data, indicator)
            if (abs(coefficients[-1]) >= settings.step_minimum_pm and candidate < best
                    and candidate ** 2 <= .2 * residual ** 2):
                best, step = candidate, True
    return best, step


def screen_flatness(forward, backward, *, settings: FlatnessSettings) -> dict:
    """Screen a complete paired rectangle in metres; preserve noisy/ambiguous cases.

    Uses raw paired heights for plane/step models, not the line-flattened
    display. Tiles expose heterogeneous fields rather than selecting one
    quiet pixel. Returns geometrical candidates, never a cleanliness claim.
    """
    f, b = np.asarray(forward, dtype=float), np.asarray(backward, dtype=float)
    if f.ndim != 2 or f.shape != b.shape or not f.size:
        raise ValueError("paired nonempty matrices with identical shapes required")
    if not (np.isfinite(f).all() and np.isfinite(b).all()):
        raise ValueError("screen only complete paired pixels; missing values are not zeros")
    if min(f.shape) < 4:
        return {"verdict": "insufficient_evidence", "reason": "At least four paired rows/columns needed.",
                "tiles": [], "settings": asdict(settings)}
    # Centre before unit conversion to keep nanometre/pm fits well conditioned.
    f = (f - np.median(f)) * 1e12
    b = (b - np.median(b)) * 1e12
    if np.ptp(f) == 0 or np.ptp(b) == 0:
        return {"verdict": "insufficient_evidence", "reason": "Constant channel: cannot distinguish flat from frozen data.",
                "tiles": [], "settings": asdict(settings)}
    paired = (f + b) / 2
    diff = f - b
    # Equal-sized partitions avoid undersized final tiles.
    rs = np.array_split(np.arange(f.shape[0]), max(1, f.shape[0] // settings.tile_rows))
    cs = np.array_split(np.arange(f.shape[1]), max(1, f.shape[1] // settings.tile_columns))
    tiles = []
    for rows in rs:
        for cols in cs:
            roi = (int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1)
            r0, r1, c0, c1 = roi
            # Both directions must look locally planar/terraced; averaging can
            # cancel opposite scan artifacts and produce a false flat field.
            rf, sf = _terrain_tile(f[r0:r1, c0:c1], settings)
            rb, sb = _terrain_tile(b[r0:r1, c0:c1], settings)
            disagreement = _rms(diff[r0:r1, c0:c1])
            roughness = max(rf, rb)
            kind = ("retrace_uncertain" if disagreement > settings.retrace_limit_pm else
                    "rough" if roughness > settings.residual_limit_pm else
                    "terrace" if sf and sb else "flat")
            tiles.append({"roi": list(roi), "kind": kind, "residual_pm": roughness,
                          "retrace_difference_rms_pm": disagreement,
                          "step_in_both_directions": bool(sf and sb), "pixels": (r1-r0)*(c1-c0)})
    fractions = {kind: sum(t["pixels"] for t in tiles if t["kind"] == kind) / f.size
                 for kind in ("flat", "terrace", "rough", "retrace_uncertain")}
    usable = fractions["flat"] + fractions["terrace"]
    # Whole-line height changes can be real terraces OR time-dependent junction
    # changes. Line-linear displays erase them, so retain an explicit raw flag.
    line_deltas = np.diff(np.median(paired, axis=1))
    line_deviations = np.abs(line_deltas - np.median(line_deltas))
    line_noise = 1.4826 * np.median(line_deviations)
    boundaries = (np.flatnonzero(line_deviations > max(settings.step_minimum_pm, 6*line_noise)) + 1).tolist()
    if fractions["retrace_uncertain"] >= .5:
        verdict, reason = "uncertain_retrace", "Directional disagreement dominates; do not infer surface dirt from this scan."
    elif (usable < settings.minimum_flat_fraction
          and fractions["terrace"] > 0):
        verdict, reason = "terraced_needs_local_check", "Corresponding steps explain part of the relief; other tiles remain unresolved. Inspect raw/linear views and a terrace interior, rather than rejecting the field as dirt."
    elif usable < settings.minimum_flat_fraction:
        verdict, reason = "rough_or_unstable", "Widespread nonplanar or inconsistent tiles; choose another region before tip assessment."
    elif fractions["rough"] + fractions["retrace_uncertain"] > 0:
        verdict, reason = "mixed", "Flat areas coexist with features/artifacts; verify a clear terrace and the entire intended scan window."
    elif boundaries:
        verdict, reason = "flat_with_line_boundary", "Terrace interiors look flat; full-line boundaries need raw/time evidence before attribution."
    elif fractions["terrace"] > 0:
        verdict, reason = "terraced_candidate", "Sharp corresponding steps separate approximately planar interiors."
    else:
        verdict, reason = "flat_candidate", "Both raw directions are locally planar within the selected screening settings."
    return {"verdict": verdict, "reason": reason, "area_fractions": fractions,
            "flat_or_terraced_fraction": usable, "line_boundaries": boundaries,
            "raw_range_pm": float(np.ptp(paired)), "retrace_difference_rms_pm": _rms(diff),
            "tiles": tiles, "settings": asdict(settings),
            "scope": "Selected acquired rectangle only; geometrical screening, not chemical cleanliness, future rows or tip acceptance."}
