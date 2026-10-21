"""Deterministic I/Z measurements. No I/O, instrument actions or tip-quality claims."""
import math

import numpy as np


STABILITY_LIMIT_METRICS = (
    "current_difference_noise_a", "current_maximum_jump_a", "current_drift_a_per_s",
    "current_setpoint_error_fraction", "z_difference_noise_m", "z_maximum_jump_m",
    "z_drift_m_per_s", "xy_span_m", "current_rms_fraction",
)


def compare_pulse_windows(before, during, after, *, minimum_duration_s,
                          maximum_sample_gap_s, expected_current_a,
                          current_full_scale_a=None):
    """Three pulse response facts; no combined score or action decision.

    During is the sampled typed-command envelope, not a hardware-triggered
    waveform. A missed peak is unknown; near rail means >=99% of supplied range.
    """
    options = dict(minimum_duration_s=minimum_duration_s,
                   maximum_sample_gap_s=maximum_sample_gap_s,
                   expected_current_a=expected_current_a)
    pre = analyze_signal_window(before, **options)
    post = analyze_signal_window(after, **options)
    if current_full_scale_a is not None:
        _positive(current_full_scale_a, "current_full_scale_a")
    body = np.asarray(during, dtype=float)
    valid_body = bool(body.ndim == 2 and body.shape[1] == 8 and len(body)
                      and np.isfinite(body).all())
    peak = float(np.max(np.abs(body[:, 4]))) if valid_body else None
    valid = pre["status"] != "invalid_data" and post["status"] != "invalid_data"
    same_xy = bool(valid and np.ptp(np.vstack((before, after))[:, 5:7], axis=0).max() == 0)
    valid = valid and same_xy
    a, b = pre["metrics"], post["metrics"]
    delta = b["z_median_m"] - a["z_median_m"] if valid else None
    ratio = (b["current_rms_a"] / a["current_rms_a"]
             if valid and a["current_rms_a"] > 0 else None)
    return {
        "peak_abs_current_a": peak,
        "near_full_scale": None if peak is None or current_full_scale_a is None
                           else peak >= .99 * current_full_scale_a,
        "current_full_scale_a": current_full_scale_a,
        "pulse_sample_count": len(body) if valid_body else 0,
        "peak_scope": "sampled_command_window", "peak_complete": False,
        "current_before_a": a.get("current_median_a"),
        "current_after_a": b.get("current_median_a"),
        "current_rms_before_a": a.get("current_rms_a"),
        "current_rms_after_a": b.get("current_rms_a"),
        "current_rms_ratio": ratio,
        "current_after_setpoint_error_fraction": b.get("current_setpoint_error_fraction"),
        "z_before_m": a.get("z_median_m"), "z_after_m": b.get("z_median_m"),
        "delta_z_m": delta,
        "z_direction": ("unknown" if delta is None else
                        "increased" if delta > 0 else "decreased" if delta < 0 else "unchanged"),
        "comparison_valid": valid,
        "same_xy": same_xy,
        "window_flags": {"before": pre["quality_flags"], "after": post["quality_flags"]},
    }


def continuous_signal_samples(rows, maximum_sample_gap_s):
    """Shared IXYZ provenance rule, including recognizer windows."""
    return bool(len(rows) >= 2 and np.all(np.diff(rows[:, 0]) == 1)
                and np.ptp(rows[:, 1]) == 0 and np.all(np.diff(rows[:, 2]) > 0)
                and np.diff(rows[:, 2]).max() / 1e9 <= maximum_sample_gap_s)


def _positive(value, name):
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


def _channel_metrics(t, values):
    differences = np.diff(values)
    centered = t - np.mean(t)
    slope = float(np.dot(centered, values - np.mean(values)) / np.dot(centered, centered))
    return {
        "median": float(np.median(values)),
        "difference_noise": float(1.4826 * np.median(np.abs(differences - np.median(differences))) / math.sqrt(2)),
        "maximum_jump": float(np.max(np.abs(differences))),
        "drift": abs(slope), "peak_to_peak": float(np.ptp(values)),
    }


