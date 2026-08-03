#!/usr/bin/env python3
"""Deterministic stdio backend for the applicable official server scenarios."""

from __future__ import annotations

import anyio
from fastmcp import Context, FastMCP
from fastmcp.prompts import Message, PromptResult
from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import FunctionTool
from mcp import types

mcp = FastMCP(
    name="mcp-gateway-official-conformance-fixture",
    dereference_schemas=False,
)

PNG_DATA = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
AUDIO_DATA = "UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA="


@mcp.tool(name="test_simple_text")
def simple_text() -> str:
    """Return the official fixture's deterministic text response."""
    return "This is a simple text response for testing."


@mcp.tool(name="test_image_content")
def image_content() -> ToolResult:
    """Return a minimal PNG image content block."""
    return ToolResult(
        content=[types.ImageContent(type="image", data=PNG_DATA, mimeType="image/png")]
    )


@mcp.tool(name="test_audio_content")
def audio_content() -> ToolResult:
    """Return a minimal WAV audio content block."""
    return ToolResult(
        content=[
            types.AudioContent(type="audio", data=AUDIO_DATA, mimeType="audio/wav")
        ]
    )


@mcp.tool(name="test_embedded_resource")
def embedded_resource() -> ToolResult:
    """Return one embedded text resource."""
    return ToolResult(
        content=[
            types.EmbeddedResource(
                type="resource",
                resource=types.TextResourceContents(
                    uri="test://embedded-resource",
                    mimeType="text/plain",
                    text="This is an embedded resource content.",
                ),
            )
        ]
    )


@mcp.tool(name="test_multiple_content_types")
def multiple_content_types() -> ToolResult:
    """Return text, image, and embedded-resource content together."""
    return ToolResult(
        content=[
            types.TextContent(type="text", text="Multiple content types test:"),
            types.ImageContent(type="image", data=PNG_DATA, mimeType="image/png"),
            types.EmbeddedResource(
                type="resource",
                resource=types.TextResourceContents(
                    uri="test://mixed-content-resource",
                    mimeType="application/json",
                    text='{"test":"data","value":123}',
                ),
            ),
        ]
    )


@mcp.tool(name="test_tool_with_logging")
async def tool_with_logging(ctx: Context) -> str:
    """Send three deterministic log notifications during execution."""
    await ctx.info("Tool execution started")
    await anyio.sleep(0.05)
    await ctx.info("Tool processing data")
    await anyio.sleep(0.05)
    await ctx.info("Tool execution completed")
    return "Tool execution completed"


@mcp.tool(name="test_error_handling")
def error_handling() -> ToolResult:
    """Return a protocol-level tool error with explanatory content."""
    return ToolResult(
        content=[
            types.TextContent(
                type="text",
                text="This tool intentionally returns an error for testing",
            )
        ],
        is_error=True,
    )


@mcp.tool(name="test_tool_with_progress")
async def tool_with_progress(ctx: Context) -> str:
    """Send three monotonically increasing progress notifications."""
    await ctx.report_progress(0, 100)
    await anyio.sleep(0.05)
    await ctx.report_progress(50, 100)
    await anyio.sleep(0.05)
    await ctx.report_progress(100, 100)
    return "Progress completed"


def json_schema_tool(name: str = "", address: dict | None = None) -> str:
    """Exercise JSON Schema 2020-12 keyword preservation."""
    return f"{name}:{address}"


schema_tool = FunctionTool.from_function(
    json_schema_tool,
    name="json_schema_2020_12_tool",
    description="Tool with JSON Schema 2020-12 features",
)
schema_tool.parameters = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "$defs": {
        "address": {
            "type": "object",
            "properties": {
                "street": {"type": "string"},
                "city": {"type": "string"},
            },
        }
    },
    "properties": {
        "name": {"type": "string"},
        "address": {"$ref": "#/$defs/address"},
    },
    "additionalProperties": False,
}
mcp.add_tool(schema_tool)


@mcp.resource(
    "test://static-text",
    name="static-text",
    description="Official conformance text resource.",
    mime_type="text/plain",
)
def static_text() -> str:
    """Return deterministic text resource content."""
    return "This is the content of the static text resource."


@mcp.resource(
    "test://static-binary",
    name="static-binary",
    description="Official conformance binary resource.",
    mime_type="image/png",
)
def static_binary() -> bytes:
    """Return deterministic binary resource content."""
    return b"\x89PNG\r\n\x1a\n"


@mcp.resource(
    "test://template/{id}/data",
    name="template-data",
    description="Official conformance parameterized resource.",
    mime_type="application/json",
)
def template_data(id: str) -> str:
    """Return content containing the substituted template identifier."""
    return f'{{"id":"{id}","templateTest":true,"data":"Data for ID: {id}"}}'


@mcp.prompt(name="test_simple_prompt")
def simple_prompt() -> PromptResult:
    """Return the official fixture's simple prompt."""
    return PromptResult("This is a simple prompt for testing.")


@mcp.prompt(name="test_prompt_with_arguments")
def prompt_with_arguments(arg1: str, arg2: str) -> PromptResult:
    """Return a prompt containing both required arguments."""
    return PromptResult(f"Prompt with arguments: arg1='{arg1}', arg2='{arg2}'")


@mcp.prompt(name="test_prompt_with_embedded_resource")
def prompt_with_embedded_resource(resourceUri: str) -> PromptResult:  # noqa: N803
    """Return a prompt containing an embedded resource and text."""
    return PromptResult(
        [
            Message(
                types.EmbeddedResource(
                    type="resource",
                    resource=types.TextResourceContents(
                        uri=resourceUri,
                        mimeType="text/plain",
                        text="Embedded resource content for testing.",
                    ),
                )
            ),
            Message("Please process the embedded resource above."),
        ]
    )


@mcp.prompt(name="test_prompt_with_image")
def prompt_with_image() -> PromptResult:
    """Return a prompt containing a PNG image and text."""
    return PromptResult(
        [
            Message(
                types.ImageContent(type="image", data=PNG_DATA, mimeType="image/png")
            ),
            Message("Please analyze the image above."),
        ]
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
