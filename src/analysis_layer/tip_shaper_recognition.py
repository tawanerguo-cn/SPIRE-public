"""Bounded, streaming recognition of tip-shaper-like I/Z trajectories.

No I/O or instrument commands. A waveform match never establishes GUI command
identity, bias-pulse history, mechanical contact, or a successful tip outcome.
"""
from collections import deque
import math
import numpy as np
from .signal_windows import continuous_signal_samples


def _possible_clipping(current, peak):
    return bool(np.sum(np.isclose(np.abs(current), peak, rtol=1e-6, atol=0)) >= 3)


def _fit(rows, seed_ns, parameters, policy):
    data = np.asarray(rows, dtype=float)
    t = (data[:, 2] - seed_ns) / 1e9
    depth = float(parameters["tip_lift_m"])
    rise = float(parameters["lift_height_m"])
    ramp1 = float(parameters["lift_time_1_s"])
    dwell = float(parameters["bias_settling_time_s"])
    ramp2 = float(parameters["lift_time_2_s"])
    duration = ramp1 + dwell + ramp2
    if not (depth < 0 < rise and min(ramp1, ramp2) > 0 and dwell >= 0 and duration <= 3):
        return None
    best = None
    # The current trigger lies somewhere in the approaching ramp. Fit its onset
    # at acquisition resolution, including a short pre-ramp baseline.
    for onset in np.arange(-ramp1 - .12, .041, .01):
        phase = t - onset
        selected = (phase >= -.1) & (phase <= duration)
        if selected.sum() < 15:
            continue
        q = phase[selected]
        subset = data[selected]
        if q.min() > -.05 or q.max() < duration - .035:
            continue
        if not continuous_signal_samples(subset, policy["maximum_sample_gap_s"]):
            continue
        down = (q >= 0) & (q <= ramp1)
        up = (q >= ramp1 + dwell) & (q <= duration)
        if min(down.sum(), up.sum()) < policy["minimum_samples_per_ramp"]:
            continue
        moving = (q >= 0) & (q <= duration - .03)
        if np.ptp(subset[moving, 5:7], axis=0).max() > policy["maximum_xy_span_m"]:
            continue
        expected = np.interp(q, [-.1, 0, ramp1, ramp1 + dwell, duration],
                             [0, 0, depth, depth, depth + rise])
        baseline = np.median(subset[:, 7] - expected)
        error = float(np.sqrt(np.mean((subset[:, 7] - baseline - expected) ** 2)))
        if error > abs(depth) * policy["maximum_fit_error_fraction"]:
            continue
        pre = subset[q < 0, 4]
        current_baseline = float(np.median(np.abs(pre)))
        peak = float(np.max(np.abs(subset[:, 4])))
        if current_baseline < policy["minimum_baseline_current_a"] or peak / current_baseline < policy["minimum_current_ratio"]:
            continue
        result = {
            "start_monotonic_ns": int(seed_ns + onset * 1e9),
            "end_monotonic_ns": int(seed_ns + (onset + duration) * 1e9),
            "start_utc_ns": int(subset[0, 3] + (onset - t[selected][0]) * 1e9),
            "signal_seq_start": int(subset[0, 0]), "signal_seq_end": int(subset[-1, 0]),
            "segment_id": int(subset[0, 1]), "sample_count": len(subset),
            "fit_rmse_m": error, "observed_z_dip_m": float(np.min(subset[:, 7]) - baseline),
            "baseline_current_a": current_baseline, "peak_current_a": peak,
            "peak_current_ratio": peak / current_baseline,
            "possible_current_clipping": _possible_clipping(subset[:, 4], peak),
        }
        if best is None or error < best["fit_rmse_m"]:
            best = result
    return best


