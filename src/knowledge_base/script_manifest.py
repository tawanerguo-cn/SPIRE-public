"""Read the one authoritative agent-tool manifest; never infer hardware access."""
from pathlib import Path
import tomllib


def load_agent_tool_manifest() -> tuple[dict, ...]:
    path = Path(__file__).parent / "script_manifests" / "spm_agent_tools.toml"
    manifest = tomllib.loads(path.read_text(encoding="utf-8"))
    tools = tuple(manifest["tools"])
    if manifest["safety"]["instrument_access"] != "offline_and_fake_only":
        raise ValueError("unsupported instrument access in agent tool manifest")
    if len({entry["name"] for entry in tools}) != len(tools):
        raise ValueError("duplicate agent tool names")
    return tools
