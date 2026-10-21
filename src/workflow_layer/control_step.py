"""One operator-authorized control step through the shared typed device only."""
from dataclasses import asdict
import math

from instrument_layer.action_events import ActionName, ActionIntent
from instrument_layer.models import FeedbackPokeRecipe, ScanFrame, to_jsonable
from instrument_layer.nanonis_device import InstrumentPreconditionError
from .tip_conditioning import pulse_and_assess
from .conditioning_burst import run_pulse_burst
from .noise_observation import observe_noise
from .guarded_spectroscopy import acquire_guarded_spectrum
from .lock_in_observation import inspect_lock_in_configuration


def validate_quick_arguments(action, arguments):
    """Small JSON surface; positions are always explicit physical XY in nm."""
    specifications = {
        "move": ({"position_nm"}, set()),
        "poke": ({"position_nm", "depth_nm"}, set()),
        "scan": ({"center_nm", "size_nm", "frame_time_s"}, {"angle_deg", "direction_up"}),
    }
    if action not in specifications:
        return
    required, optional = specifications[action]
    if not isinstance(arguments, dict) or required - arguments.keys() or arguments.keys() - required - optional:
        raise ValueError(f"{action} requires {sorted(required)}; optional {sorted(optional)}")
    def number(value, name, positive=False):
        if type(value) not in (int, float) or not math.isfinite(value) or (positive and value <= 0):
            raise ValueError(f"{name} must be a {'positive ' if positive else ''}finite number")
    point_name = "center_nm" if action == "scan" else "position_nm"
    point = arguments[point_name]
    if not isinstance(point, (list, tuple)) or len(point) != 2:
        raise ValueError(f"{point_name} must be [x_nm, y_nm]")
    for value in point:
        number(value, point_name)
    for name in ("depth_nm", "size_nm", "frame_time_s", "angle_deg"):
        if name in arguments:
            number(arguments[name], name, name != "angle_deg")
    if "direction_up" in arguments and type(arguments["direction_up"]) is not bool:
        raise ValueError("direction_up must be boolean")


def _quick_step(device, action, arguments):
    """Compose existing typed operations; no backend access, retry or new bounds."""
    validate_quick_arguments(action, arguments)
    def check_control():
        if device.control_stop_requested():
            raise InstrumentPreconditionError("Control step cancelled before execution")
    check_control()
    if device.get_auto_approach_running() or device.get_bias_spectroscopy_status():
        raise ValueError("Finish approach/spectroscopy before a quick move, poke or scan")
    values = arguments
    completed = []
    def preflight(action, parameters):
        device.validate_control_action(action, parameters,
            require_live_enabled=not device.is_fake_backend).raise_for_violations()
    def run(name, operation):
        check_control()
        result = operation()
        completed.append({"action": name, "result": to_jsonable(result)})
        return result
    try:
        if action == "scan":
            buffer = device.get_scan_buffer()
            if buffer.pixels != buffer.lines or buffer.lines < 1:
                raise ValueError("Square scan requires equal existing X/Y pixel counts; buffer is not changed by this shortcut")
            center = [v * 1e-9 for v in values["center_nm"]]
            size = values["size_nm"] * 1e-9
            frame = ScanFrame(center_x_m=center[0], center_y_m=center[1], width_m=size,
                              height_m=size, angle_deg=values.get("angle_deg", 0.))
            line_time = values["frame_time_s"] / (2 * buffer.lines)
            preflight("scan_frame_set", asdict(frame))
            device.safety_gate.require_scan_frame_envelope(frame, device.get_piezo_range())
            current_frame = device.get_scan_frame()
            for width in (current_frame.width_m, size):
                preflight("scan_speed_set", dict(forward_line_time_s=line_time,
                    backward_line_time_s=line_time, forward_m_per_s=width/line_time,
                    backward_m_per_s=width/line_time))
            preflight("scan_start_current_frame", dict(
                direction_up=int(values.get("direction_up", True)), timeout_ms=10000))
            if device.get_scan_status().running:
                run("stop_scan", device.stop_scan)
            run("set_scan_line_times", lambda: device.set_scan_line_times(
                forward_line_time_s=line_time, backward_line_time_s=line_time))
            run("set_scan_frame", lambda: device.set_scan_frame(**asdict(frame)))
            result = run("start_scan", lambda: device.start_scan(direction_up=values.get("direction_up", True)))
            return {"steps": completed, "scan": to_jsonable(result), "frame_time_s": values["frame_time_s"],
                    "pixels": buffer.pixels, "lines": buffer.lines, "line_time_s": line_time}
        position = values["position_nm"]
        device.validate_tip_xy(position[0]*1e-9, position[1]*1e-9,
            require_live_enabled=not device.is_fake_backend).raise_for_violations()
        if action == "poke":
            recipe = FeedbackPokeRecipe(approach_depth_m=values["depth_nm"] * 1e-9)
            shaper = recipe.to_tip_shaper_recipe(current_bias_v=device.get_bias_v())
            parameters = asdict(shaper) | {"change_bias": int(shaper.change_bias),
                "restore_feedback": int(shaper.restore_feedback), "estimated_duration_s": shaper.estimated_duration_s}
            preflight("tip_shaper", parameters)
        if device.get_scan_status().running:
            run("stop_scan", device.stop_scan)
        run("move_tip_xy", lambda: device.move_tip_xy(position[0]*1e-9, position[1]*1e-9))
        if action == "poke":
            run("run_feedback_poke", lambda: device.run_feedback_poke(recipe))
        return {"steps": completed, "position_nm": list(position)}
    except BaseException as error:
        error.acquired_evidence = (getattr(error, "acquired_evidence", None) or {}) | {
            "quick_step": {"action": action, "completed_steps": completed}}
        raise


