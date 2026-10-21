"""Compose existing data readers and analysis into reproducible observations."""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path

import numpy as np

from analysis_layer import AtomDetectorSettings, LocalMaximumAtomDetector
from analysis_layer.scan_images import process_scan_image, render_scan_observation, scan_quality_metrics
from data_layer import read_nanonis_sxm
from data_layer.scan_observation import ScanObservation, write_observation_bundle


def observe_saved_scan(path: str | Path, *, channel: str = "Z", processing: str = "plane") -> ScanObservation:
    scan = read_nanonis_sxm(path)
    selected = scan.channel(channel)
    raw = scan.oriented_channel(channel)
    processed = process_scan_image(raw, processing)
    backward = scan.oriented_channel(channel, "backward") if selected.backward is not None else None
    valid = np.isfinite(raw)
    valid.setflags(write=False)
    identity = json.dumps([scan.sha256, selected.name, processing, "sxm-top-origin-v1"])
    keep = {"COMMENT", "Scan>series name", "Piezo Configuration>Active Calib.",
            "Lock-in>Lock-in status", "Lock-in>Frequency (Hz)", "Lock-in>Amplitude",
            "Bias Spectroscopy>Channels", "Bias Spectroscopy>Lock-In run"}
    return ScanObservation(
        "scan-" + sha256(identity.encode()).hexdigest()[:24], scan.path.resolve(), scan.sha256,
        scan.recorded_date, scan.recorded_time, scan.scan_frame, selected.name, selected.unit,
        scan.scan_direction, scan.bias_v, scan.z_setpoint_a,
        raw, processed, backward, valid, processing,
        tuple((key, value) for key, value in scan.header if key in keep),
    )


def inspect_saved_scan(
    path: str | Path, *, channel: str = "Z", processing: str = "plane",
    detector_settings: AtomDetectorSettings | None = None,
) -> tuple[ScanObservation, tuple, dict]:
    observation = observe_saved_scan(path, channel=channel, processing=processing)
    detector = LocalMaximumAtomDetector(detector_settings)
    candidates = detector.detect(observation.processed, observation.frame, valid_mask=observation.valid_mask)
    summary = observation.summary() | {
        "quality": scan_quality_metrics(observation),
        "detector": {"name": type(detector).__name__, "settings": asdict(detector.settings),
                     "status": "baseline_not_lab_validated", "score_kind": "robust_height_not_probability"},
        "candidates": [asdict(candidate) for candidate in candidates],
        "authorizes_live_action": False,
    }
    return observation, candidates, summary


def export_saved_scan_observation(
    path: str | Path, output_directory: str | Path, *, crop=None, acquired_only: bool = False, **kwargs,
) -> dict:
    observation, candidates, summary = inspect_saved_scan(path, **kwargs)
    if acquired_only:
        if crop is not None:
            raise ValueError("select either an explicit crop or acquired_only")
        visible = observation.valid_mask.copy()
        if observation.backward is not None:
            visible |= np.isfinite(observation.backward)
        row, column = np.nonzero(visible)
        if not row.size:
            raise ValueError("scan has no acquired pixels to display")
        crop = (int(row.min()), int(row.max()) + 1, int(column.min()), int(column.max()) + 1)
    # Only the preview is cropped. Full-grid arrays, masks and source remain intact.
    summary = summary | {"preview_crop": list(crop) if crop is not None else None,
                         "preview_scale": "equal physical scale; local x/y in nm"}
    destination = write_observation_bundle(
        observation, output_directory, summary=summary,
        images={"overview.png": render_scan_observation(observation, crop=crop),
                "candidates.png": render_scan_observation(observation, candidates=candidates, crop=crop)},
    )
    return summary | {"output_directory": str(destination)}