def _fit_family(rows, seed_ns, timings, policy):
    """Fit amplitudes independently of configuration, then measure plateaus.

    Timings remain a reference model. Core fitting excludes the last acquisition
    interval, but acceptance requires an observed return plateau: a feedback
    recovery jump cannot substitute for the programmed positive ramp.
    """
    data = np.asarray(rows, dtype=float)
    if len(data) < 20:
        return None
    t = (data[:, 2] - seed_ns) / 1e9
    ramp1, dwell, ramp2 = (float(timings[k]) for k in
                         ("lift_time_1_s", "bias_settling_time_s", "lift_time_2_s"))
    duration = ramp1 + dwell + ramp2
    if not (all(math.isfinite(v) for v in (ramp1, dwell, ramp2))
            and min(ramp1, ramp2) > 0 and dwell > 0 and duration <= 3):
        return None
    best = None
    for onset in np.arange(-ramp1 - .12, .251, .005):
        q = t - onset
        core = (q >= 0) & (q <= duration - .05)
        pre = (q > -.085) & (q < -.025)
        hold = (q > ramp1 + .15 * dwell) & (q < ramp1 + .85 * dwell)
        down = core & (q < ramp1)
        up = core & (q > ramp1 + dwell)
        if min(down.sum(), up.sum()) < policy["minimum_samples_per_ramp"] or min(pre.sum(), hold.sum()) < 2:
            continue
        p = q[core]
        design = np.column_stack((np.ones(len(p)), np.clip(p / ramp1, 0, 1),
                                  np.clip((p - ramp1 - dwell) / ramp2, 0, 1)))
        # Nanometres avoid loss of conditioning in the least-squares solve.
        z = data[:, 7] * 1e9
        fit = np.linalg.lstsq(design, z[core], rcond=None)[0]
        error = float(np.sqrt(np.mean((design @ fit - z[core]) ** 2))) * 1e-9
        depth, rise = fit[1] * 1e-9, fit[2] * 1e-9
        minimum = policy["minimum_inferred_displacement_m"]
        maximum = policy["maximum_inferred_displacement_m"]
        if not (minimum <= -depth <= maximum and minimum <= rise <= maximum
                and error <= -depth * policy["maximum_fit_error_fraction"]):
            continue
        if np.ptp(data[core, 5:7], axis=0).max() > policy["maximum_xy_span_m"]:
            continue
        initial = float(np.median(z[pre]))
        bottom = float(np.median(z[hold]))
        if np.ptp(z[hold]) * 1e-9 > -depth * policy["maximum_fit_error_fraction"]:
            continue
        # Require at least three stable samples near the expected ramp endpoint.
        stable = np.abs(np.diff(z)) * 1e-9 <= max(minimum * .005, -depth * .005)
        plateaus = []
        first = None
        for i, flat in enumerate(np.r_[stable, False]):
            if flat and first is None:
                first = i
            if not flat and first is not None:
                if i - first >= 2 and duration - .15 <= q[first] <= duration + .08:
                    level = float(np.median(z[first:i + 1]))
                    if abs(level - sum(fit)) * 1e-9 <= max(-depth, rise) * .3:
                        plateaus.append((first, i, level))
                first = None
        if not plateaus:
            continue
        first, last, top = max(plateaus, key=lambda item: item[1] - item[0])
        observed_depth, observed_rise = (bottom - initial) * 1e-9, (top - bottom) * 1e-9
        if not (minimum <= -observed_depth <= maximum and minimum <= observed_rise <= maximum):
            continue
        used = (q >= -.1) & (t <= t[last])
        subset = data[used]
        if len(subset) < 20 or not continuous_signal_samples(subset, policy["maximum_sample_gap_s"]):
            continue
        baseline = float(np.median(np.abs(data[pre, 4])))
        denominator = max(baseline, policy["minimum_baseline_current_a"])
        current = np.abs(data[core, 4])
        peak = float(current.max())
        if (peak < policy["minimum_peak_current_a"] or
                np.sum(current >= denominator * policy["minimum_current_ratio"]) < 3):
            continue
        # The complete post-return window is optional; its absence is explicit.
        end_tick = int(data[first, 2])
        post = (data[:, 2] > data[last, 2]) & (data[:, 2] <= end_tick + .45e9)
        post_complete = bool(data[-1, 2] >= end_tick + .45e9)
        extra = max(0., float(z[post].max() - top) * 1e-9) if post.any() else None
        end_current = float(np.median(np.abs(data[first:last + 1, 4])))
        result = {
            "start_monotonic_ns": int(seed_ns + onset * 1e9),
            "end_monotonic_ns": end_tick,
            "start_utc_ns": int(data[0, 3]) + int(seed_ns + onset * 1e9) - int(data[0, 2]),
            "signal_seq_start": int(subset[0, 0]), "signal_seq_end": int(subset[-1, 0]),
            "segment_id": int(subset[0, 1]), "sample_count": len(subset),
            "fit_rmse_m": error, "observed_z_dip_m": observed_depth,
            "observed_z_return_m": observed_rise, "program_end_offset_m": (top - initial) * 1e-9,
            "baseline_current_a": baseline, "peak_current_a": peak,
            "peak_current_ratio": peak / denominator,
            "current_ratio_uses_floor": baseline < policy["minimum_baseline_current_a"],
            "possible_current_clipping": _possible_clipping(current, peak),
            "post_return": {"complete": post_complete, "extra_retraction_m": extra,
                            "current_at_return_a": end_current,
                            "high_current_at_return": end_current >= denominator * policy["minimum_current_ratio"],
                            "signal_seq_end": int(data[post][-1, 0]) if post.any() else int(data[last, 0]),
                            "interpretation": "Observed response, not proof of SafeTip or mechanical indentation."},
        }
        if best is None or error < best["fit_rmse_m"]:
            best = result
    return best


