from __future__ import annotations

from dataclasses import dataclass
import math
import json
from pathlib import Path
import time

from analysis_layer import (
    Ag111SpectrumMetrics,
    CompactProtrusionMetrics,
    analyze_ag111_spectrum,
    analyze_compact_protrusion,
)
from analysis_layer.tip_quality import aggregate_tip_quality_scores
from analysis_layer.tip_quality import score_quantitative_tip_metrics
from analysis_layer.tip_reference_verification import measure_retrace_pair, measure_poke_shape
from analysis_layer.signal_windows import analyze_signal_window
from data_layer import inspect_experiment_artifact, read_nanonis_dat, read_nanonis_sxm
from knowledge_base import (
    DEFAULT_TIP_QUALITY_REFERENCE_DIRECTORY,
    DEFAULT_TIP_QUALITY_REFERENCE_ID,
    TipQualityEvidence,
    load_tip_quality_reference,
    load_tip_quality_rubric,
)
from knowledge_base.tip_quality_reference import PrivateReferenceSourcesUnavailable


def quantify_basic_tip_quality(request: dict, *, evidence_root: str | Path) -> dict:
    """Offline scan/noise/poke report through the existing data and analysis layers."""
    rubric = load_tip_quality_rubric()
    material = request['material']
    if material not in rubric['materials']:
        raise ValueError("unknown material")
    if not isinstance(request.get('conditions'), str) or not request['conditions'].strip():
        raise ValueError("measurement conditions are required")
    root = Path(evidence_root).resolve()

    def source(relative):
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError("evidence path must stay within evidence_root")
        return path

    scan = read_nanonis_sxm(source(request['scan']))
    forward = scan.oriented_channel('Z', 'forward')
    backward = scan.oriented_channel('Z', 'backward')

    def crop(key):
        bounds = request[key]
        if len(bounds) != 4 or any(type(v) is not int for v in bounds):
            raise ValueError("ROI requires four integer row/column bounds")
        r0, r1, c0, c1 = bounds
        if not (0 <= r0 < r1 <= scan.rows and 0 <= c0 < c1 <= scan.columns):
            raise ValueError("ROI is outside the scan")
        return forward[r0:r1,c0:c1], backward[r0:r1,c0:c1]

    background_roi = request.get('background_roi')
    retrace = measure_retrace_pair(*(crop('background_roi') if background_roi is not None else (forward, backward)))
    issues = []
    shape = None
    if request.get('poke_roi') is not None:
        try:
            shape = measure_poke_shape(*crop('poke_roi'), pixel_x_m=scan.range_x_m/scan.columns,
                                       pixel_y_m=scan.range_y_m/scan.rows)
        except ValueError as error:
            issues.append(f"poke: {error}")
    signal_path = source(request['signal_window']) if request.get('signal_window') is not None else None
    # Supports the existing observe_noise receipt or an exported IXYZ window.
    signal_data = json.loads(signal_path.read_text(encoding='utf-8')) if signal_path is not None else None
    if signal_data is None:
        signal = None
    elif signal_data.get('analysis_version') == 'signal-window-v2' and signal_data.get('source_rows_sha256'):
        signal = signal_data
    elif 'rows' in signal_data:
        rows, context = signal_data['rows'], signal_data['context']
        signal = analyze_signal_window(rows, **context)
    else:
        rows = signal_data['result']['result']['rows']
        context = signal_data['result']['result']['analysis']['supplied_context']
        signal = analyze_signal_window(rows, **context)
    valid_signal = signal is not None and not signal['quality_flags']
    if signal is not None and not valid_signal:
        issues.extend(signal['quality_flags'])
    metrics = signal['metrics'] if signal is not None else {}
    groups = {
        'retrace': {key:retrace[key] for key in ('retrace_rms_m','retrace_correlation')},
        'current': {key:metrics.get(key) if valid_signal else None
                    for key in ('current_rms_fraction','current_setpoint_error_fraction')},
        'poke': {key:shape[key] if shape else None
                 for key in ('principal_aspect_ratio','secondary_lobe_fraction')},
    }
    calibration = request.get('calibration')
    if calibration is not None:
        if calibration.get('material') != material or calibration.get('purpose') != 'basic_conditioning':
            raise ValueError("calibration must match material and purpose")
        if not calibration.get('id') or not calibration.get('source'):
            raise ValueError("calibration needs an id and anchor source")
    score = score_quantitative_tip_metrics(groups, {} if calibration is None else calibration['anchors'])
    signal_artifact = (inspect_experiment_artifact(signal_path,
        observed_monotonic_ns=time.perf_counter_ns(), observed_utc_ns=time.time_ns()) if signal_path is not None else None)
    def unit(value, factor):
        return value * factor if value is not None else None
    brief = {
        'background': {'selection': 'explicit ROI' if background_roi is not None else 'all acquired pixels; select a background ROI when features are present',
            'paired_fraction': retrace['paired_fraction'],
            'paired_pixels': retrace['paired_pixels'],
            'retrace_rms_pm': unit(retrace['retrace_rms_m'], 1e12),
            'mean_image_residual_rms_pm': unit(retrace['structure_rms_m'], 1e12),
            'retrace_correlation': retrace['retrace_correlation'],
            'interpretation': 'Compare absolute height and target visibility; weak correlation on a flat noise-dominated background is not a failure grade.'},
        'current': {'rms_pa': unit(metrics.get('current_rms_a'),1e12),
                    'rms_fraction': metrics.get('current_rms_fraction'),
                    'setpoint_error_fraction': metrics.get('current_setpoint_error_fraction')} if valid_signal else None,
        'poke': {'equivalent_fwhm_radius_nm': unit(shape['equivalent_fwhm_radius_m'],1e9),
                 'principal_aspect_ratio': shape['principal_aspect_ratio'],
                 'half_height_components': shape['half_height_components'],
                 'minimum_relative_height_pm': unit(shape['minimum_relative_height_m'],1e12),
                 'border_residual_rms_pm': unit(shape['border_residual_rms_m'],1e12)} if shape is not None else None,
        'automatic_acceptance': False,
    }
    return {
        'analysis_version':'basic-tip-quantitative-v1', 'material':material,
        'purpose':'basic_conditioning', 'conditions':request['conditions'],
        'scan':{'path':str(scan.path),'sha256':scan.sha256,'bias_v':scan.bias_v,
                'setpoint_a':scan.z_setpoint_a,'background_roi':background_roi,
                'poke_roi':request.get('poke_roi'), 'roi_coordinates':'oriented SXM top-origin, exclusive stops'},
        'signal_source':{'path':str(signal_path),'sha256':signal_artifact.sha256} if signal_artifact is not None else None,
        'poke_depth_m_supplied':request.get('poke_depth_m'),
        'retrace':retrace,'current':signal,'poke':shape,'issues':issues,'brief':brief,
        'calibration':calibration, 'quantitative':score,
        'status':'metrics_only' if calibration is None else 'scored' if score['score'] is not None else 'incomplete',
        'authorizes_live_action':False,
    }


