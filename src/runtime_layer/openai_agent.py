"""Optional Responses API loop over existing tools and RunStore, with bounded work.

The SDK is imported only by the CLI in API mode. A Responses-compatible client is
injected here, so transport and failure paths can be tested without credentials.
"""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import json
import math
import time

from data_layer.recording import RunStore
from knowledge_base.agent_prompt import SPM_AGENT_INSTRUCTIONS
from .spm_tools import SPMTools, ToolOutput
from .tool_registry import ToolRegistry


@dataclass(frozen=True)
class AgentLimits:
    max_turns: int = 8
    max_tool_calls: int = 16
    max_output_tokens: int = 4096
    max_input_bytes: int = 16 * 1024 * 1024
    timeout_s: float = 120.0

    def __post_init__(self):
        for name in ("max_turns", "max_tool_calls", "max_output_tokens", "max_input_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(self.timeout_s, bool) or not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive and finite")


def responses_tools(registry: ToolRegistry) -> list[dict]:
    # Preserve Python defaults and free-form recipe/condition dictionaries.
    # Local strict validation still applies before dispatch.
    return [{"type": "function", "name": entry["name"], "description": entry["summary"],
             "parameters": entry["parameters"], "strict": False}
            for entry in registry.definitions()]


def _tool_output(call_id: str, result: ToolOutput) -> dict:
    text = json.dumps({"data": result.data, "is_error": result.is_error},
                      ensure_ascii=False, allow_nan=False)
    output = text
    if result.images:
        output = [{"type": "input_text", "text": text}]
        output.extend({"type": "input_image", "image_url": "data:image/png;base64," +
                       base64.b64encode(payload).decode(), "detail": "auto"} for payload in result.images)
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def run_openai_agent(surface: SPMTools, *, client, model: str, prompt: str,
                     limits: AgentLimits | None = None) -> dict:
    """Run a fresh session. No resume, implicit model choice, code execution or live switch."""
    if not isinstance(model, str) or not model.strip() or not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("model and prompt must be nonempty strings")
    limits = limits or AgentLimits()
    # Disable even the SDK's default transport retries. A failed session is reviewed,
    # never silently replayed, and already-committed tool evidence remains available.
    client = client.with_options(max_retries=0, timeout=limits.timeout_s)
    registry = ToolRegistry(surface)
    definitions = responses_tools(registry)
    instructions = SPM_AGENT_INSTRUCTIONS + "\nConfigured paths (JSON):\n" + json.dumps({
        "read_roots": [str(path) for path in surface.read_roots],
        "output_root": str(surface.output_root)}, ensure_ascii=False)
    history = [{"role": "user", "content": prompt}]
    seen_calls = set()
    turns = calls = tool_errors = 0
    status, reason, report = "failed", "runtime_error", ""
    store = RunStore.create(surface._output_directory("agent_runs"),
                            signal_catalog={}, requested_period_ns=0)

    def event(kind, payload):
        store.append_event(kind, payload, monotonic_ns=time.perf_counter_ns(), utc_ns=time.time_ns())

    def outcome():
        return {"status": status, "reason": reason, "report": report,
                "report_scope": "model_interpretation_not_experimental_validation",
                "model": model, "turns": turns, "tool_calls": calls, "tool_errors": tool_errors,
                "run_id": store.run_id, "run_directory": str(store.run_dir.resolve()),
                "database_path": str(store.database_path.resolve()), "authorizes_live_action": False}

    try:
        event("agent_started", {"model": model, "prompt": prompt, "instructions": instructions,
                                "tools": definitions, "limits": asdict(limits)})
        for turn in range(1, limits.max_turns + 1):
            request = {"model": model, "instructions": instructions, "input": history,
                       "tools": definitions, "parallel_tool_calls": False, "store": False,
                       "include": ["reasoning.encrypted_content"],
                       "max_output_tokens": limits.max_output_tokens}
            input_bytes = len(json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            if input_bytes > limits.max_input_bytes:
                status, reason = "stopped", "max_input_bytes"
                break
            turns = turn
            event("agent_request_started", {"turn": turn, "input_bytes": input_bytes})
            response = client.responses.create(**request)
            output = [item.model_dump(mode="json", exclude_none=True) for item in response.output]
            event("agent_response", {"turn": turn, "response_id": response.id,
                  "model": response.model, "status": response.status, "output": output,
                  "usage": response.usage.model_dump(mode="json") if response.usage else None})
            if response.status != "completed":
                status, reason = "failed", "response_" + str(response.status)
                break
            # Preserve ALL output items, including reasoning and assistant messages.
            history.extend(output)
            requested = [item for item in output if item["type"] == "function_call"]
            if not requested:
                refusals = [part["refusal"] for item in output if item["type"] == "message"
                            for part in item.get("content", []) if part["type"] == "refusal"]
                report = "\n".join(refusals) if refusals else response.output_text
                if refusals:
                    status, reason = "stopped", "model_refusal"
                elif report.strip():
                    status, reason = "complete", "final_response"
                else:
                    status, reason = "failed", "empty_response"
                break
            # Validate a whole batch's identities/budget before executing any item.
            ids = [item.get("call_id") for item in requested]
            if (any(not isinstance(call_id, str) or not call_id for call_id in ids)
                    or len(set(ids)) != len(ids) or seen_calls.intersection(ids)):
                status, reason = "failed", "invalid_or_repeated_call_id"
                break
            if calls + len(requested) > limits.max_tool_calls or turn == limits.max_turns:
                status = "stopped"
                reason = "max_tool_calls" if calls + len(requested) > limits.max_tool_calls else "max_turns"
                break
            for item in requested:
                seen_calls.add(item["call_id"])
                calls += 1
                event("agent_tool_started", {"turn": turn, "call_id": item["call_id"],
                      "name": item["name"], "arguments": item["arguments"]})
                result = registry.invoke(item["name"], item["arguments"])
                tool_errors += int(result.is_error)
                image_paths = surface.save_images(result)
                event("agent_tool_finished", {"call_id": item["call_id"], "name": item["name"],
                      "data": result.data, "is_error": result.is_error, "image_paths": image_paths})
                history.append(_tool_output(item["call_id"], result))
        event("agent_finished", outcome())
        store.finalize(status, stopped_utc_ns=time.time_ns())
        return outcome()
    except BaseException as error:
        # Provider exception strings may contain request bodies/credentials. Keep
        # only the type; full tool errors are already persisted as tool evidence.
        status = "stopped" if isinstance(error, KeyboardInterrupt) else "failed"
        reason = "interrupted" if status == "stopped" else type(error).__name__
        try:
            event("agent_failed", outcome())
            store.finalize(status, stopped_utc_ns=time.time_ns())
        except Exception as audit_error:
            error.add_note(f"failure audit also failed: {type(audit_error).__name__}")
            raise error
        if not isinstance(error, (Exception, KeyboardInterrupt)):
            raise
        return outcome()
    finally:
        store.close()