def analyze_signal_window(rows, *, minimum_duration_s, maximum_sample_gap_s,
                          expected_current_a=None, current_full_scale_a=None,
                          z_limits_m=None, analysis_limits=None):
    """Measure a selected window. Caller-supplied criteria are not SafetyGate bounds.

    A stable_candidate describes signals only, not permission to move or pulse.
    expected_current_a is the setpoint magnitude; signed current is retained.
    Settling, stationary feedback and unchanged acquisition settings need separate
    action/state evidence. No pass thresholds are inferred from this window.
    """
    _positive(minimum_duration_s, "minimum_duration_s")
    _positive(maximum_sample_gap_s, "maximum_sample_gap_s")
    if expected_current_a is not None:
        _positive(abs(expected_current_a), "absolute expected_current_a")
    if current_full_scale_a is not None:
        _positive(current_full_scale_a, "current_full_scale_a")
    if z_limits_m is not None:
        if len(z_limits_m) != 2 or not all(math.isfinite(v) for v in z_limits_m) or z_limits_m[0] >= z_limits_m[1]:
            raise ValueError("z_limits_m must contain ordered finite endpoints")
    if analysis_limits is not None:
        if set(analysis_limits) != set(STABILITY_LIMIT_METRICS):
            raise ValueError("analysis_limits must specify all STABILITY_LIMIT_METRICS")
        for key, value in analysis_limits.items():
            _positive(value, key)
    result = {
        "analysis_version": "signal-window-v2", "status": "invalid_data", "metrics": {},
        "quality_flags": [], "threshold_failures": [], "authorizes_live_action": False,
        "scope": "Selected I/Z window only; no pulse identity, tip quality or live readiness inferred.",
        "required_context": "Settled stationary feedback, unchanged bias/setpoint/gain/bandwidth, no scan/STS/approach; verify using action/state evidence.",
        "analysis_limits": analysis_limits,
        "supplied_context": {"expected_current_a": expected_current_a,
            "current_full_scale_a": current_full_scale_a, "z_limits_m": z_limits_m,
            "minimum_duration_s": minimum_duration_s, "maximum_sample_gap_s": maximum_sample_gap_s},
    }
    data = np.asarray(rows, dtype=float)
    if data.ndim != 2 or data.shape[1] != 8 or data.shape[0] < 3:
        result["quality_flags"].append("insufficient_samples_or_wrong_shape")
        return result
    if not np.all(np.isfinite(data)):
        result["quality_flags"].append("nonfinite_data")
        return result
    result["sample_count"] = len(data)
    result["first_seq"], result["last_seq"] = int(data[0, 0]), int(data[-1, 0])
    if not continuous_signal_samples(data, maximum_sample_gap_s):
        result["quality_flags"].append("discontinuous_or_cross_segment")
        return result
    t = (data[:, 2] - data[0, 2]) / 1e9
    result["duration_s"] = float(t[-1])
    result["median_sample_interval_s"] = float(np.median(np.diff(t)))
    if t[-1] < minimum_duration_s:
        result["quality_flags"].append("short_window")
    current, z = data[:, 4], data[:, 7]
    if np.ptp(current) == 0 and np.ptp(z) == 0:
        result["quality_flags"].append("constant_both_channels_possible_frozen_data")
    if current_full_scale_a is not None and np.any(np.abs(current) >= current_full_scale_a):
        result["quality_flags"].append("current_at_or_beyond_supplied_range")
    if z_limits_m is not None and (np.min(z) <= z_limits_m[0] or np.max(z) >= z_limits_m[1]):
        result["quality_flags"].append("z_at_or_beyond_supplied_limits")
    im, zm = _channel_metrics(t, current), _channel_metrics(t, z)
    result["metrics"] = {
        "current_median_a": im["median"], "current_difference_noise_a": im["difference_noise"],
        "current_maximum_jump_a": im["maximum_jump"], "current_drift_a_per_s": im["drift"],
        "current_peak_to_peak_a": im["peak_to_peak"],
        # Whole-window fluctuation retains bursts hidden by robust difference MAD.
        # These sample-domain quantities are not band-integrated spectral noise.
        "current_rms_a": float(np.std(current)),
        "current_rms_fraction": None if expected_current_a is None else float(np.std(current) / abs(expected_current_a)),
        "current_abs_p95_a": float(np.percentile(np.abs(current), 95)),
        "current_abs_p99_a": float(np.percentile(np.abs(current), 99)),

        "current_setpoint_error_fraction": None if expected_current_a is None else abs(abs(im["median"]) - abs(expected_current_a)) / abs(expected_current_a),
        "z_median_m": zm["median"], "z_difference_noise_m": zm["difference_noise"],
        "z_maximum_jump_m": zm["maximum_jump"], "z_drift_m_per_s": zm["drift"],
        "z_peak_to_peak_m": zm["peak_to_peak"],
        "xy_span_m": float(np.hypot(np.ptp(data[:, 5]), np.ptp(data[:, 6]))),
    }
    result["missing_assessment_inputs"] = [name for name, value in (
        ("expected_current_a", expected_current_a), ("current_full_scale_a", current_full_scale_a),
        ("z_limits_m", z_limits_m), ("analysis_limits", analysis_limits)) if value is None]
    if result["quality_flags"]:
        return result
    if result["missing_assessment_inputs"]:
        result["status"] = "metrics_only"
        return result
    result["threshold_failures"] = [key for key, limit in analysis_limits.items() if result["metrics"][key] > limit]
    result["status"] = "unstable" if result["threshold_failures"] else "stable_candidate"
    return result
