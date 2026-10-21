"""MCP, direct JSON tools, or an optional model API session; no live mode exists."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from knowledge_base.script_manifest import load_agent_tool_manifest
from .spm_tools import SPMTools
from .tool_registry import ToolRegistry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-root", action="append", required=True)
    parser.add_argument("--output-root", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mcp", action="store_true")
    mode.add_argument("--tool", choices=[entry["name"] for entry in load_agent_tool_manifest()])
    mode.add_argument("--api", action="store_true", help="run an OpenAI Responses API session")
    mode.add_argument("--list-tools", action="store_true", help="print shared tool JSON schemas")
    tool_input = parser.add_mutually_exclusive_group()
    tool_input.add_argument("--arguments", help="inline JSON arguments for --tool")
    tool_input.add_argument("--arguments-file", help="read JSON arguments from a file for --tool")
    parser.add_argument("--model", help="explicit model ID; required with --api")
    parser.add_argument("--prompt-file", help="UTF-8 task text; API mode reads stdin if omitted")
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--max-tool-calls", type=int, default=16)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--max-input-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--timeout", type=float, default=120.0, help="API request timeout in seconds")
    args = parser.parse_args()
    if args.api and not args.model:
        parser.error("--api requires --model")
    if not args.api and (args.model or args.prompt_file):
        parser.error("--model and --prompt-file require --api")
    if (args.arguments is not None or args.arguments_file) and not args.tool:
        parser.error("--arguments and --arguments-file require --tool")
    surface = SPMTools(read_roots=args.read_root, output_root=args.output_root)
    if args.list_tools:
        print(json.dumps(ToolRegistry(surface).definitions(), ensure_ascii=False, allow_nan=False))
        return
    if args.mcp:
        from runtime_layer.spm_mcp import run_spm_server
        run_spm_server(surface)
        return
    if args.api:
        from runtime_layer.openai_agent import AgentLimits, run_openai_agent
        try:
            from openai import OpenAI
        except ImportError:
            parser.error('install optional API dependencies: python -m pip install -e ".[api]"')
        try:
            limits = AgentLimits(max_turns=args.max_turns, max_tool_calls=args.max_tool_calls,
                max_output_tokens=args.max_output_tokens, max_input_bytes=args.max_input_bytes,
                timeout_s=args.timeout)
        except ValueError as error:
            parser.error(str(error))
        prompt = Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else sys.stdin.read()
        if not prompt.strip():
            parser.error("provide a nonempty task in --prompt-file or stdin")
        # Credentials come from the SDK environment, never from model arguments.
        with OpenAI(max_retries=0, timeout=limits.timeout_s) as client:
            result = run_openai_agent(surface, client=client, model=args.model, prompt=prompt, limits=limits)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        if result["status"] != "complete":
            raise SystemExit(1)
        return
    if args.arguments is not None:
        arguments = args.arguments
    elif args.arguments_file:
        arguments = Path(args.arguments_file).read_text(encoding="utf-8")
    elif sys.stdin.isatty():
        arguments = "{}"
    else:
        arguments = sys.stdin.read() or "{}"
    result = ToolRegistry(surface).invoke(args.tool, arguments)
    # CLI renders are explicit local artifacts; MCP returns the same bytes natively.
    images = surface.save_images(result)
    print(json.dumps(result.data | {"image_paths": images}, ensure_ascii=False, allow_nan=False))
    if result.is_error:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
