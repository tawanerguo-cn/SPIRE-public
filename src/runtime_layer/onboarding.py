"""Offline setup and diagnostics for a Nanonis SPiRE installation."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tomllib

from instrument_layer import FakeNanonisBackend, NanonisDevice, load_connection_config
from knowledge_base.instrument_profile import (
    DEFAULT_INSTRUMENT_PROFILE_DIRECTORY,
    DEFAULT_INSTRUMENT_PROFILE_ID,
)
from runtime_layer.recording_control import DEFAULT_RECORDING_CONFIG, load_recording_settings

_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CONNECTION_TEMPLATE = """[connection]
host = "127.0.0.1"
port = 6501
timeout_s = 3.0
backend = "fake"
"""
_RECORDING_TEMPLATE = """[recording]
enabled = false
instrument_profile = "{profile_id}"
instrument_profile_directory = "profiles"
root = "data/recordings"
storage_quota_gib = 100
storage_warning_gib = 95
gate_mode = "near_sample"
gate_poll_interval_s = 0.1
action_audit_start_mode = "tip_near_sample"
observe_external_state = true
external_state_poll_interval_s = 0.1
observe_gui_actions = false
gui_action_binding_profile = "spire_local_nanonis"
data_logger_enabled = false
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create a private Nanonis site configuration")
    init.add_argument("--directory", required=True)
    init.add_argument("--profile-id", default="my_nanonis_site")
    doctor = commands.add_parser("doctor", help="run local checks without connecting to hardware")
    doctor.add_argument("--site")
    doctor.add_argument("--json", action="store_true")
    demo = commands.add_parser("demo", help="write a synthetic fake workflow and audit record")
    demo.add_argument("--output", required=True)
    mcp = commands.add_parser("mcp-config", help="print a Codex stdio server entry")
    mcp.add_argument("--read-root", required=True, action="append")
    mcp.add_argument("--output-root", required=True)
    mcp.add_argument("--site")
    args = parser.parse_args(argv)
    if args.command == "init":
        return _init_site(args.directory, args.profile_id)
    if args.command == "doctor":
        return _doctor(args.site, args.json)
    if args.command == "demo":
        return _demo(args.output)
    return _mcp_config(args.read_root, args.output_root, args.site)


def _init_site(directory: str, profile_id: str) -> int:
    if not _PROFILE_ID_RE.fullmatch(profile_id):
        return _error("profile id must contain 1 to 64 ASCII letters, digits, underscores, or hyphens")
    target = Path(directory).expanduser().absolute()
    if target.exists() or target.is_symlink():
        return _error("target already exists; choose a new path")
    source = DEFAULT_INSTRUMENT_PROFILE_DIRECTORY / f"{DEFAULT_INSTRUMENT_PROFILE_ID}.toml"
    if not source.is_file():
        return _error("packaged Nanonis profile template is unavailable")
    target_claimed = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Claim the destination itself exclusively; no rename can replace an existing
        # empty directory, even if another process creates it after the check above.
        target.mkdir()
        target_claimed = True
        (target / ".gitignore").write_text(
            "# Private Nanonis site configuration\n*\n", encoding="utf-8"
        )
        (target / "profiles").mkdir()
        (target / "connection.toml").write_text(_CONNECTION_TEMPLATE, encoding="utf-8")
        (target / "recording.toml").write_text(
            _RECORDING_TEMPLATE.format(profile_id=profile_id), encoding="utf-8"
        )
        profile = _prepare_draft_profile(source.read_text(encoding="utf-8"), profile_id)
        (target / "profiles" / f"{profile_id}.toml").write_text(profile, encoding="utf-8")
        (target / "README.md").write_text(_site_readme(profile_id), encoding="utf-8")
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        if target_claimed:
            _cleanup_created_target(target, target.parent)
        return _error("could not create the site configuration; check the parent directory permissions")
    print(f"Created private site configuration at {_display_path(target)}")
    print("Profile status is draft. Review all signals, units, thresholds, and commissioning before live use.")
    print(f"Run: spire doctor --site {_display_path(target)}")
    print(f"Set SPIRE_CONNECTION_CONFIG to {_display_path(target / 'connection.toml')} for Nanonis tools.")
    return 0