def rate_tip_quality_card(card: dict, *, evidence_root: str | Path) -> dict:
    """Hash-check file evidence and aggregate explicit human/analyst grades.

    Reviewer metadata is supplied by the caller, not authenticated here. No
    metrics-to-grade calibration, automatic acceptance, or instrument access.
    """
    rubric = load_tip_quality_rubric()
    purpose = card.get("purpose")
    required = rubric["required_axes"].get(purpose)
    if required is None:
        raise ValueError("unknown rating purpose")
    material = card.get("material")
    if material not in rubric["materials"]:
        raise ValueError("material must select a registered material")
    if not isinstance(card.get("conditions"), str) or not card["conditions"].strip():
        raise ValueError("rating card requires measurement conditions and unknowns")
    comparable = card.get("comparison_conditions_verified")
    if comparable is not None and type(comparable) is not bool:
        raise ValueError("comparison_conditions_verified must be boolean or null")
    root = Path(evidence_root).resolve()
    verified = {}
    for evidence in card.get("evidence", []):
        identifier = evidence["id"]
        if not isinstance(identifier, str) or not identifier or identifier in verified:
            raise ValueError("evidence ids must be nonempty and unique")
        source = (root / evidence["path"]).resolve()
        if not source.is_relative_to(root):
            raise ValueError("evidence path must stay within evidence_root")
        actual = inspect_experiment_artifact(
            source, observed_monotonic_ns=time.perf_counter_ns(), observed_utc_ns=time.time_ns(),
        )
        if actual.sha256 != evidence["sha256"]:
            raise ValueError(f"evidence hash mismatch: {identifier}")
        verified[identifier] = {"path": str(source), "sha256": actual.sha256}
    axes = card.get("axes", {})
    if set(axes) != set(rubric["axes"]):
        raise ValueError("rating card must explicitly include all three axes")
    scores = {}
    analyst_axes = []
    for name, axis in axes.items():
        scores[name] = axis.get("score")
        if scores[name] is None:
            continue
        if not isinstance(axis.get("reason"), str) or not axis["reason"].strip():
            raise ValueError(f"scored axis needs an explanation: {name}")
        identifiers = axis.get("evidence_ids", [])
        if not identifiers or any(identifier not in verified for identifier in identifiers):
            raise ValueError(f"scored axis needs verified evidence: {name}")
        if axis.get("source_kind") not in {"operator_review", "analyst_assessment"}:
            raise ValueError(f"scored axis needs explicit review provenance: {name}")
        if not isinstance(axis.get("reviewer"), str) or not axis["reviewer"].strip():
            raise ValueError(f"scored axis needs a reviewer: {name}")
        if axis["source_kind"] == "analyst_assessment" and name in required:
            analyst_axes.append(name)
    ordinal = aggregate_tip_quality_scores(scores, required_axes=required)
    missing = [name for name in required if scores[name] is None]
    if comparable is not True:
        ordinal = None
    status = (
        "conditions_unverified" if comparable is not True else
        "incomplete" if missing else "provisional_analyst_review" if analyst_axes else
        "complete_supplied_operator_review"
    )
    return {
        "rubric_version": rubric["schema_version"], "material": material, "purpose": purpose,
        "conditions": card["conditions"], "axes": axes, "required_axes": required,
        "missing_axes": missing, "score": ordinal,
        "grade": None if ordinal is None else rubric["score_labels"][ordinal],
        "status": status, "verified_evidence": verified,
        "historical_operator_verdict": card.get("historical_operator_verdict"),
        "reviewer_identity_authenticated": False,
        "automated_metric_thresholds_calibrated": False,
        "automatic_acceptance": False, "authorizes_live_action": False,
    }


