"""Finite, direct STM feedback observation; independent of recorder scheduling.

Feedback must be ON (stationary or scanning). After conditioning, explicitly
select settling and sustained-overcurrent times. Ordinary feedback observation
excludes unrelated HOLD, approach and spectroscopy operations.
The optional pulse callback retains one finite sampler across before/action/after;
expected current during that typed action is not a post-action current fault.
It is a software protective check, not a deterministic hardware interlock.
"""
import math
import time
from threading import Event, Thread

from analysis_layer.signal_windows import analyze_signal_window
from instrument_layer.models import ZControllerStatus, to_jsonable
from instrument_layer.signal_chart import sample_signal_chart


def validate_feedback_observation_policy(device, policy, *, observation_mode="feedback"):
    if observation_mode not in {"feedback", "spectroscopy"}:
        raise ValueError("Unknown direct observation mode")
    policy = dict(settling_s=0., overcurrent_hold_s=0., withdraw_on_overcurrent=1) | policy
    values = device.safety_gate.require(observation_mode + "_observation", policy)
    if values["interval_s"] >= values["maximum_sample_gap_s"]:
        raise ValueError("Observation interval must be smaller than maximum sample gap")
    if values["settling_s"] + values["overcurrent_hold_s"] + values["interval_s"] >= values["duration_s"]:
        raise ValueError("Observation must leave time after settling to assess sustained current")
    return values


