"""Numerical scan preparation and scientific previews; no instrument access."""
from __future__ import annotations

from io import BytesIO

import numpy as np

from data_layer.scan_observation import ScanObservation
from .atoms import AtomCandidate


def process_scan_image(data, processing: str = "plane") -> np.ndarray:
    image = np.asarray(data, dtype=np.float64)
    if image.ndim != 2 or not image.size or np.isinf(image).any():
        raise ValueError("scan must be a non-empty matrix without infinities")
    output = image.copy()
    valid = np.isfinite(image)
    if processing == "raw":
        pass
    elif processing == "line_median":
        for row in range(image.shape[0]):
            if valid[row].any():
                output[row, valid[row]] -= np.median(image[row, valid[row]])
    elif processing == "line_linear":
        # Fit each acquired fast-axis line independently, without smoothing or
        # interpolating missing pixels. Retain raw alongside this display:
        # line fits can suppress genuine slow-axis steps and temporal jumps.
        x = np.arange(image.shape[1], dtype=float)
        for row in range(image.shape[0]):
            mask = valid[row]
            if mask.sum() >= 2:
                design = np.column_stack((x[mask], np.ones(int(mask.sum()))))
                output[row, mask] -= design @ np.linalg.lstsq(design, image[row, mask], rcond=None)[0]
            elif mask.any():
                output[row, mask] = 0.0
    elif processing == "plane":
        y, x = np.indices(image.shape, dtype=float)
        if valid.sum() >= 3:
            design = np.column_stack((x[valid], y[valid], np.ones(int(valid.sum()))))
            if np.linalg.matrix_rank(design) == 3:
                coefficients = np.linalg.lstsq(design, image[valid], rcond=None)[0]
                output[valid] -= design @ coefficients
            else:
                output[valid] -= np.median(image[valid])
    else:
        raise ValueError("processing must be raw, plane, line_median, or line_linear")
    output.setflags(write=False)
    return output


def scan_quality_metrics(observation: ScanObservation) -> dict:
    valid = observation.valid_mask
    result = {"paired_pixels": 0, "forward_backward_correlation": None,
              "complete": bool(valid.all()), "classification": "not_assessed"}
    if observation.backward is not None:
        other = process_scan_image(observation.backward, observation.processing)
        paired = valid & np.isfinite(other)
        result["paired_pixels"] = int(paired.sum())
        if paired.sum() >= 3:
            first = observation.processed[paired]
            second = other[paired]
            if np.std(first) > 0 and np.std(second) > 0:
                result["forward_backward_correlation"] = float(np.corrcoef(first, second)[0, 1])
    return result


