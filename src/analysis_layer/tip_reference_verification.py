from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from data_layer import NanonisDatFile, NanonisSxmFile
from .scan_images import process_scan_image


def measure_retrace_pair(forward, backward) -> dict:
    """Paired pixels in the same physical orientation; no registration or smoothing."""
    first, second = np.asarray(forward, dtype=float), np.asarray(backward, dtype=float)
    if first.ndim != 2 or first.shape != second.shape or not first.size:
        raise ValueError("retrace requires equal nonempty matrices")
    if np.isinf(first).any() or np.isinf(second).any():
        raise ValueError("infinite scan values")
    paired = np.isfinite(first) & np.isfinite(second)
    if paired.sum() < 3:
        raise ValueError("not enough paired pixels")
    # Fit one common plane so independent flattening cannot hide directional errors.
    mean = np.where(paired, (first + second) / 2, np.nan)
    plane = mean - process_scan_image(mean, "plane")
    a, b = (first - plane)[paired], (second - plane)[paired]
    delta = a - b
    centered = delta - np.median(delta)
    scale = float(np.std((a + b) / 2))
    return {
        "paired_pixels": int(paired.sum()),
        "paired_fraction": float(paired.mean()),
        "retrace_offset_m": float(np.median(delta)),
        "retrace_rms_m": float(np.sqrt(np.mean(centered ** 2))),
        "retrace_p95_m": float(np.percentile(np.abs(centered), 95)),
        "retrace_relative_rms": None if scale == 0 else float(np.sqrt(np.mean(centered ** 2)) / scale),
        "retrace_correlation": None if min(np.std(a), np.std(b)) == 0 else float(np.corrcoef(a, b)[0, 1]),
        "structure_rms_m": scale,
    }


def measure_poke_shape(forward, backward, *, pixel_x_m, pixel_y_m) -> dict:
    """Measure an explicitly selected positive poke feature, never search the whole image."""
    for value in (pixel_x_m, pixel_y_m):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("pixel sizes must be positive")
    first, second = np.asarray(forward, dtype=float), np.asarray(backward, dtype=float)
    if first.ndim != 2 or first.shape != second.shape or min(first.shape) < 5:
        raise ValueError("poke ROI requires paired matrices at least 5 by 5")
    if not (np.isfinite(first).all() and np.isfinite(second).all()):
        raise ValueError("poke ROI must be fully acquired")
    mean = (first + second) / 2
    y, x = np.indices(mean.shape)
    border = (x == 0) | (y == 0) | (x == mean.shape[1]-1) | (y == mean.shape[0]-1)
    fit = np.column_stack((x[border], y[border], np.ones(border.sum())))
    c = np.linalg.lstsq(fit, mean[border], rcond=None)[0]
    z = mean - (c[0]*x + c[1]*y + c[2])
    peak = np.unravel_index(np.argmax(z), z.shape)
    height = float(z[peak])
    if height <= 0:
        raise ValueError("no positive poke feature")
    mask = z >= height / 2
    if mask[border].any():
        raise ValueError("poke half-height contour touches ROI boundary")
    remaining = set(map(tuple, np.argwhere(mask)))
    components = []
    while remaining:
        seed = min(remaining)
        remaining.remove(seed)
        component, pending = [seed], [seed]
        while pending:
            row, col = pending.pop()
            for point in ((row-1,col), (row+1,col), (row,col-1), (row,col+1)):
                if point in remaining:
                    remaining.remove(point)
                    pending.append(point)
                    component.append(point)
        components.append(component)
    primary = next(component for component in components if peak in component)
    fx = _fwhm(z[peak[0], :], peak[1], height/2)
    fy = _fwhm(z[:, peak[1]], peak[0], height/2)
    if fx is None or fy is None:
        raise ValueError("unbounded poke width")
    points = np.array([(col*pixel_x_m, row*pixel_y_m) for row, col in primary])
    eigen = np.linalg.eigvalsh(np.cov(points.T)) if len(points) >= 3 else np.array([0, 0])
    aspect = None if eigen[0] <= 0 else float(np.sqrt(eigen[1] / eigen[0]))
    return {
        "height_m": height, "fwhm_x_m": fx*pixel_x_m, "fwhm_y_m": fy*pixel_y_m,
        "equivalent_fwhm_radius_m": math.sqrt(fx*pixel_x_m*fy*pixel_y_m)/2,
        "principal_aspect_ratio": aspect,
        "secondary_lobe_fraction": float(1 - len(primary)/mask.sum()),
        "half_height_components": len(components),
        "minimum_relative_height_m": float(np.min(z)),
        "minimum_position_row_column": [int(v) for v in np.unravel_index(np.argmin(z), z.shape)],
        "border_residual_rms_m": float(np.std(z[border])),
        "border_residual_scope": "Includes any morphology on the ROI edge; use a separate clean background to estimate noise.",
        "negative_height_scope": "Relative to the local border plane; compare with background noise and both images, not a zero-height pass threshold.",
        "roi_retrace": measure_retrace_pair(first, second),
    }


