#!/usr/bin/env python3
"""Minimal, deterministic stdio backend for the official MCP smoke subset.

It intentionally implements only an ordinary tool.  The official conformance
runner's full suite needs many specially named fixture tools with prescribed
responses, so this is not presented as a complete certification fixture.
"""

from fastmcp import FastMCP

mcp = FastMCP(name="mcp-gateway-official-conformance-fixture")


@mcp.tool
def fixture_echo(message: str = "ok") -> str:
    """Return a deterministic value so tools/list has a real tool to inspect."""
    return message


if __name__ == "__main__":
    mcp.run(transport="stdio")
