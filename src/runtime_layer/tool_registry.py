"""One manifest-backed schema, validation and dispatch path for every transport."""
from __future__ import annotations

import inspect
import json
from typing import get_type_hints

from pydantic import ConfigDict, create_model

from knowledge_base.script_manifest import load_agent_tool_manifest
from .spm_tools import SPMTools, ToolOutput


def tool_error(error: Exception) -> ToolOutput:
    return ToolOutput({"error_type": type(error).__name__, "error": str(error),
                       "authorizes_live_action": False}, is_error=True)


class ToolRegistry:
    """Expose only manifest names, with parameters derived from typed methods.

    This validates transport arguments, not experimental safety. Workflow and
    SafetyGate checks remain authoritative for units, bounds and prerequisites.
    """

    def __init__(self, surface: SPMTools):
        self.surface = surface
        self._entries = {entry["name"]: entry for entry in load_agent_tool_manifest()}
        self._functions = {}
        self._models = {}
        for name in self._entries:
            function = getattr(surface, name)
            hints = get_type_hints(function)
            fields = {}
            for parameter in inspect.signature(function).parameters.values():
                if parameter.kind not in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY):
                    raise TypeError(f"unsupported tool signature: {name}")
                default = ... if parameter.default is parameter.empty else parameter.default
                fields[parameter.name] = (hints[parameter.name], default)
            self._functions[name] = function
            self._models[name] = create_model(
                name + "Arguments", __config__=ConfigDict(
                    extra="forbid", strict=True, allow_inf_nan=False), **fields)

    def definitions(self) -> list[dict]:
        return [entry | {"parameters": self._models[name].model_json_schema()}
                for name, entry in self._entries.items()]

    def invoke(self, name: str, arguments: dict | str) -> ToolOutput:
        """Reject malformed/unregistered calls as data; never evaluate model code."""
        try:
            if name not in self._entries:
                raise ValueError(f"unknown tool: {name}")
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
            # Also checks non-finite values inside untyped nested dicts.
            json.dumps(arguments, allow_nan=False)
            validated = self._models[name].model_validate(arguments).model_dump()
            result = self._functions[name](**validated)
            json.dumps(result.data, allow_nan=False)
            return result
        except Exception as error:
            return tool_error(error)
