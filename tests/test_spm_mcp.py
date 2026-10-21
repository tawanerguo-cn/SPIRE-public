"""Optional real stdio transport test; its only instrument is the fake backend."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from test_tip_reference_verification import _write_sxm
from test_recording_review import make_recording
from runtime_layer.spm_tools import SPMTools
from runtime_layer.tool_registry import ToolRegistry


@unittest.skipUnless(importlib.util.find_spec("mcp") and importlib.util.find_spec("matplotlib"),
                     "optional agent and validation dependencies")
class SPMMCPTests(unittest.TestCase):
    def test_stdio_tools_images_errors_plan_and_fake_execution(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def exercise(root):
            path = root / "source.sxm"
            _write_sxm(path)
            path.write_bytes(path.read_bytes().replace(b":SCAN_DIR:", b":SCAN_ANGLE:\n0\n:SCAN_DIR:"))
            env = dict(os.environ) | {"RUN_LIVE_NANONIS": "0", "MPLCONFIGDIR": str(root / "mpl"),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            env.pop("OPENAI_API_KEY", None)
            server = StdioServerParameters(command=sys.executable, args=["-B", "-m",
                "runtime_layer.agent_cli", "--mcp", "--read-root", str(root),
                "--output-root", str(root / "derived")], env=env)
            async with stdio_client(server) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    menu = await client.list_tools()
                    local = ToolRegistry(SPMTools(read_roots=[root], output_root=root / "derived"))
                    self.assertEqual({t.name: t.input_schema for t in menu.tools},
                                     {t["name"]: t["parameters"] for t in local.definitions()})
                    self.assertEqual({t.name for t in menu.tools},
                        {"observe_scan", "view_scan_region", "prepare_atom_plan", "inspect_spectrum", "run_fake_atom_plan", "inspect_atom_run", "inspect_recording",
                         "describe_instrument_service", "run_fake_coarse_relocation", "inspect_workflow_run", "inspect_signal_window", "quantify_tip_quality", "assess_scan_flatness"})
                    catalogue = await client.call_tool("describe_instrument_service", {})
                    self.assertFalse(catalogue.is_error, catalogue)
                    self.assertFalse(catalogue.structured_content["connected_to_hardware"])
                    invalid_type = await client.call_tool("run_fake_coarse_relocation", {
                        "xy_direction": "x_plus", "retract_steps": 10, "xy_steps": "200", "group": 1})
                    self.assertTrue(invalid_type.is_error)
                    self.assertFalse((root / "derived" / "runs").exists())
                    relocation = await client.call_tool("run_fake_coarse_relocation", {
                        "xy_direction": "x_plus", "retract_steps": 10, "xy_steps": 200, "group": 1})
                    self.assertFalse(relocation.is_error, relocation)
                    self.assertEqual(relocation.structured_content["status"], "awaiting_postcheck")
                    evidence = await client.call_tool("inspect_workflow_run", {
                        "database_path": relocation.structured_content["database_path"]})
                    self.assertFalse(evidence.is_error, evidence)
                    self.assertTrue(any(e["event_type"] == "coarse_relocation_retracted"
                                        for e in evidence.structured_content["events"]))
                    rejected_move = await client.call_tool("run_fake_coarse_relocation", {
                        "xy_direction": "x_plus", "retract_steps": 10, "xy_steps": 1001, "group": 1})
                    self.assertTrue(rejected_move.is_error)
                    failure = await client.call_tool("inspect_workflow_run", {
                        "database_path": rejected_move.structured_content["database_path"]})
                    self.assertFalse(failure.is_error, failure)
                    self.assertEqual(failure.structured_content["status"], "failed")
                    recording = make_recording(root)
                    window = await client.call_tool("inspect_signal_window", {
                        "database_path": str(recording.database_path), "start_seq": 0, "stop_seq": 2,
                        "minimum_duration_s": .04, "maximum_sample_gap_s": .03})
                    self.assertFalse(window.is_error, window)
                    self.assertEqual(window.structured_content["status"], "invalid_data")
                    self.assertFalse(window.structured_content["authorizes_live_action"])
                    reviewed = await client.call_tool("inspect_recording", {"database_path": str(recording.database_path)})
                    self.assertFalse(reviewed.is_error, reviewed)
                    self.assertEqual(reviewed.structured_content["background_groups"][0]["count"], 3)
                    self.assertTrue(Path(reviewed.structured_content["summary_path"]).is_file())
                    observed = await client.call_tool("observe_scan", {"path": str(path), "mode": "joint"})
                    self.assertFalse(observed.is_error, observed)
                    data = observed.structured_content
                    self.assertEqual(data["source_path"], str(path.resolve()))
                    self.assertTrue(any(item.type == "image" for item in observed.content))
                    flatness = await client.call_tool("assess_scan_flatness", {
                        "path": str(path), "profile": "ag100_site_screen_v1"})
                    self.assertFalse(flatness.is_error, flatness)
                    self.assertFalse(flatness.structured_content["authorizes_live_action"])
                    self.assertTrue(any(item.type == "image" for item in flatness.content))
                    rejected = await client.call_tool("view_scan_region", {"path": str(path),
                        "observation_id": "stale", "row_start": 5, "row_stop": 20,
                        "column_start": 5, "column_stop": 25})
                    self.assertTrue(rejected.is_error)
                    planned = await client.call_tool("prepare_atom_plan", {"path": str(path),
                        "observation_id": data["observation_id"], "selections": [
                            {"row": 15, "column": 20, "selected_by": "transport test", "rationale": "synthetic test"}]})
                    self.assertFalse(planned.is_error, planned)
                    result = await client.call_tool("run_fake_atom_plan", {"plan_path": planned.structured_content["plan_path"]})
                    self.assertFalse(result.is_error, result)
                    self.assertEqual(result.structured_content["status"], "complete")
                    self.assertEqual(result.structured_content["atoms"][0]["classification"], "unknown")
                    recovered = await client.call_tool("inspect_atom_run", {"database_path":
                        str(Path(result.structured_content["run_directory"]) / "run.sqlite"), "include_spectra": True})
                    self.assertFalse(recovered.is_error, recovered)
                    self.assertTrue(any(event["event_type"] == "atom_spectrum_acquired"
                                        for event in recovered.structured_content["events"]))
                    bounded = await client.call_tool("run_fake_atom_plan", {
                        "plan_path": planned.structured_content["plan_path"], "maximum_atoms": 21})
                    self.assertTrue(bounded.is_error)
                    failed_read = await client.call_tool("inspect_atom_run", {"database_path":
                        str(Path(bounded.structured_content["run_directory"]) / "run.sqlite")})
                    self.assertFalse(failed_read.is_error)
                    self.assertEqual(failed_read.structured_content["status"], "failed")

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(asyncio.wait_for(exercise(Path(directory)), timeout=45))


if __name__ == "__main__":
    unittest.main()
