from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analysis_layer import AtomDetectorSettings
from workflow_layer.scan_observation import export_saved_scan_observation, inspect_saved_scan


def main() -> int:
    parser = argparse.ArgumentParser(description="Read a saved SXM; report geometry, masks and candidate features. No instrument connection.")
    parser.add_argument("scan", type=Path)
    parser.add_argument("--output", type=Path, help="New directory for PNGs, arrays and provenance; existing directories are refused.")
    parser.add_argument("--processing", choices=("raw", "plane", "line_median", "line_linear"), default="plane")
    view = parser.add_mutually_exclusive_group()
    view.add_argument("--acquired-only", action="store_true", help="Preview the acquired bounding box at true physical aspect; retain full arrays and missing pixels.")
    view.add_argument("--crop", type=int, nargs=4, metavar=("ROW_START", "ROW_STOP", "COLUMN_START", "COLUMN_STOP"),
                      help="Preview a full-grid pixel crop at true physical aspect; stops are exclusive.")
    parser.add_argument("--threshold-sigma", type=float, default=3.0)
    parser.add_argument("--minimum-separation-pixels", type=float, default=2.0)
    args = parser.parse_args()
    if (args.acquired_only or args.crop is not None) and args.output is None:
        parser.error("preview cropping requires --output")
    settings = AtomDetectorSettings(threshold_sigma=args.threshold_sigma,
                                    minimum_separation_pixels=args.minimum_separation_pixels)
    kwargs = {"processing": args.processing, "detector_settings": settings}
    if args.output:
        result = export_saved_scan_observation(args.scan, args.output,
            crop=tuple(args.crop) if args.crop is not None else None, acquired_only=args.acquired_only, **kwargs)
    else:
        _, _, result = inspect_saved_scan(args.scan, **kwargs)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
