"""Explicit, local, expiring authorization of existing bounded actions.

This does not commission a device or add commands. A local authorization may
only narrow the registry's parameter bounds; ordinary device defaults stay off.
"""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform

from .safety import SafetyGate, SafetyViolation, build_control_safety_gate


class AuthorizedSafetyGate(SafetyGate):
    def __init__(self, actions, *, evidence, expires_at):
        super().__init__(actions)
        self.authorization_evidence = evidence
        self.expires_at = expires_at

    def validate(self, action, parameters, *, require_live_enabled=False):
        result = super().validate(action, parameters, require_live_enabled=require_live_enabled)
        if require_live_enabled and datetime.now(timezone.utc) >= self.expires_at:
            violation = SafetyViolation(action, None, "authorization_expired",
                "The local control authorization has expired")
            return replace(result, ok=False, violations=result.violations + (violation,))
        return result


def load_control_authorization(path, *, connection):
    """Load a station/connection-bound grant; refuse unknown or expanded bounds."""
    path = Path(path).resolve()
    encoded = path.read_bytes()
    value = json.loads(encoded)
    required = {"schema_version", "authorization_id", "operator_instruction", "machine",
                "host", "port", "expires_at", "actions"}
    if set(value) - {"safety_profile"} != required or value["schema_version"] != 1:
        raise ValueError("Invalid control authorization schema")
    for field in ("authorization_id", "operator_instruction", "machine", "host", "expires_at"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise ValueError(f"Authorization {field} must be nonempty text")
    if value["machine"].casefold() != platform.node().casefold():
        raise ValueError("Authorization belongs to a different computer")
    if value["host"] != connection.host or type(value["port"]) is not int or value["port"] != connection.port:
        raise ValueError("Authorization does not match the requested instrument endpoint")
    expires = datetime.fromisoformat(value["expires_at"])
    if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
        raise ValueError("Authorization is expired or lacks a timezone")
    grants = value["actions"]
    if not isinstance(grants, dict) or not grants:
        raise ValueError("Authorization must explicitly name its actions")
    defaults = {item.name: item for item in build_control_safety_gate(value.get("safety_profile")).list_actions()}
    if set(grants) - set(defaults):
        raise ValueError("Authorization names an unregistered action")
    actions = []
    for name, spec in defaults.items():
        if name not in grants:
            actions.append(replace(spec, live_enabled=False))
            continue
        restrictions = grants[name]
        params = {item.name: item for item in spec.parameters}
        if not isinstance(restrictions, dict) or set(restrictions) - set(params):
            raise ValueError(f"Unknown parameter restrictions for {name}")
        for key, bounds in restrictions.items():
            if not isinstance(bounds, list) or len(bounds) != 2:
                raise ValueError(f"{name}.{key} needs [minimum, maximum]")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in bounds):
                raise ValueError(f"{name}.{key} bounds must be finite numbers")
            low, high = bounds
            original = params[key]
            if low > high or (original.minimum is not None and low < original.minimum) or (
                    original.maximum is not None and high > original.maximum):
                raise ValueError(f"{name}.{key} may only narrow registered bounds")
            if original.integer and (int(low) != low or int(high) != high):
                raise ValueError(f"{name}.{key} requires integer bounds")
            params[key] = replace(original, minimum=low, maximum=high)
        actions.append(replace(spec, parameters=tuple(params.values()), live_enabled=True))
    evidence = value | {"source_path": str(path), "source_sha256": hashlib.sha256(encoded).hexdigest(),
                        "scope": "Operator-authorized bounded commissioning, not proof of physical acceptance"}
    return AuthorizedSafetyGate(actions, evidence=evidence, expires_at=expires)
