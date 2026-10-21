from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from statistics import median
from typing import Mapping, Protocol, Sequence

from instrument_layer import ScanFrameData


class TipQualityDecision(str, Enum):
    GOOD = "good"
    DEGRADED = "degraded"
    UNCERTAIN = "uncertain"


def score_quantitative_tip_metrics(groups: Mapping, anchors: Mapping) -> dict:
    """Explicit 0/100 anchors, linear interpolation, weakest required metric/group.

    Anchors are analysis configuration, not learned or hardware thresholds.
    Missing metrics/anchors preserve unknown; no universal pass grade is implied.
    """
    if not groups or set(anchors) - set(groups):
        raise ValueError("anchors must name measured groups")
    result = {}
    for group, metrics in groups.items():
        scales = anchors.get(group, {})
        if set(scales) - set(metrics):
            raise ValueError(f"unknown anchored metric in {group}")
        scores = {}
        for name, scale in scales.items():
            good, bad = scale['good'], scale['bad']
            if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in (good,bad)) or good == bad:
                raise ValueError("good and bad anchors must be distinct finite numbers")
            value = metrics[name]
            if value is None:
                scores[name] = None
            elif isinstance(value, bool) or not isinstance(value, (float,int)) or not math.isfinite(value):
                raise ValueError("metric must be finite or unknown")
            else:
                scores[name] = float(100 * min(1, max(0, (value-bad)/(good-bad))))
        complete = bool(scores) and set(scales) == set(metrics) and all(s is not None for s in scores.values())
        result[group] = {"metrics": dict(metrics), "scores": scores,
                         "score": min(scores.values()) if complete else None}
    totals = [group['score'] for group in result.values()]
    return {"groups": result, "score": None if any(v is None for v in totals) else min(totals),
            "formula": "100*clip((value-bad)/(good-bad),0,1); minimum within/across required groups",
            "automatic_acceptance": False}


def aggregate_tip_quality_scores(
    scores: Mapping[str, int | None], *, required_axes: Sequence[str],
) -> int | None:
    """Combine explicit ordinal reviews; never infer an axis from image metrics.

    Unknown required evidence leaves the overall grade unknown. A strong axis
    cannot compensate for a weak axis. This result never authorizes an action.
    """
    if not required_axes or len(set(required_axes)) != len(required_axes):
        raise ValueError("required_axes must be nonempty and unique")
    for value in scores.values():
        if value is not None and (type(value) is not int or not 0 <= value <= 3):
            raise ValueError("axis score must be an integer 0..3 or null")
    values = [scores.get(name) for name in required_axes]
    return None if any(value is None for value in values) else min(values)


@dataclass(frozen=True)
class TipQualityAssessment:
    decision: TipQualityDecision
    score: float
    confidence: float
    reason: str
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class TipQualityThresholds:
    """Lab-reviewed analysis thresholds; these are evidence thresholds, not instrument bounds."""

    minimum_contrast: float
    maximum_line_jump_ratio: float
    minimum_pixels: int = 25
    good_score: float = 0.8


class TipQualityAnalyzer(Protocol):
    def assess(self, scan: ScanFrameData) -> TipQualityAssessment: ...


class DeterministicTipQualityAnalyzer:
    """Explainable baseline using robust contrast and scan-line discontinuity metrics."""

    def __init__(self, thresholds: TipQualityThresholds) -> None:
        if thresholds.minimum_contrast <= 0.0:
            raise ValueError("minimum_contrast must be positive")
        if thresholds.maximum_line_jump_ratio <= 0.0:
            raise ValueError("maximum_line_jump_ratio must be positive")
        if thresholds.minimum_pixels < 4:
            raise ValueError("minimum_pixels must be at least 4")
        if not 0.0 <= thresholds.good_score <= 1.0:
            raise ValueError("good_score must be between 0 and 1")
        self.thresholds = thresholds

    def assess(self, scan: ScanFrameData) -> TipQualityAssessment:
        image = _coerce_image(scan.data)
        flat = [value for row in image for value in row]
        if len(flat) < self.thresholds.minimum_pixels:
            return TipQualityAssessment(
                TipQualityDecision.UNCERTAIN,
                0.0,
                len(flat) / self.thresholds.minimum_pixels,
                "insufficient_pixels",
                {"pixel_count": float(len(flat))},
            )

        ordered = sorted(flat)
        low = ordered[int(0.05 * (len(ordered) - 1))]
        high = ordered[int(0.95 * (len(ordered) - 1))]
        contrast = high - low
        center = median(flat)
        robust_scale = 1.4826 * median(abs(value - center) for value in flat)
        scale = robust_scale if robust_scale > 0.0 else max(contrast, 1e-30)
        row_centers = [median(row) for row in image]
        largest_line_jump = max(
            (abs(right - left) for left, right in zip(row_centers, row_centers[1:])),
            default=0.0,
        )
        line_jump_ratio = largest_line_jump / scale
        contrast_score = min(1.0, contrast / self.thresholds.minimum_contrast)
        stability_score = max(0.0, 1.0 - line_jump_ratio / self.thresholds.maximum_line_jump_ratio)
        score = 0.5 * contrast_score + 0.5 * stability_score
        metrics = {
            "contrast": contrast,
            "robust_scale": robust_scale,
            "largest_line_jump": largest_line_jump,
            "line_jump_ratio": line_jump_ratio,
        }
        if score >= self.thresholds.good_score:
            return TipQualityAssessment(TipQualityDecision.GOOD, score, 1.0, "metrics_passed", metrics)
        return TipQualityAssessment(TipQualityDecision.DEGRADED, score, 1.0, "metrics_failed", metrics)


def _coerce_image(data: Sequence[Sequence[float]]) -> tuple[tuple[float, ...], ...]:
    image = tuple(tuple(float(value) for value in row) for row in data)
    if not image or not image[0]:
        raise ValueError("tip-quality image must not be empty")
    columns = len(image[0])
    if any(len(row) != columns for row in image):
        raise ValueError("tip-quality image must be rectangular")
    if any(not math.isfinite(value) for row in image for value in row):
        raise ValueError("tip-quality image must contain only finite values")
    return image
