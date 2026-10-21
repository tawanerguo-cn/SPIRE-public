"""Real SDK protocol tests with a local HTTP transport: no API bill or hardware."""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_layer.recording import read_run_event_page
from knowledge_base.script_manifest import load_agent_tool_manifest
from runtime_layer.openai_agent import AgentLimits, run_openai_agent
from runtime_layer.spm_tools import SPMTools
from runtime_layer.tool_registry import ToolRegistry
from test_tip_reference_verification import _write_sxm

ROOT = Path(__file__).resolve().parents[1]


def call(name="describe_instrument_service", arguments="{}", call_id="call_1"):
    return {"type": "function_call", "id": "fc_" + call_id, "call_id": call_id,
            "name": name, "arguments": arguments, "status": "completed"}


def message(text="Only offline/fake evidence was observed."):
    return {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def response(output, status="completed"):
    return {"id": "resp_test", "object": "response", "created_at": 1,
            "model": "test-model", "status": status, "output": output,
            "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 2}}}


def agent_cli_command(*arguments):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env.pop("OPENAI_API_KEY", None)
    return [sys.executable, "-B", "-m", "runtime_layer.agent_cli", *arguments], env


class ToolRegistryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.surface = SPMTools(read_roots=[self.root], output_root=self.root / "out")
        self.registry = ToolRegistry(self.surface)

    def test_schema_comes_from_manifest_and_typed_defaults(self):
        definitions = {item["name"]: item for item in self.registry.definitions()}
        self.assertEqual(set(definitions), {item["name"] for item in load_agent_tool_manifest()})
        schema = definitions["observe_scan"]["parameters"]
        self.assertEqual(schema["required"], ["path"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["mode"]["default"], "joint")
        self.assertEqual(schema["properties"]["smoothing_radius_pixels"]["type"], "integer")

    def test_invalid_inputs_do_not_dispatch_or_create_workflow_runs(self):
        valid = {"xy_direction": "x_plus", "retract_steps": 10, "xy_steps": 200, "group": 1}
        attempts = [("save_images", {}), ("_read", {}), ("inspect_recording", {}),
                    ("describe_instrument_service", {"live": True}),
                    ("describe_instrument_service", "[]"), ("describe_instrument_service", "{broken"),
                    ("run_fake_coarse_relocation", valid | {"xy_steps": True}),
                    ("run_fake_coarse_relocation", valid | {"xy_steps": "200"}),
                    ("run_fake_coarse_relocation", valid | {"xy_steps": 1.5}),
                    ("run_fake_atom_plan", {"plan_path": "unused", "recipe": {"bias": float("nan")}})]
        for name, arguments in attempts:
            with self.subTest(name=name, arguments=arguments):
                result = self.registry.invoke(name, arguments)
                self.assertTrue(result.is_error)
                self.assertFalse(result.data["authorizes_live_action"])
        self.assertFalse((self.surface.output_root / "runs").exists())

    def test_file_escape_rejected_and_rehearsal_lock_released_after_failure(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "scan.sxm"
            _write_sxm(outside)
            result = self.registry.invoke("observe_scan", {"path": str(outside), "mode": "metrics"})
            self.assertTrue(result.is_error)
            self.assertIn("outside", result.data["error"])
        args = {"xy_direction": "x_plus", "retract_steps": 10, "xy_steps": 200, "group": 1}
        with patch("runtime_layer.spm_tools.rehearse_coarse_relocation", side_effect=RuntimeError("failure")):
            self.assertTrue(self.registry.invoke("run_fake_coarse_relocation", args).is_error)
        self.assertFalse(self.registry.invoke("run_fake_coarse_relocation", args).is_error)

    def test_cli_reports_invalid_json_as_json_and_nonzero_exit(self):
        command, env = agent_cli_command("--read-root", str(self.root),
            "--output-root", str(self.surface.output_root), "--tool", "describe_instrument_service")
        result = subprocess.run(command, env=env, input="{", text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["error_type"], "JSONDecodeError")
        self.assertNotIn("Traceback", result.stderr)

    def test_list_tools_and_direct_json_tool_run_without_api_credentials(self):
        command, env = agent_cli_command("--list-tools", "--read-root", str(self.root),
            "--output-root", str(self.surface.output_root))
        listed = subprocess.run(command, env=env, text=True, capture_output=True, timeout=20)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout), self.registry.definitions())
        self.assertFalse((self.surface.output_root / "agent_runs").exists())

        command, env = agent_cli_command("--read-root", str(self.root),
            "--output-root", str(self.surface.output_root), "--tool", "describe_instrument_service",
            "--arguments", "{}")
        invoked = subprocess.run(command, env=env, text=True, capture_output=True, timeout=20)
        self.assertEqual(invoked.returncode, 0, invoked.stderr)
        self.assertFalse(json.loads(invoked.stdout)["connected_to_hardware"])
        self.assertFalse((self.surface.output_root / "agent_runs").exists())

        # An empty piped stdin remains equivalent to {} for tools with no required fields.
        command, env = agent_cli_command("--read-root", str(self.root),
            "--output-root", str(self.surface.output_root), "--tool", "describe_instrument_service")
        piped = subprocess.run(command, env=env, input="", text=True, capture_output=True, timeout=20)
        self.assertEqual(piped.returncode, 0, piped.stderr)
        self.assertFalse(json.loads(piped.stdout)["connected_to_hardware"])

    def test_tty_without_arguments_does_not_read_stdin_and_reports_missing_fields(self):
        from runtime_layer import agent_cli

        class TerminalInput(io.StringIO):
            def isatty(self):
                return True

        argv = ["spire-agent", "--read-root", str(self.root), "--output-root",
                str(self.surface.output_root), "--tool", "describe_instrument_service"]
        output = io.StringIO()
        with patch.object(agent_cli.sys, "argv", argv), patch.object(agent_cli.sys, "stdin", TerminalInput()), \
             redirect_stdout(output):
            agent_cli.main()
        self.assertFalse(json.loads(output.getvalue())["connected_to_hardware"])

        argv[-1] = "observe_scan"
        output = io.StringIO()
        with patch.object(agent_cli.sys, "argv", argv), patch.object(agent_cli.sys, "stdin", TerminalInput()), \
             redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
            agent_cli.main()
        self.assertEqual(stopped.exception.code, 1)
        failure = json.loads(output.getvalue())
        self.assertFalse(failure["authorizes_live_action"])
        self.assertIn("ValidationError", failure["error_type"])
        self.assertIn("path", failure["error"])

    def test_inline_and_file_arguments_are_mutually_exclusive(self):
        command, env = agent_cli_command("--read-root", str(self.root),
            "--output-root", str(self.surface.output_root), "--tool", "describe_instrument_service",
            "--arguments", "{}", "--arguments-file", str(self.root / "arguments.json"))
        result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument", result.stderr)

    def test_api_cli_requires_explicit_model_before_starting(self):
        command, env = agent_cli_command("--api", "--read-root", str(self.root),
            "--output-root", str(self.surface.output_root))
        result = subprocess.run(command, env=env, input="task", text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--api requires --model", result.stderr)
        self.assertFalse((self.surface.output_root / "agent_runs").exists())

    def test_limits_reject_invalid_values(self):
        for values in ({"max_turns": 0}, {"max_tool_calls": -1}, {"max_output_tokens": True},
                       {"max_input_bytes": 1.5}, {"timeout_s": float("nan")}, {"timeout_s": 0}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                AgentLimits(**values)


@unittest.skipUnless(importlib.util.find_spec("openai"), "optional API dependencies")
class ResponsesRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.surface = SPMTools(read_roots=[self.root], output_root=self.root / "out")
        self.requests = []

    def run_session(self, replies, **kwargs):
        import httpx
        from openai import OpenAI
        replies = iter(replies)

        def handle(request):
            self.assertEqual(request.url.path, "/v1/responses")
            self.requests.append(json.loads(request.content))
            reply = next(replies)
            if isinstance(reply, BaseException):
                raise reply
            if isinstance(reply, int):
                return httpx.Response(reply, json={"error": {"message": "synthetic failure", "type": "test"}})
            return httpx.Response(200, json=reply)

        with OpenAI(api_key="test-only-secret", base_url="https://example.invalid/v1",
                    http_client=httpx.Client(transport=httpx.MockTransport(handle))) as client:
            return run_openai_agent(self.surface, client=client, model="test-model", prompt="Inspect saved evidence.", **kwargs)

    def events(self, result):
        page = read_run_event_page(result["database_path"])
        self.assertEqual(page["status"], result["status"])
        return page["events"]

    def test_sdk_roundtrip_reasoning_tool_output_and_durable_audit(self):
        reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
        result = self.run_session([response([reasoning, call()]), response([message()])])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["tool_calls"], 1)
        first, second = self.requests
        self.assertFalse(first["store"])
        self.assertFalse(first["parallel_tool_calls"])
        self.assertEqual(first["max_output_tokens"], 4096)
        self.assertIn(reasoning, second["input"])
        output = second["input"][-1]
        self.assertEqual(output["call_id"], "call_1")
        self.assertFalse(json.loads(output["output"])["data"]["connected_to_hardware"])
        events = self.events(result)
        self.assertEqual([e["event_type"] for e in events], ["agent_started", "agent_request_started",
            "agent_response", "agent_tool_started", "agent_tool_finished", "agent_request_started",
            "agent_response", "agent_finished"])
        self.assertNotIn("test-only-secret", json.dumps(events))
        self.assertEqual(events[2]["payload"]["usage"]["total_tokens"], 30)

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "optional plotting dependency")
    def test_real_scan_image_reaches_model_and_is_saved(self):
        path = self.root / "scan.sxm"
        _write_sxm(path)
        path.write_bytes(path.read_bytes().replace(b":SCAN_DIR:", b":SCAN_ANGLE:\n0\n:SCAN_DIR:"))
        result = self.run_session([response([call("observe_scan", json.dumps({"path": str(path)}))]),
                                   response([message()])])
        self.assertEqual(result["status"], "complete")
        content = self.requests[1]["input"][-1]["output"]
        png = base64.b64decode(content[1]["image_url"].split(",", 1)[1])
        self.assertTrue(png.startswith(b"\x89PNG"))
        tool = next(e["payload"] for e in self.events(result) if e["event_type"] == "agent_tool_finished")
        self.assertEqual(Path(tool["image_paths"][0]).read_bytes(), png)
        self.assertFalse(tool["data"]["authorizes_live_action"])

    def test_fake_workflow_still_enforces_existing_safety_bounds(self):
        args = {"xy_direction": "x_plus", "retract_steps": 10, "xy_steps": 1001, "group": 1}
        result = self.run_session([response([call("run_fake_coarse_relocation", json.dumps(args))]),
                                   response([message("The workflow failed its bounded preflight.")])])
        self.assertEqual(result["tool_errors"], 1)
        output = json.loads(self.requests[1]["input"][-1]["output"])
        self.assertTrue(output["is_error"])
        self.assertEqual(read_run_event_page(output["data"]["database_path"])["status"], "failed")

    def test_malformed_and_unknown_calls_return_errors_without_dispatch(self):
        result = self.run_session([response([call(arguments="{broken")]),
            response([call("__init__", "{}", "call_2")]), response([message()])])
        self.assertEqual(result["tool_errors"], 2)
        for request in self.requests[1:]:
            self.assertTrue(json.loads(request["input"][-1]["output"])["is_error"])

    def test_duplicate_call_id_is_not_executed_twice(self):
        result = self.run_session([response([call()]), response([call()])])
        self.assertEqual(result["reason"], "invalid_or_repeated_call_id")
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual(len([e for e in self.events(result) if e["event_type"] == "agent_tool_started"]), 1)

    def test_duplicate_ids_within_batch_reject_entire_batch(self):
        result = self.run_session([response([call(), call()])])
        self.assertEqual(result["reason"], "invalid_or_repeated_call_id")
        self.assertEqual(result["tool_calls"], 0)

    def test_tool_and_turn_budgets_stop_before_side_effects(self):
        cases = [([call(), call(call_id="call_2")], AgentLimits(max_tool_calls=1), "max_tool_calls"),
                 ([call()], AgentLimits(max_turns=1), "max_turns")]
        for output, limits, reason in cases:
            with self.subTest(reason=reason):
                result = self.run_session([response(output)], limits=limits)
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["status"], "stopped")
                self.assertEqual(result["tool_calls"], 0)
                self.events(result)

    def test_input_budget_stops_before_api_request(self):
        result = self.run_session([], limits=AgentLimits(max_input_bytes=1))
        self.assertEqual(result["reason"], "max_input_bytes")
        self.assertEqual(self.requests, [])
        self.events(result)

    def test_incomplete_response_does_not_execute_partial_tools(self):
        result = self.run_session([response([call()], status="incomplete")])
        self.assertEqual(result["reason"], "response_incomplete")
        self.assertEqual(result["tool_calls"], 0)
        self.events(result)

    def test_rate_limit_and_timeout_have_no_sdk_retry(self):
        import httpx
        for error in (429, httpx.ReadTimeout("test-only-secret")):
            with self.subTest(error=type(error).__name__):
                before = len(self.requests)
                result = self.run_session([error])
                self.assertEqual(result["status"], "failed")
                self.assertEqual(len(self.requests), before + 1)
                self.assertNotIn("test-only-secret", json.dumps(self.events(result)))

    def test_failure_after_tool_preserves_result_and_does_not_replay(self):
        result = self.run_session([response([call()]), 500])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["tool_calls"], 1)
        self.assertTrue(any(e["event_type"] == "agent_tool_finished" for e in self.events(result)))

    def test_refusal_empty_response_and_interrupt_are_not_success(self):
        refused = message()
        refused["content"] = [{"type": "refusal", "refusal": "Cannot comply."}]
        for reply, reason in ((response([refused]), "model_refusal"),
                              (response([]), "empty_response"), (KeyboardInterrupt(), "interrupted")):
            with self.subTest(reason=reason):
                result = self.run_session([reply])
                self.assertEqual(result["reason"], reason)
                self.assertNotEqual(result["status"], "complete")
                self.events(result)


if __name__ == "__main__":
    unittest.main()