def execute_control_step(device, request, *, observer=None):
    """No retries or escalation. Typed methods own all command bounds and audit.

    The enclosing snapshot binds authorization and purpose to the nested action
    events in the same existing recorder database, including failed operations.
    """
    if set(request) != {"action", "arguments", "purpose"} or not isinstance(request["purpose"], str):
        raise ValueError("A control step needs action, arguments and purpose")
    validate_quick_arguments(request["action"], request["arguments"])
    methods = {
        **{name: (lambda name=name, **kw: _quick_step(device, name, kw)) for name in ("move", "poke", "scan")},
        "status": lambda: snapshot(),
        "observe_noise": lambda **kw: observe_noise(device, stop_requested=device.control_stop_requested, **kw),
        "start_scan": device.start_scan,
        "pause_scan": device.pause_scan,
        "resume_scan": device.resume_scan,
        "set_scan_frame": device.set_scan_frame,
        "set_scan_line_times": device.set_scan_line_times,
        "get_lock_in_modulator": device.get_lock_in_modulator,
        "get_lock_in_modulator_enabled": device.get_lock_in_modulator_enabled,
        "get_lock_in_demodulator": device.get_lock_in_demodulator,
        "get_lock_in_phase_frequency_hz": device.get_lock_in_phase_frequency_hz,
        "inspect_lock_in_configuration": lambda **kw: inspect_lock_in_configuration(device, **kw),
        "get_drift_compensation": device.get_drift_compensation,
        "set_drift_compensation": device.set_drift_compensation,
        "stop_drift_compensation": device.stop_drift_compensation,
        "get_bias_spectroscopy_advanced_settings": device.get_bias_spectroscopy_advanced_settings,
        "set_bias_spectroscopy_lock_in_run": device.set_bias_spectroscopy_lock_in_run,
        "stop_scan": device.stop_scan,
        "withdraw_tip": device.withdraw_tip,
        "stop_auto_approach": device.stop_auto_approach,
        "stop_motor": device.stop_motor,
        "set_bias_v": device.set_bias_v,
        "set_z_setpoint_a": device.set_z_setpoint_a,
        "set_z_controller_on": device.set_z_controller_on,
        "get_z_home_properties": device.get_z_home_properties,
        "home_z_controller": device.home_z_controller,
        "run_auto_approach": device.run_auto_approach,
        "apply_bias_pulse": device.apply_bias_pulse,
        "retract_coarse_z": device.retract_coarse_z,
        "move_coarse_x_plus": device.move_coarse_x_plus,
        "move_coarse_x_minus": device.move_coarse_x_minus,
        "move_coarse_y_plus": device.move_coarse_y_plus,
        "move_coarse_y_minus": device.move_coarse_y_minus,
        "move_tip_xy": device.move_tip_xy,
        "run_feedback_poke": lambda **kw: device.run_feedback_poke(FeedbackPokeRecipe(**kw)),
        "acquire_bias_spectrum": lambda **kw: acquire_guarded_spectrum(device, observer, **kw),
        "pulse_burst": lambda **kw: run_pulse_burst(device, observer,
            stop_requested=device.control_stop_requested, **kw),
        "pulse_and_assess": lambda **kw: pulse_and_assess(device, observer,
            stop_requested=device.control_stop_requested, **kw),
    }
    if request["action"] not in methods or not isinstance(request["arguments"], dict):
        raise ValueError("Unsupported control step")
    if request["action"] in {"pulse_and_assess", "pulse_burst", "acquire_bias_spectrum"} and observer is None:
        raise ValueError("This acquisition requires an independent direct observer")
    if request["action"] == "run_feedback_poke" and set(request["arguments"]) != {"approach_depth_m"}:
        raise ValueError("The station poke control exposes only approach_depth_m")
    def snapshot():
        return {"tip": to_jsonable(device.get_tip_state_snapshot()),
            "bias_v": device.get_bias_v(), "setpoint_a": device.get_z_setpoint_a(),
            "scan": to_jsonable(device.get_scan_status()),
            "auto_approach_running": device.get_auto_approach_running(),
            "bias_spectroscopy_running": device.get_bias_spectroscopy_status(),
            "scan_frame": to_jsonable(device.get_scan_frame()),
            "scan_buffer": to_jsonable(device.get_scan_buffer()),
            "scan_speed": to_jsonable(device.get_scan_speed()),
            "xy": to_jsonable(device.get_follow_me_position())}
    authorization = getattr(device.safety_gate, "authorization_evidence", None)
    protective = request["action"] in {"stop_scan", "withdraw_tip", "stop_auto_approach", "stop_motor", "stop_drift_compensation"}
    def capture():
        try:
            return snapshot()
        except Exception as error:
            if not protective:
                raise
            return {"unavailable": str(error), "error_type": type(error).__name__}
    def perform():
        before = capture()
        result = methods[request["action"]](**request["arguments"])
        # The noise observer already captures its final state and errors. Another
        # failed read here must not discard its retained waveform.
        try:
            if request['action'] == 'observe_noise':
                after = result.get('after', {'unavailable': result.get('final_state_error')})
            elif request['action'] == 'inspect_lock_in_configuration':
                # Do not resume queries after a transport error, or discard a
                # partial report because an unrelated final state query fails.
                after = {"not_read": "diagnostic evidence retained; independent state read required"}
            else:
                after = capture()
        except BaseException as error:
            error.acquired_evidence = (getattr(error, 'acquired_evidence', None) or {}) | {
                'control_step': {'before': before, 'result': to_jsonable(result),
                                 'final_state_error': str(error)}}
            raise
        return {"before": before, "result": to_jsonable(result), "after": after,
                "tip_quality": "awaiting_independent_postcheck"}
    return device.action_events.execute(ActionName.INSTRUMENT_STATE_SNAPSHOT,
        arguments={"control_step": request, "authorization": authorization},
        operation=perform, source="workflow_layer.control_step.execute_control_step",
        intent=ActionIntent.PROTECTIVE if protective else ActionIntent.EXECUTE,
        fail_open_on_sink_error=protective)
