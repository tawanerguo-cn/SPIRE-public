from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import median
from typing import Protocol, Sequence, runtime_checkable

from instrument_layer import BiasSpectroscopyData, ScanFrame
import numpy as np


@dataclass(frozen=True)
class AtomCandidate:
    candidate_id: str
    row: int
    column: int
    x_m: float
    y_m: float
    height: float
    score: float
    frame_x_fraction: float = 0.0
    frame_y_fraction: float = 0.0
    position_uncertainty_m: float = 0.0


@dataclass(frozen=True)
class AtomDetectorSettings:
    threshold_sigma: float = 3.0
    edge_margin_pixels: int = 1
    minimum_separation_pixels: float = 2.0
    maximum_candidates: int = 1000
    smoothing_radius_pixels: int = 0
    background_radius_pixels: int = 0


@dataclass(frozen=True)
class SpectrumClassification:
    label: str
    confidence: float
    distance: float | None
    reason: str
    reference_id: str | None = None
    score_kind: str = "similarity_not_calibrated_probability"


class AtomDetector(Protocol):
    def detect(self, data: Sequence[Sequence[float]], frame: ScanFrame) -> tuple[AtomCandidate, ...]: ...


class SpectrumClassifier(Protocol):
    def classify(self, spectrum: BiasSpectroscopyData) -> SpectrumClassification: ...


@runtime_checkable
class SpectralPrior(Protocol):
    """Analysis input contract; workflow supplies priors loaded by knowledge."""
    reference_id: str
    label: str
    channel_name: str
    bias_v: tuple[float, ...]
    values: tuple[float, ...]
    conditions: tuple[tuple[str, str], ...]
    status: str


