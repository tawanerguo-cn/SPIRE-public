"""Optional stdio MCP transport over the shared tool registry; no live mode."""
from __future__ import annotations

import asyncio
import base64
import json

from .spm_tools import SPMTools
from .tool_registry import ToolRegistry


def create_spm_server(surface: SPMTools):
    from mcp.server.lowlevel import Server
    from mcp.types import CallToolResult, ImageContent, ListToolsResult, TextContent, Tool, ToolAnnotations

    registry = ToolRegistry(surface)

    async def list_tools(context, params):
        return ListToolsResult(tools=[Tool(
            name=entry["name"], description=entry["summary"], input_schema=entry["parameters"],
            annotations=ToolAnnotations(read_only_hint=entry["read_only"],
                                        destructive_hint=False, open_world_hint=False))
            for entry in registry.definitions()])

    async def call_tool(context, params):
        result = await asyncio.to_thread(registry.invoke, params.name,
                                        params.arguments if params.arguments is not None else {})
        content = [TextContent(type="text", text=json.dumps(result.data, ensure_ascii=False, allow_nan=False))]
        content.extend(ImageContent(type="image", data=base64.b64encode(payload).decode(),
                                   mime_type="image/png") for payload in result.images)
        return CallToolResult(content=content, structured_content=result.data, is_error=result.is_error)

    return Server("SPiRE offline and fake laboratory tools", on_list_tools=list_tools, on_call_tool=call_tool)


def run_spm_server(surface: SPMTools) -> None:
    from mcp.server.stdio import stdio_server

    async def serve():
        server = create_spm_server(surface)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(serve())
