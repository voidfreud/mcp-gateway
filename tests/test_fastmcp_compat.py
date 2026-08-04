"""Explicit compatibility contract for the gateway's FastMCP private seams."""

import tomllib
from pathlib import Path

import anyio
from fastmcp import Client, FastMCP
from fastmcp.server import create_proxy
from mcp.server.lowlevel.server import NotificationOptions

from mcp_gateway import config_loader as cl


def test_fastmcp_runtime_dependency_is_exactly_pinned():
    """Private API use makes an open-ended FastMCP requirement unsafe."""
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text()
    )
    dependencies = pyproject["project"]["dependencies"]
    assert "fastmcp==3.4.5" in dependencies


def test_fastmcp_private_runtime_contract():
    """Fail clearly when a deliberate FastMCP upgrade moves a private seam."""
    backend = cl.Backend(name="compat", transport="stdio", command="/bin/true")
    proxy = create_proxy(cl.to_proxy_config_one(backend), name="compat-proxy")

    assert isinstance(proxy._transforms, list)
    assert isinstance(proxy._mcp_server.notification_options, NotificationOptions)

    virtual = FastMCP("compat-virtual")
    provider = virtual.local_provider
    assert isinstance(provider._components, dict)
    assert virtual.local_provider is provider


def test_fastmcp_provider_component_swap_changes_the_client_catalog():
    """The private map assignment used by virtual hot reload must stay live."""
    target = FastMCP("compat-target")
    staged = FastMCP("compat-staged")

    @target.tool
    def old_tool() -> str:
        return "old"

    @staged.tool
    def new_tool() -> str:
        return "new"

    old_components = target.local_provider._components
    target.local_provider._components = staged.local_provider._components

    async def listed_names():
        async with Client(target) as client:
            return [tool.name for tool in await client.list_tools()]

    assert target.local_provider._components is not old_components
    assert anyio.run(listed_names) == ["new_tool"]