def _prepare_draft_profile(source: str, profile_id: str) -> str:
    replacements = {
        r'(?m)^id = "[^"]*"$': f'id = "{profile_id}"',
        r'(?m)^status = "[^"]*"$': 'status = "draft"',
        r'(?m)^reviewed_on = "[^"]*"$': 'reviewed_on = ""',
        r'(?m)^station = "[^"]*"$': 'station = "Set your local station name"',
        r'(?m)^application = "[^"]*"$': 'application = "Nanonis; verify software and controller"',
        r"(?m)^active_session_path = .*?$": 'active_session_path = "../data/session"',
        r'(?m)^lowest_experiment_setpoint_range_a = \[[^\]]*\]$':
            'lowest_experiment_setpoint_range_a = [1e-12, 2e-12] # Example only; replace with measured site limits.',
        r'(?m)^friendly_name = "[^"]*"$': 'friendly_name = "Not configured"',
        r'(?m)^interface = "[^"]*"$': 'interface = "not_configured"',
        r'(?m)^windows_driver_service = "[^"]*"$': 'windows_driver_service = "not_configured"',
        r'(?m)^capture_backend = "[^"]*"$': 'capture_backend = "unavailable"',
        r'(?m)^spire_vision_gate_status = "[^"]*"$': 'spire_vision_gate_status = "not_implemented"',
        r'(?m)^signal_names = \[[^\]]*\]$':
            'signal_names = ["Current (A)", "X (m)", "Y (m)", "Z (m)"] # Verify channel names and units.',
        r'(?m)^minimum_effective_current_a = [^\r\n]+$':
            'minimum_effective_current_a = 1e-12 # Example only; calibrate before live use.',
        r'(?m)^minimum_setpoint_fraction = [^\r\n]+$':
            'minimum_setpoint_fraction = 0.7 # Example only; calibrate before live use.',
        r'(?m)^maximum_setpoint_fraction = [^\r\n]+$':
            'maximum_setpoint_fraction = 1.5 # Example only; calibrate before live use.',
        r'(?m)^minimum_z_limit_clearance_fraction = [^\r\n]+$':
            'minimum_z_limit_clearance_fraction = 1e-4 # Example only; calibrate before live use.',
        r'(?m)^minimum_z_limit_clearance_m = [^\r\n]+$':
            'minimum_z_limit_clearance_m = 1e-12 # Example only; calibrate before live use.',
        r'(?m)^arrival_hold_s = [^\r\n]+$':
            'arrival_hold_s = 1.0 # Example only; verify against sampled controller behavior.',
        r'(?m)^arrival_minimum_samples = [^\r\n]+$':
            'arrival_minimum_samples = 8 # Example only; verify against sample rate.',
        r'(?m)^far_noise_current_ceiling_a = [^\r\n]+$':
            'far_noise_current_ceiling_a = 5e-13 # Example only; calibrate before live use.',
        r'(?m)^far_noise_window_s = [^\r\n]+$':
            'far_noise_window_s = 1.0 # Example only; calibrate before live use.',
        r'(?m)^far_noise_minimum_samples = [^\r\n]+$':
            'far_noise_minimum_samples = 8 # Example only; calibrate before live use.',
        r'(?m)^far_noise_hold_s = [^\r\n]+$':
            'far_noise_hold_s = 0.0 # Example only; calibrate before live use.',
        r'(?m)^zctrl_withdraw_closes_segment = [^\r\n]+$':
            'zctrl_withdraw_closes_segment = false # Conservative example; verify against local acquisition semantics.',
        r'(?m)^live_status = "[^"]*"$': 'live_status = "not_commissioned"',
        r'(?m)^nanonis_gui_state_reflection = "[^"]*"$':
            'nanonis_gui_state_reflection = "not_commissioned"',
        r'(?m)^direction_readback = "[^"]*"$':
            'direction_readback = "not_commissioned; verify the supported readback before use"',
        r'(?m)^start_live_status = "[^"]*"$': 'start_live_status = "not_commissioned"',
        r'(?m)^commissioned_on = "[^"]*"$': 'commissioned_on = ""',
    }
    result = source
    for pattern, replacement in replacements.items():
        result, count = re.subn(pattern, replacement, result)
        expected = 2 if pattern in {
            r'(?m)^live_status = "[^"]*"$',
            r'(?m)^commissioned_on = "[^"]*"$',
        } else 1
        if count != expected:
            raise ValueError("bundled Nanonis profile no longer matches its expected schema")
    result = re.sub(r"(?m)^\s*#.*(?:\r?\n|$)", "", result)
    result = re.sub(r"(?ms)^\[\[evidence\]\]\s*.*?(?=^\[\[evidence\]\]|\Z)", "", result)
    result = re.sub(
        r"(?m)^\[profile\]\s*$",
        "evidence = [] # No site-specific commissioning evidence has been recorded.\n\n[profile]",
        result,
        count=1,
    )
    result = re.sub(
        r"(?ms)^\[(?:motor_control|feedback_poke)\]\s*\n.*?(?=^\[|\Z)",
        "",
        result,
    )
    tomllib.loads(result)
    return result