def recognize_tip_shaper(signals, pattern, *, parameter_snapshots=(), observation_context=None,
                         maximum_samples=5_000_000):
    policy = pattern["recognition"]
    buffer = deque(maxlen=1200)
    detections = []
    seeds = deque()
    previous = None
    last_seed = -math.inf
    examined = 0
    limited = False
    snapshots = sorted(parameter_snapshots, key=lambda value: value["monotonic_ns"])
    context = observation_context or {}
    intervals = sorted(context.get("feedback_intervals", ()), key=lambda item: item["start_monotonic_ns"])
    excluded = context.get("excluded_process_intervals", ())
    interval_index = 0

    def resolve(seed, feedback=None):
        prior = [p for p in snapshots if p["monotonic_ns"] <= seed]
        current = prior[-1] if prior else None
        # A successful snapshot is configuration evidence, never execution proof.
        candidates = []
        if current and current.get("available") and current.get("parameters"):
            candidates.append((current["parameters"], "recorded_configuration", current))
        candidates.append((pattern["parameters"], "reviewed_waveform_example", None))
        matches = []
        for params, source, evidence in candidates:
            try:
                match = _fit(list(buffer), seed, params, policy)
            except (KeyError, TypeError, ValueError):
                match = None
            if match:
                match.update(parameter_source=source, matched_parameters=dict(params),
                             parameter_snapshot=evidence,
                             parameter_snapshot_age_s=(None if evidence is None else
                                 (match["start_monotonic_ns"] - evidence["monotonic_ns"]) / 1e9),
                             parameters_are_execution_proof=False,
                             command_identity="inferred_tip_shaper_like",
                             actor="unknown", outcome="not_assessed",
                             confidence="waveform_match", pattern_id=pattern["pattern"]["id"])
                if evidence and evidence["monotonic_ns"] > match["start_monotonic_ns"]:
                    # A snapshot obtained during a procedure cannot establish
                    # the configuration that preceded it.
                    continue
                matches.append(match)
            try:
                inferred = _fit_family(list(buffer), seed, params, policy)
            except (KeyError, TypeError, ValueError):
                inferred = None
            if inferred and (evidence is None or evidence["monotonic_ns"] <= inferred["start_monotonic_ns"]):
                if match and abs(match["start_monotonic_ns"] - inferred["start_monotonic_ns"]) < .15e9:
                    # A configuration match still needs observed response fields;
                    # do not replace them with configuration values.
                    for field in ("observed_z_dip_m", "observed_z_return_m", "program_end_offset_m", "post_return"):
                        match[field] = inferred[field]
                    match["amplitude_measurement_signal_range"] = [inferred["signal_seq_start"], inferred["signal_seq_end"]]
                inferred.update(parameter_source="waveform_inferred",
                    matched_parameters={"tip_lift_m": inferred["observed_z_dip_m"],
                                        "lift_height_m": inferred["observed_z_return_m"]},
                    reference_timings={key: params[key] for key in
                        ("lift_time_1_s", "bias_settling_time_s", "lift_time_2_s")},
                    timing_source=source, parameter_snapshot=evidence,
                    parameter_snapshot_age_s=None if evidence is None else
                        (inferred["start_monotonic_ns"] - evidence["monotonic_ns"]) / 1e9,
                    parameters_are_execution_proof=False, command_identity="inferred_tip_shaper_like",
                    actor="unknown", outcome="not_assessed", confidence="waveform_match",
                    pattern_id=pattern["pattern"]["id"])
                matches.append(inferred)
        matches = [m for m in matches if not any(
            m["start_monotonic_ns"] <= (ep["end_monotonic_ns"] if ep["end_monotonic_ns"] is not None else math.inf)
            and m["end_monotonic_ns"] >= ep["start_monotonic_ns"] for ep in excluded)]
        if feedback:
            matches = [m for m in matches if
                feedback["start_monotonic_ns"] - .35e9 <= m["start_monotonic_ns"] <= feedback["start_monotonic_ns"] + .3e9
                and m["end_monotonic_ns"] <= feedback["end_monotonic_ns"] + .2e9]
        if matches:
            # Prefer a matching prior configuration; otherwise retain
            # the measured amplitudes, never populate unknown settings from a screenshot.
            match = min(matches, key=lambda item: (item["parameter_source"] != "recorded_configuration",
                item["parameter_source"] != "waveform_inferred", item["fit_rmse_m"]))
            match["feedback_interval"] = feedback
            match["confidence"] = "waveform_and_feedback_match" if feedback else "waveform_match"
            overlaps = [d for d in detections if match["start_monotonic_ns"] <= d["end_monotonic_ns"]
                        and match["end_monotonic_ns"] >= d["start_monotonic_ns"]]
            if overlaps and not (feedback and all(d.get("feedback_interval") is None for d in overlaps)):
                return
            for prior_match in overlaps:
                detections.remove(prior_match)
            match["id"] = f"tip-shaper-waveform-{match['signal_seq_start']}"
            detections.append(match)

    for row in signals:
        if examined >= maximum_samples:
            limited = True
            break
        examined += 1
        if (not all(math.isfinite(v) for v in row)
                or (previous is not None and (row[1] != previous[1] or row[2] <= previous[2]
                    or row[0] != previous[0] + 1 or row[2] - previous[2] > policy["maximum_sample_gap_s"] * 1e9))):
            # Complete events before a boundary remain valid; never use samples
            # on the far side to complete an interrupted trajectory.
            for seed in seeds:
                resolve(*seed)
            buffer.clear()
            seeds.clear()
            last_seed = -math.inf
        previous = row
        if not all(math.isfinite(v) for v in row):
            continue
        buffer.append(row)
        while interval_index < len(intervals) and intervals[interval_index]["start_monotonic_ns"] <= row[2]:
            interval = intervals[interval_index]
            interval_index += 1
            tick = interval["start_monotonic_ns"]
            duration = (interval["end_monotonic_ns"] - tick) / 1e9
            if buffer[0][2] <= tick and .3 <= duration <= 3.6:
                seeds.append((tick, interval))
        while seeds and row[2] - seeds[0][0] >= 3.6e9:
            resolve(*seeds.popleft())
        if len(buffer) >= 20 and row[2] - last_seed >= 1.2e9:
            baseline = np.median([abs(p[4]) for p in list(buffer)[-20:-1]])
            if baseline >= policy["minimum_baseline_current_a"] and abs(row[4]) >= baseline * policy["minimum_current_ratio"]:
                seeds.append((row[2], None))
                last_seed = row[2]
    # Final candidates are accepted only when _fit has the full return ramp.
    for seed in seeds:
        resolve(*seed)
    detections.sort(key=lambda item: item["start_monotonic_ns"])
    return {"analysis_version": "tip-shaper-waveform-v2", "detections": detections,
            "examined_samples": examined, "complete": not limited,
            "limits": ["Matches describe observed waveforms, not confirmed GUI commands or tip quality.",
                       "Missing high-frequency segments cannot be reconstructed.",
                       "Current triggers may miss low-current conditioning; unmatched does not mean no operation.",
                       "Only negative approach / positive return patterns up to 3 s are fitted.",
                       "Inferred amplitudes are scanner displacements; reference timings and unknown settings are not readbacks.",
                       "Post-return retraction and clipped current do not prove protection activation or successful conditioning."]}
