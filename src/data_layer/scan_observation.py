"""Immutable, source-bound observations; arrays use the full top-origin scan grid."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json

import numpy as np
from numpy.typing import NDArray

from instrument_layer import ScanFrame


@dataclass(frozen=True)
class ScanObservation:
    observation_id: str
    source_path: Path
    source_sha256: str
    recorded_date: str
    recorded_time: str
    frame: ScanFrame
    channel_name: str
    channel_unit: str
    scan_direction: str
    bias_v: float
    setpoint_a: float | None
    raw: NDArray[np.floating]
    processed: NDArray[np.floating]
    backward: NDArray[np.floating] | None
    valid_mask: NDArray[np.bool_]
    processing: str
    metadata: tuple[tuple[str, str], ...]

    @property
    def rows(self) -> int:
        return self.raw.shape[0]

    @property
    def columns(self) -> int:
        return self.raw.shape[1]

    def summary(self) -> dict:
        from dataclasses import asdict

        valid = self.raw[self.valid_mask]
        return {
            "observation_id": self.observation_id,
            "source_path": str(self.source_path), "source_sha256": self.source_sha256,
            "recorded_date": self.recorded_date, "recorded_time": self.recorded_time,
            "frame": asdict(self.frame), "rows": self.rows, "columns": self.columns,
            "channel_name": self.channel_name, "channel_unit": self.channel_unit,
            "scan_direction": self.scan_direction, "bias_v": self.bias_v,
            "setpoint_a": self.setpoint_a, "processing": self.processing,
            "orientation": "top-left origin; columns +local_x; rows -local_y; clockwise frame rotation",
            "valid_pixels": int(self.valid_mask.sum()),
            "valid_fraction": float(self.valid_mask.mean()),
            "complete_rows": int(self.valid_mask.all(axis=1).sum()),
            "raw_min": float(valid.min()) if valid.size else None,
            "raw_max": float(valid.max()) if valid.size else None,
            "sample_identity": "unverified; filename and header retained as source evidence only",
            "metadata": dict(self.metadata),
        }


def write_observation_bundle(
    observation: ScanObservation, output_directory: str | Path,
    *, summary: dict, images: dict[str, bytes],
) -> Path:
    """Create a new directory, never overwrite sources or existing derived results."""
    destination = Path(output_directory).resolve()
    for name in images:
        if Path(name).name != name or not name.endswith(".png"):
            raise ValueError("image names must be local PNG basenames")
    serialized = json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False)
    destination.mkdir(parents=True, exist_ok=False)
    for name, payload in images.items():
        with (destination / name).open("xb") as stream:
            stream.write(payload)
    with (destination / "observation.json").open("x", encoding="utf-8") as stream:
        stream.write(serialized)
        stream.write("\n")
    # Original file remains authoritative. This captures the exact processed arrays
    # underlying the previews, with NaNs and masks preserved, not imputed as zeros.
    with (destination / "arrays.npz").open("xb") as stream:
        values = {"raw": observation.raw, "processed": observation.processed, "valid_mask": observation.valid_mask}
        if observation.backward is not None:
            values["backward"] = observation.backward
        np.savez_compressed(stream, **values)
    return destination