def observe_feedback(device, *, duration_s, interval_s, maximum_sample_gap_s,
                     maximum_abs_current_a, z_margin_m, stop_requested=lambda: False,
                     progress=None, stop_scan_at_end=False,
                     settling_s=0., overcurrent_hold_s=0., withdraw_on_overcurrent=1,
                     observation_mode="feedback", finished=lambda: False,
                     conditioning_action=None, baseline_s=0.,
                     clock=time.perf_counter, utc_ns=time.time_ns, wait=time.sleep):
    """Read bounded in-memory IXYZ through the existing signal implementation.

    No recording database, service state, ingestion thread or permanent stream
    is read or created. Caller can save this one observation as an artifact.
    Stops and withdrawal are distinct: only measured current/Z violations
    request withdrawal. Cancellation hands control back without cleanup writes.
    """
    policy = validate_feedback_observation_policy(device, dict(
        duration_s=duration_s, interval_s=interval_s,
        maximum_sample_gap_s=maximum_sample_gap_s,
        maximum_abs_current_a=maximum_abs_current_a, z_margin_m=z_margin_m,
        settling_s=settling_s, overcurrent_hold_s=overcurrent_hold_s,
        withdraw_on_overcurrent=withdraw_on_overcurrent), observation_mode=observation_mode)
    if conditioning_action is not None and (observation_mode != "feedback"
            or not math.isfinite(baseline_s) or baseline_s < 3 * interval_s):
        raise ValueError("Pulse baseline needs at least three sample intervals in feedback mode")
    if conditioning_action is not None:
        device.safety_gate.require("feedback_observation", policy | {"duration_s": baseline_s + duration_s})
    rows, state_checks, errors = [], [], []
    read_durations = []
    missed_slots = 0
    overcurrent_since = None
    current_not_recovered = False
    withdraw_required = False
    current_check = {"settling_samples": 0, "overcurrent_samples": 0,
                     "maximum_abs_current_a": 0., "longest_overcurrent_s": 0.}
    start = clock()
    report = {"source": "direct_nanonis_signals", "purpose": "finite_control_observation",
              "policy": policy, "started_utc_ns": utc_ns(), "status": "initializing",
              "rows": rows, "state_checks": state_checks, "protective_errors": errors,
              "tip_quality": "not_assessed", "hardware_interlock": False}
    report["current_check"] = current_check
    report["observation_mode"] = observation_mode
    report["withdraw_reason"] = None
    report["reporting_errors"] = []
    worker = None
    action_done = Event()
    action_error = []
    post_start = None
    phases = []
    if conditioning_action is not None:
        report["row_phases"] = phases
        report["pulse_window"] = {"status": "not_sent"}

    def run_action():
        report["pulse_window"].update(status="running", start_monotonic_ns=int(clock()*1e9))
        try:
            report["pulse_result"] = conditioning_action()
            report["pulse_window"]["status"] = "completed"
        except BaseException as error:
            action_error.append(error)
            report["pulse_window"].update(status="failed", error=str(error))
        finally:
            report["pulse_window"]["end_monotonic_ns"] = int(clock()*1e9)
            action_done.set()

    def phase():
        if conditioning_action is None:
            return "after"
        return "before" if worker is None else "after" if action_done.is_set() else "during"

    def cancelled():
        return stop_requested() or device.control_stop_requested()

    def emit_progress(value):
        if progress is not None and not report["reporting_errors"]:
            try:
                progress(value)
            except Exception as error:
                # A failed progress file must neither interrupt protection nor
                # turn into an instrument command. Preserve it in the result.
                report["reporting_errors"].append(str(error))

    def protect():
        # Stop scan before withdrawing; failed stop/audit does not suppress a
        # measured current/Z response. A read or software error alone cannot add it.
        actions = (("stop_bias_spectroscopy",) + (("withdraw_tip",) if withdraw_required else ()) if observation_mode == "spectroscopy"
                   else ("stop_scan", "stop_auto_approach", "stop_bias_spectroscopy") + (("withdraw_tip",) if withdraw_required else ()))
        for name in actions:
            if cancelled():
                break
            try:
                getattr(device, name)()
            except BaseException as error:
                errors.append({"action": name, "error": str(error), "type": type(error).__name__})

    def state():
        result = {"utc_ns": utc_ns(), "feedback_on": device.get_z_controller_on(),
                  "feedback_status": device.get_z_controller_status().value,
                  "approach_running": device.get_auto_approach_running(),
                  "spectroscopy_running": device.get_bias_spectroscopy_status(),
                  "bias_v": device.get_bias_v(), "setpoint_a": device.get_z_setpoint_a()}
        state_checks.append(result)
        if observation_mode == "spectroscopy":
            # Feedback HOLD and the changing bias are intentional in this mode.
            # Current and Z are still checked from direct samples throughout.
            if result["approach_running"] or device.get_scan_status().running:
                raise RuntimeError("Spectroscopy observation excludes approach and scanning")
        elif (not result["feedback_on"] or result["feedback_status"] != ZControllerStatus.ON.value
              or result["approach_running"] or result["spectroscopy_running"]):
            raise RuntimeError("Observation requires engaged feedback without approach or spectroscopy")
        if not all(math.isfinite(result[k]) for k in ("bias_v", "setpoint_a")):
            raise RuntimeError("Nonfinite control state")
        fixed = ("setpoint_a",) if observation_mode == "spectroscopy" else ("bias_v", "setpoint_a")
        if len(state_checks) > 1 and any(result[k] != state_checks[0][k] for k in fixed):
            raise RuntimeError("Bias or setpoint changed during settled feedback observation")
        return result

    try:
        if cancelled():
            raise RuntimeError("Control observation cancelled")
        references = device.resolve_signals(["Current (A)", "X (m)", "Y (m)", "Z (m)"])
        if len(references) != 4 or any(r.error is not None or r.index is None for r in references):
            raise RuntimeError("Required IXYZ signals are unavailable")
        report["signal_references"] = to_jsonable(references)
        limits = device.get_z_limits()
        low, high = limits.low_limit_m, limits.high_limit_m
        if not all(math.isfinite(v) for v in (low, high)) or low + z_margin_m >= high - z_margin_m:
            raise RuntimeError("Invalid scanner Z limits or observation margin")
        report["z_limits"] = to_jsonable(limits)
        report["z_limits_enabled"] = device.get_z_limits_enabled()
        state()
        start = clock()
        previous = start
        next_progress = start
        next_state = start + 1
        next_sample = start
        post_start = start if conditioning_action is None else None
        report["status"] = "observing"
        while post_start is None or clock() < post_start + duration_s:
            if cancelled():
                raise RuntimeError("Control observation cancelled")
            read_started = clock()
            sample_phase = phase()
            sample = sample_signal_chart(device, references, clock=clock)
            now = clock()
            if sample_phase != phase():
                sample_phase = "transition"
            if conditioning_action is not None and action_done.is_set() and post_start is None:
                post_start = report["pulse_window"]["end_monotonic_ns"] / 1e9
                report["post_started_monotonic_ns"] = int(post_start * 1e9)
                next_state = now
            if cancelled():
                raise RuntimeError("Control observation cancelled")
            read_durations.append(now - read_started)
            if now - previous > maximum_sample_gap_s:
                raise RuntimeError("Direct observation sample gap exceeded policy")
            if len(sample.values) != 4 or any(v.error is not None or v.value is None or not math.isfinite(v.value) for v in sample.values):
                raise RuntimeError("Invalid direct signal sample")
            values = [v.value for v in sample.values]
            rows.append([len(rows), 0, int(sample.timestamp_s * 1e9), utc_ns(), *values])
            phases.append(sample_phase)
            if not low + z_margin_m < values[3] < high - z_margin_m:
                withdraw_required = True
                report["withdraw_reason"] = "z_margin"
                raise RuntimeError("Z reached observation margin")
            current_check["maximum_abs_current_a"] = max(current_check["maximum_abs_current_a"], abs(values[0]))
            # This is the existing POST-action current policy. Baseline is
            # comparison data; pulse current and HOLD are expected responses.
            # Typed prechecks, SafetyGate and post-action protection still run.
            if sample_phase in {"before", "during", "transition"}:
                overcurrent_since = None
            elif sample_phase == "after" and now - post_start < settling_s:
                current_check["settling_samples"] += 1
            elif abs(values[0]) > maximum_abs_current_a:
                current_check["overcurrent_samples"] += 1
                if overcurrent_since is None:
                    overcurrent_since = now
                held = now - overcurrent_since
                current_check["longest_overcurrent_s"] = max(current_check["longest_overcurrent_s"], held)
                if held >= overcurrent_hold_s:
                    if policy["withdraw_on_overcurrent"]:
                        withdraw_required = True
                        report["withdraw_reason"] = "sustained_overcurrent"
                        raise RuntimeError("Current exceeded observation ceiling for the configured hold time after settling")
                    current_not_recovered = True
                    break
            else:
                overcurrent_since = None
            if now >= next_state and sample_phase not in {"during", "transition"}:
                state()
                next_state = clock() + 1
            if progress is not None and (len(rows) == 1 or now >= next_progress):
                emit_progress({"status": "observing", "utc_ns": utc_ns(), "sample_count": len(rows),
                          "elapsed_s": now - start, "current_a": values[0], "z_m": values[3],
                          "phase": sample_phase if conditioning_action is not None else
                                   "settling" if now - start < settling_s else "assessment"})
                next_progress = now + 1
            previous = now
            if conditioning_action is not None and worker is None and now - start >= baseline_s:
                state()
                if cancelled():
                    raise RuntimeError("Control observation cancelled before pulse")
                worker = Thread(target=run_action, name="finite-conditioning-action")
                worker.start()
            if observation_mode == "spectroscopy" and finished() and overcurrent_since is None:
                break
            next_sample += interval_s
            read_finished = clock()
            if read_finished > next_sample:
                skipped = math.floor((read_finished - next_sample) / interval_s) + 1
                missed_slots += skipped
                next_sample += skipped * interval_s
            # Absolute deadlines avoid adding every TCP round trip to the period;
            # missed deadlines skip slots instead of bursting commands to catch up.
            deadline = post_start + duration_s if post_start is not None else next_sample
            wait(max(0., min(deadline, next_sample) - read_finished))
        if cancelled():
            raise RuntimeError("Control observation cancelled")
        if observation_mode == "spectroscopy" and not finished():
            raise RuntimeError("Spectroscopy observation watchdog expired before acquisition finished")
        state()
        if clock() - previous > maximum_sample_gap_s:
            raise RuntimeError("Final state confirmation exceeded observation gap policy")
        if cancelled():
            raise RuntimeError("Control observation cancelled")
        if stop_scan_at_end or current_not_recovered:
            device.stop_scan()
        report["status"] = ("not_recovered" if current_not_recovered else
                            "completed" if overcurrent_since is None else "unsettled")
        if current_not_recovered:
            report["error"] = "Sustained current after settling: scan stopped, feedback preserved for the next conditioning decision"
        elif overcurrent_since is not None:
            report["error"] = "Observation ended during an unresolved current excursion; no stability acceptance"
    except BaseException as error:
        report.update(status="cancelled" if cancelled() else "aborted", error=str(error), error_type=type(error).__name__)
        if report["status"] != "cancelled":
            protect()
    if worker is not None:
        worker.join()
    report["ended_utc_ns"] = utc_ns()
    report["timing"] = {"requested_interval_s": interval_s, "missed_slots": missed_slots,
                        "signal_reads": len(read_durations),
                        "maximum_read_s": max(read_durations, default=0.),
                        "mean_read_s": sum(read_durations) / len(read_durations) if read_durations else None}
    if rows and observation_mode == "feedback":
        selected = feedback_assessment_rows(report, settling_s)
        report["analysis"] = analyze_signal_window(selected,
            minimum_duration_s=max(interval_s, duration_s - settling_s - 2 * interval_s),
            maximum_sample_gap_s=maximum_sample_gap_s,
            expected_current_a=state_checks[0]["setpoint_a"] if state_checks and state_checks[0]["setpoint_a"] else None,
            z_limits_m=(report["z_limits"]["low_limit_m"], report["z_limits"]["high_limit_m"]))
    if progress is not None:
        emit_progress({key: report[key] for key in ("status", "ended_utc_ns", "protective_errors")}
                 | {"error": report.get("error"), "sample_count": len(rows)})
    if action_error:
        error = action_error[0]
        error.acquired_evidence = (getattr(error, "acquired_evidence", None) or {}) | {
            "post_failure_observation": report}
        raise error
    return report


def feedback_assessment_rows(report, settling_s):
    rows = report["rows"]
    if not rows:
        return []
    start = report.get("post_started_monotonic_ns", rows[0][2])
    phases = report.get("row_phases", ["after"] * len(rows))
    return [row for row, phase in zip(rows, phases)
            if phase == "after" and row[2] - start >= settling_s * 1e9]
