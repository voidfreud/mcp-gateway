"""Public ``tools/list`` pagination at the transformed proxy boundary."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import anyio
import structlog
from fastmcp import Client, FastMCP
from fastmcp.server import create_proxy as fastmcp_create_proxy
from mcp.types import LATEST_PROTOCOL_VERSION
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_gateway import config_loader as cl
from mcp_gateway import runtime, server
from mcp_gateway import virtual_tools as vt


def _rpc_payload(response) -> dict[str, Any]:
    """Read the one JSON-RPC response from FastMCP's JSON or SSE wire body."""
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line.removeprefix("data:").strip())
    raise AssertionError(f"missing MCP response in {response.text!r}")


def _make_source_catalog() -> FastMCP:
    """A backend which itself paginates, forcing the proxy to consume pages."""
    source = FastMCP("source", list_page_size=2)
    for number in range(55):

        def listed_tool(value: int = number) -> str:
            return str(value)

        listed_tool.__name__ = f"source_{number:02d}"
        source.tool(listed_tool)
    return source


def test_mount_paginates_full_transformed_catalog_with_opaque_cursor(monkeypatch):
    """Gateway pages its final catalog after absorbing upstream cursor pages."""
    source = _make_source_catalog()
    created_with: dict[str, Any] = {}

    def create_proxy(**settings):
        settings.pop("client_factory")
        created_with.update(settings)
        return fastmcp_create_proxy(source, **settings)

    monkeypatch.setattr(server, "FastMCPProxy", create_proxy)
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "name": "backend",
                    "transport": "stdio",
                    "command": "/bin/true",
                    "stateless": True,
                    "tools": [{"original": "source_00", "name": "renamed_00"}],
                }
            ]
        }
    )
    backend = cfg.backends[0]
    app = Starlette()

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with AsyncExitStack() as stack:
            assert await server._mount_backend(
                app,
                stack,
                backend,
                cfg,
                {backend.name: [f"source_{number:02d}" for number in range(55)]},
                {},
                {},
                runtime.BackendRuntime(),
                structlog.get_logger("test"),
            )
            yield

    app.router.lifespan_context = lifespan
    protocol_version = str(LATEST_PROTOCOL_VERSION)
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    with TestClient(app) as client:
        initialized = client.post(
            "/backend/mcp",
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": protocol_version,
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                }
            ),
            headers=headers,
        )
        assert initialized.status_code == 200
        headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
        headers["Mcp-Protocol-Version"] = protocol_version
        client.post(
            "/backend/mcp",
            content=json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            ),
            headers=headers,
        )

        names: list[str] = []
        cursor: str | None = None
        cursors: set[str] = set()
        request_id = 2
        while True:
            params = {"cursor": cursor} if cursor is not None else {}
            response = client.post(
                "/backend/mcp",
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/list",
                        "params": params,
                    }
                ),
                headers=headers,
            )
            assert response.status_code == 200
            result = _rpc_payload(response)["result"]
            page = [tool["name"] for tool in result["tools"]]
            assert 1 <= len(page) <= cl.DOWNSTREAM_TOOLS_PAGE_SIZE
            names.extend(page)
            cursor = result.get("nextCursor")
            if cursor is None:
                break
            assert isinstance(cursor, str) and cursor not in cursors
            cursors.add(cursor)
            request_id += 1

    assert created_with["list_page_size"] == cl.DOWNSTREAM_TOOLS_PAGE_SIZE == 50
    expected = {"renamed_00", *(f"source_{number:02d}" for number in range(1, 55))}
    assert len(names) == 55
    assert set(names) == expected
    assert len(names) == len(set(names))


def test_virtual_catalog_uses_the_same_bounded_pages():
    definitions = [
        {
            "name": f"virtual_{number:02d}",
            "description": f"Virtual tool {number}",
            "enabled": True,
            "inputs": [{"name": "query", "type": "string"}],
            "members": [
                {
                    "backend_id": "backend-a",
                    "tool_original": "search",
                    "args": {"query": "query"},
                }
            ],
        }
        for number in range(55)
    ]
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "id": "backend-a",
                    "name": "source",
                    "transport": "stdio",
                    "command": "/bin/true",
                    "enabled": False,
                }
            ],
            "virtual_tools": definitions,
        }
    )
    virtual = vt.build_virtual_server(cfg, cfg, {}, structlog.get_logger("test"))

    async def page_sizes() -> tuple[list[int], list[str]]:
        sizes = []
        names = []
        cursor = None
        async with Client(virtual) as client:
            while True:
                page = await client.list_tools_mcp(cursor=cursor)
                sizes.append(len(page.tools))
                names.extend(tool.name for tool in page.tools)
                cursor = page.nextCursor
                if cursor is None:
                    return sizes, names

    sizes, names = anyio.run(page_sizes)
    assert sizes == [cl.DOWNSTREAM_TOOLS_PAGE_SIZE, 5]
    assert names == [f"virtual_{number:02d}" for number in range(55)]