class LocalMaximumAtomDetector:
    """Deterministic baseline detector, intended to be replaced by reviewed expert models."""

    def __init__(self, settings: AtomDetectorSettings | None = None) -> None:
        self.settings = settings or AtomDetectorSettings()
        if not math.isfinite(self.settings.threshold_sigma) or self.settings.threshold_sigma <= 0:
            raise ValueError("threshold_sigma must be finite and positive")
        if not math.isfinite(self.settings.minimum_separation_pixels) or self.settings.minimum_separation_pixels < 0:
            raise ValueError("minimum separation must be finite and non-negative")
        for value, name, minimum in (
            (self.settings.edge_margin_pixels, "edge_margin_pixels", 1),
            (self.settings.maximum_candidates, "maximum_candidates", 1),
            (self.settings.smoothing_radius_pixels, "smoothing_radius_pixels", 0),
            (self.settings.background_radius_pixels, "background_radius_pixels", 0),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")

    def detect(
        self, data: Sequence[Sequence[float]], frame: ScanFrame,
        *, valid_mask: Sequence[Sequence[bool]] | None = None,
    ) -> tuple[AtomCandidate, ...]:
        image = _validate_image(data, allow_missing=valid_mask is not None)
        rows = len(image)
        columns = len(image[0])
        if valid_mask is not None and (
            len(valid_mask) != rows or any(len(row) != columns for row in valid_mask)
        ):
            raise ValueError("valid mask must match image shape")
        valid = tuple(tuple(
            math.isfinite(image[row][column]) and (
                valid_mask is None or bool(valid_mask[row][column])
            ) for column in range(columns)
        ) for row in range(rows))
        detection_image = np.asarray(image)
        if self.settings.smoothing_radius_pixels:
            detection_image = _masked_box_mean(detection_image, valid, self.settings.smoothing_radius_pixels)
        if self.settings.background_radius_pixels:
            detection_image = detection_image - _masked_box_mean(
                detection_image, valid, self.settings.background_radius_pixels)
        flat = [float(detection_image[r, c]) for r in range(rows) for c in range(columns) if valid[r][c]]
        if not flat:
            return ()
        center = median(flat)
        mad = median(abs(value - center) for value in flat)
        scale = 1.4826 * mad
        if scale == 0.0:
            span = max(flat) - min(flat)
            scale = span / 10.0 if span > 0.0 else 1.0
        threshold = center + self.settings.threshold_sigma * scale
        margin = self.settings.edge_margin_pixels

        peaks: list[tuple[float, int, int, float]] = []
        for row in range(margin, rows - margin):
            for column in range(margin, columns - margin):
                if not all(valid[r][c] for r in range(row - 1, row + 2) for c in range(column - 1, column + 2)):
                    continue
                value = float(detection_image[row, column])
                if value < threshold:
                    continue
                neighbours = (
                    detection_image[other_row, other_column]
                    for other_row in range(max(0, row - 1), min(rows, row + 2))
                    for other_column in range(max(0, column - 1), min(columns, column + 2))
                    if (other_row, other_column) != (row, column)
                )
                if all(value > neighbour for neighbour in neighbours):
                    peaks.append(((value - center) / scale, row, column, image[row][column]))

        selected: list[tuple[float, int, int, float]] = []
        minimum_distance = self.settings.minimum_separation_pixels
        for peak in sorted(peaks, reverse=True):
            _score, row, column, _height = peak
            if all(math.hypot(row - kept[1], column - kept[2]) >= minimum_distance for kept in selected):
                selected.append(peak)
            if len(selected) >= self.settings.maximum_candidates:
                break

        candidates: list[AtomCandidate] = []
        for index, (score, row, column, height) in enumerate(selected, start=1):
            frame_x_fraction, frame_y_fraction = pixel_center_to_frame_fractions(rows, columns, row, column)
            x_m, y_m = pixel_center_to_xy(frame, rows, columns, row, column)
            candidates.append(
                AtomCandidate(
                    candidate_id=f"atom-{index:04d}",
                    row=row,
                    column=column,
                    x_m=x_m,
                    y_m=y_m,
                    height=height,
                    score=score,
                    frame_x_fraction=frame_x_fraction,
                    frame_y_fraction=frame_y_fraction,
                    position_uncertainty_m=0.5
                    * math.hypot(frame.width_m / columns, frame.height_m / rows),
                )
            )
        return tuple(candidates)


def _masked_box_mean(image, mask, radius):
    """Local numerical background with no zero-imputation of missing scan data."""
    radius = min(radius, max(image.shape))
    width = 2 * radius + 1
    def total(values):
        padded = np.pad(values, radius, mode="constant")
        integral = np.pad(padded, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
        return (integral[width:, width:] - integral[:-width, width:]
                - integral[width:, :-width] + integral[:-width, :-width])
    counts = total(np.asarray(mask, dtype=float))
    return np.divide(total(np.where(mask, image, 0.0)), counts,
                     out=np.zeros_like(image, dtype=float), where=counts > 0)


class ReferenceSpectrumClassifier:
    """Bias-axis-aware shape comparison with applicability and ambiguity rejection.

    L2 distance between unit-norm signals is a baseline shape score, not species proof. No
    interpolation/extrapolation is performed: incompatible sampling is rejected.
    """

    def __init__(
        self,
        references: Sequence[SpectralPrior] = (),
        *,
        channel_name: str,
        maximum_distance: float,
        axis_channel_name: str = "Bias calc (V)",
        conditions: dict[str, str] | None = None,
        minimum_margin: float = 0.02,
        allow_synthetic: bool = False,
        unknown_label: str = "unknown",
    ) -> None:
        if not math.isfinite(maximum_distance) or maximum_distance <= 0.0:
            raise ValueError("maximum_distance must be finite and positive")
        if not math.isfinite(minimum_margin) or minimum_margin < 0:
            raise ValueError("minimum_margin must be finite and nonnegative")
        if any(not isinstance(ref, SpectralPrior) for ref in references):
            raise TypeError("references require reviewed provenance and physical bias axes")
        self.references = tuple(references)
        self.channel_name = channel_name
        self.maximum_distance = float(maximum_distance)
        self.unknown_label = unknown_label
        self.axis_channel_name = axis_channel_name
        self.conditions = dict(conditions or {})
        self.minimum_margin = minimum_margin
        self.allow_synthetic = allow_synthetic

    def classify(self, spectrum: BiasSpectroscopyData) -> SpectrumClassification:
        def unknown(reason, distance=None):
            return SpectrumClassification(self.unknown_label, 0.0, distance, reason)

        if (len(set(spectrum.channel_names)) != len(spectrum.channel_names)
                or spectrum.rows != len(spectrum.channel_names)
                or len(spectrum.data) != spectrum.rows
                or spectrum.columns < 3
                or any(len(row) != spectrum.columns for row in spectrum.data)):
            return unknown("malformed_spectrum")
        try:
            row_index = spectrum.channel_names.index(self.channel_name)
        except ValueError:
            return unknown("channel_not_recorded")
        try:
            axis_index = spectrum.channel_names.index(self.axis_channel_name)
        except ValueError:
            return unknown("bias_axis_not_recorded")
        values = np.asarray(spectrum.data[row_index], dtype=float)
        axis = np.asarray(spectrum.data[axis_index], dtype=float)
        if not np.isfinite(values).all() or not np.isfinite(axis).all():
            return unknown("nonfinite_data")
        delta = np.diff(axis)
        if not ((delta > 0).all() or (delta < 0).all()):
            return unknown("bias_axis_not_monotonic")
        if np.ptp(values) == 0:
            return unknown("constant_or_empty_signal")
        if delta[0] < 0:
            axis, values = axis[::-1], values[::-1]
        observed = np.asarray(_normalize(values))
        distances = []
        applicable = 0
        for reference in self.references:
            if reference.status not in {"reviewed", "synthetic"}:
                continue
            if reference.status == "synthetic" and not self.allow_synthetic:
                continue
            if reference.channel_name != self.channel_name or any(
                self.conditions.get(key) != value for key, value in reference.conditions
            ):
                continue
            applicable += 1
            ref_axis = np.asarray(reference.bias_v)
            ref_values = reference.values
            if ref_axis[0] > ref_axis[-1]:
                ref_axis, ref_values = ref_axis[::-1], ref_values[::-1]
            if len(ref_axis) != len(axis) or not np.allclose(ref_axis, axis, rtol=1e-6, atol=1e-9):
                continue
            distance = float(np.linalg.norm(observed - _normalize(ref_values)))
            distances.append((distance, reference.label, reference.reference_id))
        if not distances:
            return unknown("bias_axis_mismatch" if applicable else "no_applicable_reviewed_reference")
        distance, label, reference_id = min(distances)
        confidence = max(0.0, 1.0 - distance / self.maximum_distance)
        if distance > self.maximum_distance:
            return unknown("outside_reference_distance", distance)
        others = [d for d, other_label, _ in distances if other_label != label]
        if others and min(others) - distance <= self.minimum_margin:
            return unknown("ambiguous_reference_match", distance)
        return SpectrumClassification(label, confidence, distance, "nearest_applicable_reference", reference_id)


def pixel_center_to_xy(
    frame: ScanFrame,
    rows: int,
    columns: int,
    row: int,
    column: int,
) -> tuple[float, float]:
    """Map a pixel center into the rotated physical scan frame."""

    if rows <= 0 or columns <= 0 or not (0 <= row < rows and 0 <= column < columns):
        raise ValueError("pixel coordinates must lie inside a non-empty image")
    geometry = (frame.center_x_m, frame.center_y_m, frame.width_m, frame.height_m, frame.angle_deg)
    if not all(math.isfinite(value) for value in geometry) or frame.width_m <= 0 or frame.height_m <= 0:
        raise ValueError("scan frame must have finite geometry and positive dimensions")

    local_x = ((column + 0.5) / columns - 0.5) * frame.width_m
    local_y = (0.5 - (row + 0.5) / rows) * frame.height_m
    angle = math.radians(frame.angle_deg)
    # Nanonis scan-frame angles are positive clockwise.
    x_m = frame.center_x_m + local_x * math.cos(angle) + local_y * math.sin(angle)
    y_m = frame.center_y_m - local_x * math.sin(angle) + local_y * math.cos(angle)
    return x_m, y_m


def pixel_center_to_frame_fractions(
    rows: int,
    columns: int,
    row: int,
    column: int,
) -> tuple[float, float]:
    return (
        2.0 * ((column + 0.5) / columns - 0.5),
        2.0 * (0.5 - (row + 0.5) / rows),
    )


def _validate_image(
    data: Sequence[Sequence[float]], *, allow_missing: bool = False,
) -> tuple[tuple[float, ...], ...]:
    image = tuple(tuple(float(value) for value in row) for row in data)
    if not image or not image[0]:
        raise ValueError("scan image must not be empty")
    columns = len(image[0])
    if any(len(row) != columns for row in image):
        raise ValueError("scan image must be rectangular")
    if any(math.isinf(value) or (not allow_missing and math.isnan(value)) for row in image for value in row):
        raise ValueError("scan image values must be finite")
    return image


def _normalize(values: Sequence[float]) -> tuple[float, ...]:
    output = tuple(float(value) for value in values)
    if not output or not all(math.isfinite(x) for x in output):
        raise ValueError("spectrum must contain finite values")
    scale = max(abs(value) for value in output)
    if scale == 0.0:
        raise ValueError("zero signal has no shape")
    scaled = tuple(value / scale for value in output)
    norm = math.sqrt(sum(value * value for value in scaled))
    return tuple(value / norm for value in scaled)