@dataclass(frozen=True)
class TipEvidenceAnalysisResult:
    scan_path: Path
    scan_sha256: str
    scan: CompactProtrusionMetrics
    spectrum_path: Path
    spectrum_sha256: str
    spectrum: Ag111SpectrumMetrics


@dataclass(frozen=True)
class ReferenceReproductionCheck:
    name: str
    passed: bool
    expected: str | int | float | bool
    actual: str | int | float | bool


@dataclass(frozen=True)
class TipReferenceReproductionResult:
    reference_id: str
    analysis: TipEvidenceAnalysisResult
    checks: tuple[ReferenceReproductionCheck, ...]

    @property
    def reproduced(self) -> bool:
        return all(check.passed for check in self.checks)


def analyze_tip_evidence(
    scan_path: str | Path,
    spectrum_path: str | Path,
) -> TipEvidenceAnalysisResult:
    scan_file = read_nanonis_sxm(scan_path)
    spectrum_file = read_nanonis_dat(spectrum_path)
    return TipEvidenceAnalysisResult(
        scan_path=scan_file.path,
        scan_sha256=scan_file.sha256,
        scan=analyze_compact_protrusion(scan_file),
        spectrum_path=spectrum_file.path,
        spectrum_sha256=spectrum_file.sha256,
        spectrum=analyze_ag111_spectrum(spectrum_file),
    )


