"""mcp-gateway: a local MCP proxy that rewrites every broadcast text a backend
MCP server shows Claude Code — tool names, descriptions, parameters, server
instructions — while forwarding the real calls untouched."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mcp-gateway")
except PackageNotFoundError:  # running from a checkout without install
    __version__ = "0.0.0.dev0"
