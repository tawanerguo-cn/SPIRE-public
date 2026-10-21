from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workflow_layer import (  # noqa: E402
    analyze_tip_evidence,
    verify_reviewed_tip_reference,
)
from workflow_layer.tip_reference_verification import rate_tip_quality_card
from knowledge_base.tip_quality_reference import PrivateReferenceSourcesUnavailable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze compact post-poke scan evidence and Ag(111) dI/dV, or "
            "reproduce a reviewed SPiRE tip-quality reference."
        )
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze = subparsers.add_parser("analyze", help="Analyze a new scan/spectrum pair.")
    analyze.add_argument("--scan", type=Path, required=True)
    analyze.add_argument("--spectrum", type=Path, required=True)
    verify = subparsers.add_parser(
        "verify",
        help="Recompute and hash-check a reviewed knowledge reference.",
    )
    verify.add_argument("--reference-id", default="ag111_short_scan_reference")
    rate = subparsers.add_parser("rate", help="Hash-check and grade an explicit review card.")
    rate.add_argument("--card", type=Path, required=True)
    rate.add_argument("--evidence-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    if args.command == "rate":
        card = json.loads(args.card.read_text(encoding="utf-8"))
        result = rate_tip_quality_card(card, evidence_root=args.evidence_root)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if result["status"] == "complete_supplied_operator_review" else 2

    if args.command == "analyze":
        result = analyze_tip_evidence(args.scan, args.spectrum)
        if args.json:
            print(json.dumps(asdict(result), ensure_ascii=False, default=str))
        else:
            _print_analysis(result)
        return 0

    try:
        result = verify_reviewed_tip_reference(args.reference_id)
    except PrivateReferenceSourcesUnavailable as error:
        print(str(error), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(asdict(result) | {"reproduced": result.reproduced}, default=str))
    else:
        _print_analysis(result.analysis)
        print(f"reference_id={result.reference_id}")
        print(f"reference_reproduced={str(result.reproduced).lower()}")
        for check in result.checks:
            if not check.passed:
                print(
                    f"mismatch={check.name} expected={check.expected} actual={check.actual}"
                )
    return 0 if result.reproduced else 3


def _print_analysis(result) -> None:
    scan = result.scan
    spectrum = result.spectrum
    print(f"scan={result.scan_path}")
    print(f"scan_sha256={result.scan_sha256}")
    print(f"scan_acquired_rows={scan.acquired_rows}/{scan.total_rows}")
    print(
        "protrusion_center_pixel="
        f"{scan.center_column},{scan.center_row}"
    )
    print(f"protrusion_height_nm={scan.protrusion_height_m * 1e9:.6f}")
    print(f"protrusion_fwhm_x_nm={scan.protrusion_fwhm_x_m * 1e9:.6f}")
    print(f"protrusion_fwhm_y_nm={scan.protrusion_fwhm_y_m * 1e9:.6f}")
    print(f"protrusion_aspect_ratio={scan.protrusion_fwhm_aspect_ratio:.6f}")
    print(
        "protrusion_equivalent_fwhm_radius_nm="
        f"{scan.protrusion_equivalent_fwhm_radius_m * 1e9:.6f}"
    )
    print(
        "z_forward_backward_correlation="
        f"{scan.z_forward_backward_correlation:.6f}"
    )
    print(f"compact_candidates={scan.candidate_count}")
    print(f"spectrum={result.spectrum_path}")
    print(f"spectrum_sha256={result.spectrum_sha256}")
    print(f"spectrum_points={spectrum.points}")
    print(f"surface_state_onset_mv={spectrum.surface_state_onset_v * 1e3:.6f}")
    print(f"didv_step_pa={spectrum.didv_step_a * 1e12:.6f}")
    print(
        "didv_detrended_noise_rms_pa="
        f"{spectrum.didv_detrended_noise_rms_a * 1e12:.6f}"
    )
    print(f"didv_step_to_noise={spectrum.didv_step_to_noise:.6f}")
    print("analysis_only=true")
    print("authorizes_live_action=false")


if __name__ == "__main__":
    raise SystemExit(main())