def verify_reviewed_tip_reference(
    reference_id: str = DEFAULT_TIP_QUALITY_REFERENCE_ID,
    *,
    reference_directory: str | Path = DEFAULT_TIP_QUALITY_REFERENCE_DIRECTORY,
) -> TipReferenceReproductionResult:
    reference = load_tip_quality_reference(
        reference_id,
        reference_directory=reference_directory,
    )
    if not reference.source_available:
        raise PrivateReferenceSourcesUnavailable(
            f"Reference {reference_id!r} contains historical summary metrics, but its "
            "private scan, spectrum, and run-store files are not included in this release. "
            "Use the analyze command with your own scan and spectrum to inspect new data."
        )
    scan_evidence = _evidence_with_role(
        reference.evidence,
        "primary_human_quick_check_after_poke",
    )
    spectrum_evidence = _evidence_with_role(
        reference.evidence,
        "primary_sts_quality_evidence",
    )
    analysis = analyze_tip_evidence(
        scan_evidence.source_path,
        spectrum_evidence.source_path,
    )
    checks = [
        _exact("scan.sha256", scan_evidence.sha256, analysis.scan_sha256),
        _exact(
            "scan.acquired_rows",
            int(scan_evidence.metric("acquired_rows")),
            analysis.scan.acquired_rows,
        ),
        _close(
            "scan.protrusion_height_m",
            float(scan_evidence.metric("protrusion_height_m")),
            analysis.scan.protrusion_height_m,
        ),
        _close(
            "scan.protrusion_fwhm_x_m",
            float(scan_evidence.metric("protrusion_fwhm_x_m")),
            analysis.scan.protrusion_fwhm_x_m,
        ),
        _close(
            "scan.protrusion_fwhm_y_m",
            float(scan_evidence.metric("protrusion_fwhm_y_m")),
            analysis.scan.protrusion_fwhm_y_m,
        ),
        _close(
            "scan.protrusion_fwhm_aspect_ratio",
            float(scan_evidence.metric("protrusion_fwhm_aspect_ratio")),
            analysis.scan.protrusion_fwhm_aspect_ratio,
        ),
        _close(
            "scan.protrusion_equivalent_fwhm_radius_m",
            float(
                scan_evidence.metric("protrusion_equivalent_fwhm_radius_m")
            ),
            analysis.scan.protrusion_equivalent_fwhm_radius_m,
        ),
        _close(
            "scan.z_forward_backward_correlation",
            float(scan_evidence.metric("z_forward_backward_correlation")),
            analysis.scan.z_forward_backward_correlation,
        ),
        _exact(
            "spectrum.sha256",
            spectrum_evidence.sha256,
            analysis.spectrum_sha256,
        ),
        _exact(
            "spectrum.points",
            int(spectrum_evidence.metric("points")),
            analysis.spectrum.points,
        ),
        _close(
            "spectrum.surface_state_onset_v",
            float(spectrum_evidence.metric("surface_state_onset_v")),
            analysis.spectrum.surface_state_onset_v,
        ),
        _close(
            "spectrum.didv_step_a",
            float(spectrum_evidence.metric("didv_step_a")),
            analysis.spectrum.didv_step_a,
        ),
        _close(
            "spectrum.didv_detrended_noise_rms_a",
            float(spectrum_evidence.metric("didv_detrended_noise_rms_a")),
            analysis.spectrum.didv_detrended_noise_rms_a,
        ),
        _close(
            "spectrum.didv_step_to_noise",
            float(spectrum_evidence.metric("didv_step_to_noise")),
            analysis.spectrum.didv_step_to_noise,
        ),
    ]
    return TipReferenceReproductionResult(
        reference_id=reference_id,
        analysis=analysis,
        checks=tuple(checks),
    )


def _evidence_with_role(
    evidence: tuple[TipQualityEvidence, ...],
    role: str,
) -> TipQualityEvidence:
    matches = tuple(item for item in evidence if item.role == role)
    if len(matches) != 1:
        raise ValueError(
            f"reference must contain exactly one {role!r} evidence entry"
        )
    return matches[0]


def _exact(name: str, expected, actual) -> ReferenceReproductionCheck:
    return ReferenceReproductionCheck(name, expected == actual, expected, actual)


def _close(name: str, expected: float, actual: float) -> ReferenceReproductionCheck:
    return ReferenceReproductionCheck(
        name,
        math.isclose(expected, actual, rel_tol=1e-9, abs_tol=1e-16),
        expected,
        actual,
    )
