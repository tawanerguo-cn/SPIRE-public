"""Describe code-registered services and read their evidence without connecting hardware."""
from data_layer.recording import read_run_event_page
from instrument_layer.capabilities import IMPLEMENTED_NANONIS_OVERLAYS, build_nanonis_capability_registry
from instrument_layer.models import to_jsonable
from instrument_layer.safety import build_default_safety_gate
from knowledge_base.script_manifest import load_agent_tool_manifest


def describe_instrument_service() -> dict:
    gate = build_default_safety_gate()
    capabilities = build_nanonis_capability_registry(tuple(IMPLEMENTED_NANONIS_OVERLAYS), safety_gate=gate)
    return {
        "instrument_family": "Nanonis SPM", "connected_to_hardware": False,
        "scope": "Registered code capabilities; not live availability, station commissioning or session authorization.",
        "capabilities": to_jsonable(capabilities), "bounded_actions": to_jsonable(gate.list_actions()),
        "agent_tools": list(load_agent_tool_manifest()), "authorizes_live_action": False,
        "execution_mode": "offline_and_fake_only",
    }


def inspect_workflow_run(database_path, *, after_event_id=0) -> dict:
    return read_run_event_page(database_path, after_event_id=after_event_id) | {
        "status_scope": "Run recording status; workflow outcome and pending postchecks are in the events.",
        "authorizes_live_action": False,
        "recovery_policy": "Persisted evidence only; reading never resumes or retries an action.",
    }