def _site_readme(profile_id: str) -> str:
    return f"""# Local Nanonis site configuration

This directory contains private, machine-specific configuration. Keep it out of Git and review all values before sharing.

1. Set the Nanonis Programming Interface host and port in connection.toml; leave backend = "fake" during offline setup.
2. In profiles/{profile_id}.toml, verify the instrument/software, signal names and units, current range, setpoint range, noise thresholds, and supported capabilities. Example values are not calibrated safety limits.
3. Review source SafetyGate bounds against your instrument and tip holder. A site profile cannot expand code-enforced bounds.
4. Run spire doctor --site . offline. Commission live actions on site. Change status to "reviewed" only after recording real evidence and software versions.
5. Recording stays disabled. Review its gate, data path, and separate commissioning before enabling it.

Signal names and numeric limits are examples. Profile review alone does not prove hardware acceptance.
"""


def _doctor(site: str | None, as_json: bool) -> int:
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    py_ok = sys.version_info >= (3, 11)
    add("python", "ok" if py_ok else "error",
        "Python 3.11 or newer." if py_ok else "Install Python 3.11 or newer.")
    for module in ("numpy", "pydantic"):
        present = importlib.util.find_spec(module) is not None
        add(f"dependency:{module}", "ok" if present else "error",
            "Installed." if present else "Install the base project dependencies.")
    for module, extra in (("mcp", "agent"), ("nanonis_spm", "live")):
        present = importlib.util.find_spec(module) is not None
        add(f"optional:{extra}", "available" if present else "not_installed",
            "Optional dependency is available." if present else f"Optional; install the [{extra}] extra when needed.")

    site_dir = None if site is None else Path(site).expanduser().resolve()
    connection_path = None if site_dir is None else site_dir / "connection.toml"
    recording_path = DEFAULT_RECORDING_CONFIG if site_dir is None else site_dir / "recording.toml"
    try:
        config = load_connection_config(connection_path)
        add("nanonis_connection_config", "ok",
            f"Configuration is valid; selected backend is {config.backend}. The interface was not contacted.")
    except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError):
        add("nanonis_connection_config", "error",
            "Configuration is missing or invalid. Check the connection table, host, port, timeout, and backend.")

    if recording_path.is_file():
        local_path = None if site_dir is None else site_dir / "recording.local.toml"
        try:
            settings = load_recording_settings(recording_path, local_path)
            profile = settings.instrument_profile
            profile_status = (
                "reference_only" if site_dir is None else
                "ok" if profile.status == "reviewed" else "draft"
            )
            add("site_profile", profile_status,
                "Bundled source profile is a reference; it does not identify a commissioned installation."
                if site_dir is None else
                "Reviewed profile loaded; live hardware and actions remain untested." if profile.status == "reviewed" else
                "Draft loaded for offline use; site limits and live actions remain uncommissioned.")
            parent = settings.root.parent
            writable = parent.is_dir() and os.access(parent, os.W_OK)
            add("recording_output_parent", "ok" if writable else "needs_attention",
                "Parent directory exists and is writable." if writable else
                "Create the output parent and check local permissions before enabling recording.")
        except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError):
            add("site_profile", "error",
                "Recording settings or the named profile are invalid; check the local TOML files and required fields.")
    elif site_dir is not None:
        add("site_profile", "error", "recording.toml is missing from the requested site directory.")
    else:
        add("site_profile", "reference_only",
            "No recording configuration is installed; run spire init to create a site-specific draft.")

    try:
        with NanonisDevice(backend=FakeNanonisBackend()) as device:
            fake_ok = device.is_fake_backend
        add("fake_backend", "ok" if fake_ok else "error",
            "Explicit offline backend initialized; no instrument connection was made.")
    except Exception:
        add("fake_backend", "error", "Offline fake backend could not initialize; reinstall the base dependencies.")

    errors = sum(item["status"] == "error" for item in checks)
    report = {
        "offline_ready": errors == 0,
        "live_readiness": "requires separate on-site review and commissioning",
        "hardware_contacted": False,
        "network_requests": 0,
        "checks": checks,
    }
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in checks:
            print(f"{item['status']:16} {item['check']}: {item['detail']}")
        print(f"offline_ready={str(report['offline_ready']).lower()}")
        print(f"live_readiness={report['live_readiness']}")
    return 1 if errors else 0