@dataclass(frozen=True)
class CompactProtrusionAnalysisConfig:
    channel_name: str = "Z"
    local_half_width_m: float = 6e-9
    local_half_height_m: float = 3.2e-9
    smoothing_radius_pixels: int = 1
    minimum_acquired_rows: int = 5


@dataclass(frozen=True)
class CompactProtrusionMetrics:
    acquired_rows: int
    total_rows: int
    center_column: int
    center_row: int
    center_x_m: float
    center_y_m: float
    protrusion_height_m: float
    protrusion_fwhm_x_m: float
    protrusion_fwhm_y_m: float
    protrusion_fwhm_aspect_ratio: float
    protrusion_equivalent_fwhm_radius_m: float
    z_forward_backward_correlation: float
    candidate_count: int


@dataclass(frozen=True)
class Ag111SpectrumAnalysisConfig:
    bias_column: str = "Bias calc (V)"
    didv_column: str = "LI Demod 1 X (A)"
    smoothing_points: int = 7
    onset_search_v: tuple[float, float] = (-0.1, -0.03)
    pre_edge_window_v: tuple[float, float] = (-0.12, -0.085)
    post_edge_window_v: tuple[float, float] = (-0.045, 0.02)
    onset_exclusion_v: tuple[float, float] = (-0.085, -0.045)


@dataclass(frozen=True)
class Ag111SpectrumMetrics:
    points: int
    bias_start_v: float
    bias_stop_v: float
    bias_step_v: float
    surface_state_onset_v: float
    didv_step_a: float
    didv_detrended_noise_rms_a: float
    didv_step_to_noise: float
    maximum_adjacent_change_outside_onset_a: float
    finite_and_monotonic: bool