def render_scan_observation(
    observation: ScanObservation, *, candidates: tuple[AtomCandidate, ...] = (),
    crop: tuple[int, int, int, int] | None = None,
) -> bytes:
    """Render raw/processed/retrace without stretching partial scans or hiding masks.

    crop=(row_start,row_stop,column_start,column_stop), with exclusive stops.
    Axes use local physical coordinates; crops retain their original position.
    """
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    r0, r1, c0, c1 = crop or (0, observation.rows, 0, observation.columns)
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (r0, r1, c0, c1)):
        raise ValueError("crop coordinates must be integers")
    if not (0 <= r0 < r1 <= observation.rows and 0 <= c0 < c1 <= observation.columns):
        raise ValueError("crop must lie inside the original scan")
    panels = [("Raw forward", observation.raw, "raw")]
    if observation.backward is not None:
        panels.append(("Raw retrace", observation.backward, "raw"))
    if observation.processing != "raw":
        label = "Linear fit subtracted" if observation.processing == "line_linear" else observation.processing
        panels.append((label + " forward", observation.processed, "processed"))
        if observation.backward is not None:
            panels.append((label + " retrace",
                           process_scan_image(observation.backward, observation.processing), "processed"))
    frame = observation.frame
    width_nm, height_nm = frame.width_m * 1e9, frame.height_m * 1e9
    extent = ((c0 / observation.columns - .5) * width_nm,
              (c1 / observation.columns - .5) * width_nm,
              (.5 - r1 / observation.rows) * height_nm,
              (.5 - r0 / observation.rows) * height_nm)
    crop_width, crop_height = extent[1] - extent[0], extent[3] - extent[2]
    columns = min(2, len(panels))
    rows = (len(panels) + columns - 1) // columns
    short_strip = crop_height / crop_width < .3
    panel_height = 1.8 if short_strip else .8 + 5 * min(crop_height / crop_width, 2)
    figure = Figure(figsize=(6 * columns, 1.5 + rows * panel_height),
                    layout="constrained")
    FigureCanvasAgg(figure)
    axes = figure.subplots(rows, columns, squeeze=False).ravel()
    scale = 1e9 if observation.channel_unit == "m" else 1.0
    unit = "nm" if observation.channel_unit == "m" else observation.channel_unit
    ranges = {}
    for kind in {panel[2] for panel in panels}:
        valid = np.concatenate([data[r0:r1, c0:c1][np.isfinite(data[r0:r1, c0:c1])] * scale
                                for _, data, group in panels if group == kind])
        limits = np.percentile(valid, (1, 99)) if valid.size else (0, 1)
        if limits[0] == limits[1]:
            limits = (limits[0] - 0.5, limits[1] + 0.5)
        ranges[kind] = limits
    for axis, (title, data, kind) in zip(axes, panels):
        shown = data[r0:r1, c0:c1] * scale
        limits = ranges[kind]
        cmap = __import__("matplotlib").colormaps["gray"].with_extremes(bad="#c5cbd5")
        im = axis.imshow(shown, origin="upper", interpolation="nearest", cmap=cmap,
                         vmin=limits[0], vmax=limits[1],
                         extent=extent, aspect="equal")
        for candidate in candidates if title.endswith("forward") and (kind == "processed" or observation.processing == "raw") else ():
            if r0 <= candidate.row < r1 and c0 <= candidate.column < c1:
                x_nm = ((candidate.column + .5) / observation.columns - .5) * width_nm
                y_nm = (.5 - (candidate.row + .5) / observation.rows) * height_nm
                axis.scatter(x_nm, y_nm, s=55, facecolors="none", edgecolors="#ef6a24")
                visible = sum(r0 <= item.row < r1 and c0 <= item.column < c1 for item in candidates)
                if visible <= 30:
                    axis.annotate(candidate.candidate_id.rsplit("-", 1)[-1], (x_nm, y_nm),
                                  xytext=(4, -9), textcoords="offset points", fontsize=7, color="#d44900")
        axis.set(title=title, xlabel="Local x (nm)", ylabel="Local y (nm)")
        if short_strip:
            figure.colorbar(im, ax=axis, orientation="horizontal", shrink=.85,
                            aspect=45, pad=.18, label=unit + " (1–99% display range)")
        else:
            figure.colorbar(im, ax=axis, shrink=0.65, label=unit + " (1–99% display range)")
    setpoint = "unknown" if observation.setpoint_a is None else f"{observation.setpoint_a * 1e12:g} pA"
    details = (f"View {crop_width:g} × {crop_height:g} nm | full-grid rows [{r0}, {r1}), columns [{c0}, {c1})\n"
               if crop is not None else "")
    features = "\nCircles = unverified image features; full IDs in JSON" if candidates else ""
    figure.suptitle(
        f"{observation.source_path.name}\n"
        f"{frame.width_m * 1e9:g} × {frame.height_m * 1e9:g} nm | "
        f"{observation.bias_v * 1e3:g} mV / {setpoint} | {observation.scan_direction} | "
        f"{100 * observation.valid_mask.mean():.1f}% finite\n"
        f"{details}Equal physical scale | gray-blue = unacquired | shared forward/retrace color ranges{features}",
        fontsize=10,
    )
    buffer = BytesIO()
    figure.savefig(buffer, format="png", dpi=150)
    return buffer.getvalue()
