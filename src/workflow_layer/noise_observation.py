"""Finite noise comparison at stationary XY through existing typed commands.

Closed reads only. Hold restores feedback after a bounded observation; withdrawn
ends retracted. This is a diagnostic experiment, never a tip-repair classifier.
"""
import math
import time
from instrument_layer.models import to_jsonable
from instrument_layer.signal_chart import sample_signal_chart
from analysis_layer.signal_windows import analyze_signal_window
from workflow_layer.feedback_observation import validate_feedback_observation_policy


def observe_noise(device, *, mode, duration_s, interval_s, maximum_sample_gap_s,
                  maximum_abs_current_a, z_margin_m, spectrum_instance=1,
                  spectrum_interval_s=1.1, stop_requested=lambda: False,
                  clock=time.perf_counter, utc_ns=time.time_ns, wait=time.sleep):
    if mode not in {"closed", "hold", "withdrawn"}:
        raise ValueError("mode must be closed, hold or withdrawn")
    validate_feedback_observation_policy(device, dict(duration_s=duration_s,
        interval_s=interval_s, maximum_sample_gap_s=maximum_sample_gap_s,
        maximum_abs_current_a=maximum_abs_current_a, z_margin_m=z_margin_m))
    if not math.isfinite(spectrum_interval_s) or spectrum_interval_s <= 0:
        raise ValueError("spectrum interval must be positive")
    if spectrum_instance not in (1, 2):
        raise ValueError("spectrum instance must be 1 or 2")
    before = dict(tip=to_jsonable(device.get_tip_state_snapshot()), bias_v=device.get_bias_v(),
        setpoint_a=device.get_z_setpoint_a(), xy=to_jsonable(device.get_follow_me_position()),
        utc_ns=utc_ns())
    try:
        before['lock_in'] = to_jsonable(device.get_lock_in_modulator())
    except Exception as error:
        before['lock_in_read_error'] = str(error)
    if device.get_scan_status().running or device.get_bias_spectroscopy_status() or device.get_auto_approach_running():
        raise RuntimeError("Noise comparison requires no raster, STS or approach")
    if not before['tip']['z_controller_on']:
        raise RuntimeError("Start contrast from engaged feedback")
    references = device.resolve_signals(["Current (A)", "X (m)", "Y (m)", "Z (m)"])
    if len(references) != 4 or any(v.error is not None or v.index is None for v in references):
        raise RuntimeError("IXYZ channels unavailable")
    settings = device.get_spectrum_analyzer_settings(spectrum_instance)
    report = dict(mode=mode, before=before, spectrum_settings=to_jsonable(settings),
        spectrum_signal=to_jsonable(device.resolve_signals([settings.channel_index])),
        rows=[], spectra=[], status="observing", protective_errors=[], authorizes_next_action=False)
    limit = before['tip']['z_limits']
    started = clock()
    hold_started = False

    def cancelled():
        return stop_requested() or device.control_stop_requested()

    def context():
        return dict(tip=to_jsonable(device.get_tip_state_snapshot()),
            bias_v=device.get_bias_v(), setpoint_a=device.get_z_setpoint_a(),
            xy=to_jsonable(device.get_follow_me_position()), utc_ns=utc_ns(),
            scan_running=device.get_scan_status().running,
            spectroscopy_running=device.get_bias_spectroscopy_status(),
            approach_running=device.get_auto_approach_running(),
            spectrum_settings=to_jsonable(device.get_spectrum_analyzer_settings(spectrum_instance)))

    def verify(value, *, feedback_on):
        if (value['tip']['z_controller_on'] != feedback_on or
                value['xy'] != before['xy'] or value['bias_v'] != before['bias_v'] or
                value['setpoint_a'] != before['setpoint_a'] or
                value['scan_running'] or value['spectroscopy_running'] or value['approach_running'] or
                value['spectrum_settings'] != report['spectrum_settings']):
            raise RuntimeError('Observation state or spectrum configuration changed; exclude this contrast from attribution')

    def withdraw():
        if cancelled():
            return
        try:
            device.withdraw_tip()
        except BaseException as error:
            report['protective_errors'].append(dict(action='withdraw_tip', error=f'{type(error).__name__}: {error}'))

    try:
        if cancelled():
            raise RuntimeError("Cancelled before contrast")
        if mode == 'hold':
            device.set_z_controller_on(False)
            hold_started = True
        elif mode == 'withdrawn':
            device.withdraw_tip(wait=True, timeout_ms=5000)
        report['measurement_start_utc_ns'] = utc_ns()
        started = clock(); previous = started; deadline = started; next_fft = started
        while clock() - started < duration_s:
            if cancelled():
                raise RuntimeError("Noise observation cancelled")
            sample = sample_signal_chart(device, references, clock=clock)
            now = clock()
            if now - previous > maximum_sample_gap_s:
                raise RuntimeError("Sample gap exceeds observation policy")
            if len(sample.values) != 4 or any(v.error or v.value is None or not math.isfinite(v.value) for v in sample.values):
                raise RuntimeError("Invalid direct sample")
            values = [v.value for v in sample.values]
            report['rows'].append([len(report['rows']),0,int(sample.timestamp_s*1e9),utc_ns(),*values])
            if values[1:3] != report['rows'][0][5:7]:
                raise RuntimeError('XY changed during noise observation')
            if mode != 'withdrawn' and not limit['low_limit_m'] + z_margin_m < values[3] < limit['high_limit_m'] - z_margin_m:
                raise RuntimeError("Z outside observation margin")
            if abs(values[0]) > maximum_abs_current_a:
                raise RuntimeError("Diagnostic current ceiling exceeded")
            if now >= next_fft:
                verify(context(), feedback_on=mode == 'closed')
                report['spectra'].append(dict(read_utc_ns=utc_ns(),data=to_jsonable(device.get_spectrum_analyzer_data(spectrum_instance))))
                next_fft = clock() + spectrum_interval_s
            previous = now
            deadline = max(deadline + interval_s, clock())
            wait(max(0, min(started+duration_s, deadline)-clock()))
        # Check the measured state BEFORE restoring HOLD, so manual withdrawal,
        # scan start or changed settings cannot be hidden by our own restoration.
        if cancelled():
            raise RuntimeError('Noise observation cancelled before restoration')
        report['measurement_end'] = context()
        verify(report['measurement_end'], feedback_on=mode == 'closed')
        if mode == 'hold' and not limit['low_limit_m'] + z_margin_m < report['measurement_end']['tip']['z_position_m'] < limit['high_limit_m'] - z_margin_m:
            raise RuntimeError('HOLD ended outside observation margin')
        if clock() - previous > maximum_sample_gap_s:
            raise RuntimeError('Final confirmation exceeds observation sample gap')
        report['status'] = 'completed'
    except BaseException as e:
        report.update(status='cancelled' if cancelled() else 'aborted',error=f"{type(e).__name__}: {e}")
        # A failure during HOLD cannot leave the uncontrolled junction engaged.
        if mode == 'hold':
            hold_started = False
            withdraw()
    finally:
        if hold_started and not cancelled():
            try:
                device.set_z_controller_on(True)
            except BaseException as error:
                report.update(status='aborted',error=f'Feedback restore failed: {error}')
                # Authorization/stop rejection can precede the typed method's
                # command/cleanup block. End HOLD even in that case.
                withdraw()
        report['elapsed_s'] = clock()-started
        try:
            report['after'] = context()
            if report['status'] == 'completed':
                verify(report['after'], feedback_on=mode != 'withdrawn')
        except BaseException as error:
            report['final_state_error'] = f'{type(error).__name__}: {error}'
            report.setdefault('error', report['final_state_error'])
            report['status'] = 'aborted'
            if mode == 'hold':
                withdraw()
    report['analysis'] = analyze_signal_window(report['rows'], minimum_duration_s=max(interval_s, duration_s-2*interval_s),
        maximum_sample_gap_s=maximum_sample_gap_s, expected_current_a=before['setpoint_a'])
    report['spectrum_freshness'] = 'No hardware timestamp. Compare changed records; first two FFT records may include prior state.'
    return report
