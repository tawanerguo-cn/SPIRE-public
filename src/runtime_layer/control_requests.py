"""Decode local saved requests; the existing CLI owns connections and execution."""
from copy import deepcopy
import json
import math
from pathlib import Path
from workflow_layer.control_step import validate_quick_arguments


def read_control_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    def constant(value):
        raise ValueError(f"Nonfinite JSON value: {value}")
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=pairs, parse_constant=constant)


def load_control_session(path):
    path = Path(path).resolve()
    session = read_control_json(path)
    allowed = {"schema_version", "host", "port", "timeout_s", "active_run_db", "authorization",
               "stop_file", "observer_port", "output_directory", "purpose", "presets"}
    if (not isinstance(session, dict) or set(session) - allowed
            or type(session.get("schema_version")) is not int or session["schema_version"] != 1):
        raise ValueError("Control session needs schema_version=1 and only documented fields")
    if "host" in session and (not isinstance(session["host"], str) or not session["host"].strip()):
        raise ValueError("host must be nonempty text")
    for name in ("port", "observer_port"):
        if name in session and (type(session[name]) is not int or not 1 <= session[name] <= 65535):
            raise ValueError(f"{name} must be an integer TCP port")
    if "timeout_s" in session and (type(session["timeout_s"]) not in (int, float)
            or not math.isfinite(session["timeout_s"]) or session["timeout_s"] <= 0):
        raise ValueError("timeout_s must be a positive finite number")
    if "purpose" in session and (not isinstance(session["purpose"], str) or not session["purpose"].strip()):
        raise ValueError("purpose must be nonempty text")
    for name in ("active_run_db", "authorization", "stop_file", "output_directory"):
        if not isinstance(session.get(name), str) or not session[name].strip():
            raise ValueError(f"Control session requires {name}")
        session[name] = str((path.parent / session[name]).resolve())
    if not isinstance(session.get("presets"), dict):
        raise ValueError("Control session requires a presets object")
    for name, preset in session["presets"].items():
        if not isinstance(preset, dict) or set(preset) - {"action", "arguments", "purpose"}:
            raise ValueError(f"Invalid preset: {name}")
        if not isinstance(preset.get("action"), str) or not isinstance(preset.get("arguments"), dict):
            raise ValueError(f"Preset {name} requires action and arguments")
        if name in {"move", "poke", "scan"} and preset["action"] != name:
            raise ValueError(f"Preset {name} must use action {name}")
        if name == "pulse" and preset["action"] not in {"apply_bias_pulse", "pulse_and_assess"}:
            raise ValueError("Preset pulse must send one pulse")
        if set(preset["arguments"]) & {"position_nm", "center_nm"}:
            raise ValueError("Positions belong to each request, not saved presets")
    return session


def resolve_control_request(value, session=None):
    """Expand short JSON to the existing action/arguments/purpose envelope."""
    session = session or {}
    if isinstance(value, str):
        value = {"preset": value}
    elif isinstance(value, list):
        if not value or not isinstance(value[0], str):
            raise ValueError("Short request starts with a preset name")
        name, *fields = value
        sizes = {"pulse": (0,), "move": (2,), "poke": (2, 3), "scan": (3, 4)}
        if name not in sizes or len(fields) not in sizes[name]:
            raise ValueError("Use pulse; move x y; poke x y [depth]; scan x y size [frame_time_s]")
        arguments = {}
        if name in {"move", "poke"}:
            arguments["position_nm"] = fields[:2]
            if len(fields) == 3:
                arguments["depth_nm"] = fields[2]
        elif name == "scan":
            arguments = {"center_nm": fields[:2], "size_nm": fields[2]}
            if len(fields) == 4:
                arguments["frame_time_s"] = fields[3]
        value = {"preset": name, "arguments": arguments}
    if not isinstance(value, dict):
        raise ValueError("Request must be an action object, preset name, or short array")
    if "preset" in value:
        if set(value) - {"preset", "arguments", "purpose"}:
            raise ValueError("Preset request accepts only preset, arguments and purpose")
        name = value["preset"]
        if not isinstance(name, str) or name not in session.get("presets", {}):
            raise ValueError(f"Unknown saved preset: {name}")
        preset = deepcopy(session["presets"][name])
        supplied = value.get("arguments", {})
        if not isinstance(supplied, dict):
            raise ValueError("arguments must be an object")
        action = preset["action"]
        required = {"move": {"position_nm"}, "poke": {"position_nm"}, "scan": {"center_nm", "size_nm"}}.get(action, set())
        if required - supplied.keys():
            raise ValueError(f"Each {action} request must supply {sorted(required)}")
        value = {"action": action, "arguments": preset["arguments"] | supplied,
                 "purpose": value.get("purpose", preset.get("purpose", session.get("purpose")))}
    if set(value) != {"action", "arguments", "purpose"}:
        raise ValueError("A control request requires action, arguments and purpose")
    if not isinstance(value["action"], str) or not isinstance(value["arguments"], dict):
        raise ValueError("action must be text and arguments an object")
    if not isinstance(value["purpose"], str) or not value["purpose"].strip():
        raise ValueError("A nonempty purpose is required in request, preset or session")
    def finite(item):
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Request contains a nonfinite number")
        if isinstance(item, dict):
            for child in item.values():
                finite(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                finite(child)
    finite(value)
    validate_quick_arguments(value["action"], value["arguments"])
    return deepcopy(value)