def _demo(output: str) -> int:
    from instrument_layer import CoarseXYDirection
    from runtime_layer.spm_tools import SPMTools

    root = Path(output).expanduser().absolute()
    if root.exists() or root.is_symlink():
        return _error("demo output must be a new path; existing files were not changed")
    if not root.parent.is_dir():
        return _error("demo output parent directory must already exist")
    try:
        root.mkdir()
        tools = SPMTools(read_roots=[root], output_root=root / "derived")
        result = tools.run_fake_coarse_relocation(
            CoarseXYDirection.Y_MINUS.value, retract_steps=10, xy_steps=200,
            group=2, approach_timeout_ms=10_000,
        )
        database = result.data["database_path"]
        evidence = tools.inspect_workflow_run(database)
        summary_path = root / "demo-summary.json"
        summary_path.write_text(json.dumps({
            "synthetic": True,
            "mode": "fake_only",
            "workflow": "bounded_coarse_relocation_rehearsal",
            "result": result.data,
            "persisted_events": evidence.data,
            "tip_quality": "not_assessed",
            "evidence_scope": "Synthetic fake-backend command and state checks only; no physical motion or sample result was measured.",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        return _error("fake demo failed; inspect the new output directory for partial evidence")
    print(json.dumps({"synthetic": True, "status": result.data.get("status"),
                      "summary": _display_path(summary_path),
                      "database": _display_path(Path(database))}, ensure_ascii=False, indent=2))
    return 1 if result.is_error or evidence.is_error else 0


def _mcp_config(read_roots: list[str], output_root: str, site: str | None) -> int:
    roots = [Path(item).expanduser().resolve() for item in read_roots]
    if not roots or any(not root.is_dir() for root in roots):
        return _error("every --read-root must name an existing local directory")
    output = Path(output_root).expanduser().resolve()
    if output.exists() and not output.is_dir():
        return _error("--output-root must name a directory or a new directory path")
    connection = None
    if site is not None:
        site_dir = Path(site).expanduser().resolve()
        connection = site_dir / "connection.toml"
        if not connection.is_file() or not (site_dir / "recording.toml").is_file():
            return _error("--site must contain connection.toml and recording.toml")

    args = ["-m", "runtime_layer.agent_cli", "--mcp"]
    for path in roots:
        args.extend(("--read-root", str(path)))
    args.extend(("--output-root", str(output)))
    env = {"NANONIS_BACKEND": "fake", "RUN_LIVE_NANONIS": "0"}
    if connection is not None:
        env["SPIRE_CONNECTION_CONFIG"] = str(connection)
    lines = ["# Read-only analysis and explicit fake rehearsals; review roots before enabling.",
             "[mcp_servers.spire]"]
    lines.append(f"command = {_toml_string(sys.executable)}")
    lines.append("args = [" + ", ".join(_toml_string(value) for value in args) + "]")
    lines.extend(("", "[mcp_servers.spire.env]"))
    lines.extend(f"{key} = {_toml_string(value)}" for key, value in env.items())
    lines.extend(("", "# Restart Codex after adding this entry. Read roots must already exist."))
    print("\n".join(lines))
    return 0


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _cleanup_created_target(target: Path, parent: Path) -> None:
    if target.is_symlink() or not target.exists():
        return
    resolved_parent = parent.resolve()
    resolved_target = target.resolve()
    if resolved_target.parent == resolved_parent:
        shutil.rmtree(resolved_target)


def _display_path(path: Path) -> str:
    return str(path).replace("\\", "/")


def _error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