def analyze_compact_protrusion(
    scan: NanonisSxmFile,
    config: CompactProtrusionAnalysisConfig = CompactProtrusionAnalysisConfig(),
) -> CompactProtrusionMetrics:
    if config.local_half_width_m <= 0.0 or config.local_half_height_m <= 0.0:
        raise ValueError("local protrusion windows must be positive")
    if config.smoothing_radius_pixels < 0:
        raise ValueError("smoothing radius must be non-negative")
    channel = scan.channel(config.channel_name)
    if channel.forward is None or channel.backward is None:
        raise ValueError("compact protrusion analysis requires forward and backward data")
    forward = channel.forward.astype(np.float64, copy=False)
    backward = np.fliplr(channel.backward).astype(np.float64, copy=False)
    paired = np.isfinite(forward) & np.isfinite(backward)
    acquired_indexes = np.flatnonzero(np.any(paired, axis=1))
    if len(acquired_indexes) < config.minimum_acquired_rows:
        raise ValueError(
            f"only {len(acquired_indexes)} paired rows are available; "
            f"need {config.minimum_acquired_rows}"
        )
    row_start = int(acquired_indexes.min())
    row_stop = int(acquired_indexes.max()) + 1
    forward = forward[row_start:row_stop]
    backward = backward[row_start:row_stop]
    paired = np.isfinite(forward) & np.isfinite(backward)
    aligned = np.where(paired, 0.5 * (forward + backward), np.nan)
    pixel_x_m = scan.range_x_m / scan.columns
    pixel_y_m = scan.range_y_m / scan.rows
    half_width = max(6, round(config.local_half_width_m / pixel_x_m))
    half_height = max(3, round(config.local_half_height_m / pixel_y_m))
    half_height = min(half_height, (aligned.shape[0] - 1) // 2)
    if half_height < 3 or aligned.shape[1] <= 2 * half_width:
        raise ValueError("the acquired scan region is too small for protrusion analysis")

    smoothed = _nan_box_mean(aligned, config.smoothing_radius_pixels)
    candidates: list[tuple[float, _CandidateMetrics]] = []
    for center_row, center_column in _local_maxima(smoothed, half_width):
        candidate = _measure_candidate(
            aligned,
            center_row,
            center_column,
            half_height,
            half_width,
            pixel_x_m,
            pixel_y_m,
        )
        if candidate is None:
            continue
        compactness_score = candidate.height_m / (
            candidate.fwhm_x_m
            * candidate.fwhm_y_m
            * candidate.aspect_ratio
        )
        candidates.append((compactness_score, candidate))
    if not candidates:
        raise ValueError("no bounded compact protrusion candidate was found")
    _, best = max(candidates, key=lambda item: item[0])
    correlation = _line_flattened_correlation(forward, backward)
    return CompactProtrusionMetrics(
        acquired_rows=len(acquired_indexes),
        total_rows=scan.rows,
        center_column=int(best.center_column),
        center_row=int(row_start + best.center_row),
        center_x_m=float((best.center_column + 0.5) * pixel_x_m),
        center_y_m=float((row_start + best.center_row + 0.5) * pixel_y_m),
        protrusion_height_m=float(best.height_m),
        protrusion_fwhm_x_m=float(best.fwhm_x_m),
        protrusion_fwhm_y_m=float(best.fwhm_y_m),
        protrusion_fwhm_aspect_ratio=float(best.aspect_ratio),
        protrusion_equivalent_fwhm_radius_m=(
            math.sqrt(best.fwhm_x_m * best.fwhm_y_m) / 2.0
        ),
        z_forward_backward_correlation=correlation,
        candidate_count=int(len(candidates)),
    )


def analyze_ag111_spectrum(
    spectrum: NanonisDatFile,
    config: Ag111SpectrumAnalysisConfig = Ag111SpectrumAnalysisConfig(),
) -> Ag111SpectrumMetrics:
    if config.smoothing_points < 3 or config.smoothing_points % 2 == 0:
        raise ValueError("smoothing_points must be an odd integer of at least three")
    bias = np.asarray(spectrum.column(config.bias_column), dtype=np.float64)
    didv = np.asarray(spectrum.column(config.didv_column), dtype=np.float64)
    finite = np.isfinite(bias) & np.isfinite(didv)
    monotonic = bool(np.all(np.diff(bias) > 0.0) or np.all(np.diff(bias) < 0.0))
    if not bool(np.all(finite)) or not monotonic:
        raise ValueError("spectrum bias and dI/dV must be finite and monotonic")
    if bias[0] > bias[-1]:
        bias = bias[::-1]
        didv = didv[::-1]
    kernel = np.ones(config.smoothing_points) / config.smoothing_points
    smoothed = np.convolve(didv, kernel, mode="valid")
    half = config.smoothing_points // 2
    smoothed_bias = bias[half:-half]
    gradient = np.gradient(smoothed, smoothed_bias)
    onset_mask = _window_mask(smoothed_bias, config.onset_search_v)
    if not bool(np.any(onset_mask)):
        raise ValueError("spectrum does not cover the configured onset-search window")
    onset_index = np.flatnonzero(onset_mask)[np.argmax(gradient[onset_mask])]
    onset_v = float(smoothed_bias[onset_index])
    pre_mask = _window_mask(bias, config.pre_edge_window_v)
    post_mask = _window_mask(bias, config.post_edge_window_v)
    if np.count_nonzero(pre_mask) < 3 or np.count_nonzero(post_mask) < 3:
        raise ValueError("spectrum does not cover the configured baseline windows")
    pre_mean = float(np.mean(didv[pre_mask]))
    post_mean = float(np.mean(didv[post_mask]))
    step = post_mean - pre_mean
    residuals = np.concatenate(
        [
            _linear_residuals(bias[pre_mask], didv[pre_mask]),
            _linear_residuals(bias[post_mask], didv[post_mask]),
        ]
    )
    noise = float(np.std(residuals))
    adjacent_bias = bias[:-1]
    outside_onset = ~_window_mask(adjacent_bias, config.onset_exclusion_v)
    maximum_adjacent = float(np.max(np.abs(np.diff(didv))[outside_onset]))
    return Ag111SpectrumMetrics(
        points=len(bias),
        bias_start_v=float(bias[0]),
        bias_stop_v=float(bias[-1]),
        bias_step_v=float(np.median(np.diff(bias))),
        surface_state_onset_v=onset_v,
        didv_step_a=step,
        didv_detrended_noise_rms_a=noise,
        didv_step_to_noise=(math.inf if noise == 0.0 else step / noise),
        maximum_adjacent_change_outside_onset_a=maximum_adjacent,
        finite_and_monotonic=True,
    )


@dataclass(frozen=True)
class _CandidateMetrics:
    center_column: int
    center_row: int
    height_m: float
    fwhm_x_m: float
    fwhm_y_m: float
    aspect_ratio: float


def _nan_box_mean(values: np.ndarray, radius: int) -> np.ndarray:
    if radius == 0:
        return values.copy()
    total = np.zeros(values.shape, dtype=np.float64)
    count = np.zeros(values.shape, dtype=np.int32)
    rows, columns = values.shape
    for row_offset in range(-radius, radius + 1):
        for column_offset in range(-radius, radius + 1):
            source_rows = slice(max(0, -row_offset), min(rows, rows - row_offset))
            source_columns = slice(
                max(0, -column_offset),
                min(columns, columns - column_offset),
            )
            target_rows = slice(max(0, row_offset), min(rows, rows + row_offset))
            target_columns = slice(
                max(0, column_offset),
                min(columns, columns + column_offset),
            )
            source = values[source_rows, source_columns]
            valid = np.isfinite(source)
            total[target_rows, target_columns] += np.where(valid, source, 0.0)
            count[target_rows, target_columns] += valid
    return np.divide(total, count, out=np.full_like(total, np.nan), where=count > 0)


def _local_maxima(values: np.ndarray, horizontal_margin: int):
    rows, columns = values.shape
    for row in range(2, rows - 2):
        for column in range(horizontal_margin, columns - horizontal_margin):
            value = values[row, column]
            if not math.isfinite(float(value)):
                continue
            neighborhood = values[row - 1 : row + 2, column - 1 : column + 2]
            if value >= np.nanmax(neighborhood):
                yield row, column


def _measure_candidate(
    values: np.ndarray,
    center_row: int,
    center_column: int,
    half_height: int,
    half_width: int,
    pixel_x_m: float,
    pixel_y_m: float,
) -> _CandidateMetrics | None:
    crop = values[
        center_row - half_height : center_row + half_height + 1,
        center_column - half_width : center_column + half_width + 1,
    ]
    row_grid, column_grid = np.indices(crop.shape)
    border = (
        (column_grid < 4)
        | (column_grid >= crop.shape[1] - 4)
        | (row_grid < 2)
        | (row_grid >= crop.shape[0] - 2)
    ) & np.isfinite(crop)
    if np.count_nonzero(border) < 20:
        return None
    design = np.column_stack(
        [
            np.ones(np.count_nonzero(border)),
            column_grid[border],
            row_grid[border],
        ]
    )
    coefficients = np.linalg.lstsq(design, crop[border], rcond=None)[0]
    residual = crop - (
        coefficients[0]
        + coefficients[1] * column_grid
        + coefficients[2] * row_grid
    )
    peak_row, peak_column = np.unravel_index(np.nanargmax(residual), residual.shape)
    if abs(peak_row - half_height) > 2 or abs(peak_column - half_width) > 2:
        return None
    height = float(residual[peak_row, peak_column])
    if height <= 0.0:
        return None
    fwhm_x = _fwhm(residual[peak_row, :], peak_column, height / 2.0)
    fwhm_y = _fwhm(residual[:, peak_column], peak_row, height / 2.0)
    if fwhm_x is None or fwhm_y is None:
        return None
    fwhm_x_m = fwhm_x * pixel_x_m
    fwhm_y_m = fwhm_y * pixel_y_m
    if min(fwhm_x_m, fwhm_y_m) <= 0.0:
        return None
    aspect = max(fwhm_x_m, fwhm_y_m) / min(fwhm_x_m, fwhm_y_m)
    return _CandidateMetrics(
        center_column=center_column + peak_column - half_width,
        center_row=center_row + peak_row - half_height,
        height_m=height,
        fwhm_x_m=fwhm_x_m,
        fwhm_y_m=fwhm_y_m,
        aspect_ratio=aspect,
    )


def _fwhm(values: np.ndarray, center: int, threshold: float) -> float | None:
    if not math.isfinite(float(values[center])) or values[center] < threshold:
        return None
    left = center
    while left > 0 and math.isfinite(float(values[left - 1])) and values[left - 1] >= threshold:
        left -= 1
    right = center
    while (
        right + 1 < len(values)
        and math.isfinite(float(values[right + 1]))
        and values[right + 1] >= threshold
    ):
        right += 1
    if left == 0 or right + 1 >= len(values):
        return None
    left_crossing = _interpolate_crossing(
        left - 1,
        float(values[left - 1]),
        left,
        float(values[left]),
        threshold,
    )
    right_crossing = _interpolate_crossing(
        right,
        float(values[right]),
        right + 1,
        float(values[right + 1]),
        threshold,
    )
    return float(right_crossing - left_crossing)


def _interpolate_crossing(
    first_x: int,
    first_y: float,
    second_x: int,
    second_y: float,
    threshold: float,
) -> float:
    if second_y == first_y:
        return 0.5 * (first_x + second_x)
    fraction = (threshold - first_y) / (second_y - first_y)
    return first_x + fraction * (second_x - first_x)


def _line_flattened_correlation(forward: np.ndarray, backward: np.ndarray) -> float:
    forward_flat = forward - np.nanmedian(forward, axis=1, keepdims=True)
    backward_flat = backward - np.nanmedian(backward, axis=1, keepdims=True)
    valid = np.isfinite(forward_flat) & np.isfinite(backward_flat)
    if np.count_nonzero(valid) < 3:
        raise ValueError("not enough paired samples for retrace correlation")
    return float(np.corrcoef(forward_flat[valid], backward_flat[valid])[0, 1])


def _window_mask(values: np.ndarray, window: tuple[float, float]) -> np.ndarray:
    return (values >= window[0]) & (values <= window[1])


def _linear_residuals(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    coefficients = np.polyfit(x, y, 1)
    return y - np.polyval(coefficients, x)
