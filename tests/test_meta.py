"""Code mode (#13): the [meta] config model, deterministic search ranking,
get_schema reading the POST-REWRITE broadcast, execute through the live proxy
path, and the disabled-by-default mount behavior."""

from __future__ import annotations

import tomllib

import anyio
import pytest
import structlog
from fastmcp import Client, FastMCP
from fastmcp.server import create_proxy
from fastmcp.server.transforms import ToolTransform
from fastmcp.tools.tool_transform import ArgTransformConfig, ToolTransformConfig
from starlette.testclient import TestClient

from mcp_gateway import composite, meta, server
from mcp_gateway import config_loader as cl

log = structlog.get_logger("test")


# --- config model ------------------------------------------------------------


def _raw(**over) -> dict:
    raw = {"backends": [{"name": "b", "transport": "http", "url": "http://x/mcp"}]}
    raw.update(over)
    return raw


def test_meta_disabled_by_default():
    cfg = cl.GatewayConfig.model_validate(_raw())
    assert cfg.meta.enabled is False


def test_meta_enabled_parses_and_roundtrips():
    cfg = cl.GatewayConfig.model_validate(_raw(meta={"enabled": True}))
    assert cfg.meta.enabled is True
    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
    assert reparsed == cfg


def test_meta_disabled_not_persisted():
    cfg = cl.GatewayConfig.model_validate(_raw())
    assert "meta" not in cl.to_raw(cfg)


def test_backend_named_meta_reserved_only_when_enabled():
    raw = _raw(meta={"enabled": True})
    raw["backends"].append({"name": "meta", "transport": "http", "url": "http://m/"})
    with pytest.raises(cl.ConfigError, match="reserved"):
        cl.GatewayConfig.model_validate(raw)
    # while code mode is off the name stays legal (legacy configs keep loading)
    raw["meta"] = {"enabled": False}
    cl.GatewayConfig.model_validate(raw)


def test_meta_unknown_key_rejected():
    with pytest.raises(Exception, match="extra|forbidden|not permitted"):
        cl.GatewayConfig.model_validate(_raw(meta={"enabled": True, "nope": 1}))


# --- fixtures: in-memory targets ----------------------------------------------


def _registry() -> dict:
    """Two in-memory 'proxies' (FastMCP servers work as Client targets — the
    same in-process path the gateway's registry provides)."""
    exa = FastMCP(name="exa")

    @exa.tool(description="Search the open web and return ranked result pages.")
    def search_web(query: str) -> str:
        return f"exa results for {query}"

    gitnexus = FastMCP(name="gitnexus")

    @gitnexus.tool(description="Blast radius: what breaks if a symbol changes.")
    def impact(target: str) -> str:
        return f"impact of {target}"

    @gitnexus.tool(description="Web of callers around a symbol.")
    def context(name: str) -> str:
        return f"context of {name}"

    return {"exa": exa, "gitnexus": gitnexus}


def _rewritten_proxy():
    """A REAL gateway-style proxy: backend tool + transform renaming the tool,
    its description, and a param — get_schema must read the exposed view."""
    backend = FastMCP(name="raw")

    @backend.tool(description="sloppy original text")
    def fetch_thing(u: str, internal_flag: bool = False) -> str:
        """"""
        return f"fetched {u}"

    proxy = create_proxy(Client(backend), name="mcp-gateway-web")
    proxy.add_transform(
        ToolTransform(
            {
                "fetch_thing": ToolTransformConfig(
                    name="fetch_url",
                    description="Fetch one URL and return its content as markdown.",
                    arguments={
                        "u": ArgTransformConfig(
                            name="url", description="The absolute URL to fetch."
                        ),
                        "internal_flag": ArgTransformConfig(hide=True),
                    },
                ),
                # a second, DISABLED tool must vanish from the broadcast
            }
        )
    )
    return proxy


def _call(srv, tool: str, args: dict):
    async def go():
        async with Client(srv) as c:
            return await c.call_tool(tool, args)

    return anyio.run(go)


# --- search -------------------------------------------------------------------


def test_search_ranks_name_match_above_description_match():
    srv = meta.build_meta_server(_registry(), {}, log)
    res = _call(srv, "search", {"query": "impact"})
    rows = res.data["matches"]
    # 'impact' IS a tool name -> that row first; description-only matches after
    assert rows[0] == {
        "backend": "gitnexus",
        "tool": "impact",
        "summary": "Blast radius: what breaks if a symbol changes.",
    }


def test_search_matches_description_and_params():
    srv = meta.build_meta_server(_registry(), {}, log)
    rows = _call(srv, "search", {"query": "web"}).data["matches"]
    names = [(r["backend"], r["tool"]) for r in rows]
    assert ("exa", "search_web") == names[0]  # name substring beats description
    assert ("gitnexus", "context") in names  # 'web' in description


def test_search_backend_filter_and_limit():
    srv = meta.build_meta_server(_registry(), {}, log)
    rows = _call(srv, "search", {"query": "web", "backend": "gitnexus"}).data["matches"]
    assert all(r["backend"] == "gitnexus" for r in rows)
    limited = _call(srv, "search", {"query": "web", "limit": 1}).data["matches"]
    assert len(limited) == 1


def test_search_unknown_backend_is_structured_error():
    srv = meta.build_meta_server(_registry(), {}, log)
    body = _call(srv, "search", {"query": "x", "backend": "ghost"}).data
    assert "not mounted" in body["error"]
    assert body["mounted"] == ["exa", "gitnexus"]


def test_search_no_hits_reports_what_was_searched():
    srv = meta.build_meta_server(_registry(), {}, log)
    body = _call(srv, "search", {"query": "zzzznope"}).data
    assert body["matches"] == []
    assert body["searched"] == {"exa": 1, "gitnexus": 2}


def test_search_includes_composites_via_hooks():
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [{"name": "exa", "transport": "http", "url": "http://x/"}],
            "composites": [
                {
                    "name": "everysearch",
                    "description": "Search every provider at once.",
                    "members": [{"backend": "exa", "tool": "search_web"}],
                }
            ],
        }
    )
    hooks = {"composite_server": composite.build_composite_server(cfg, {}, log)}
    srv = meta.build_meta_server(_registry(), hooks, log)
    rows = _call(srv, "search", {"query": "everysearch"}).data["matches"]
    assert rows[0]["backend"] == "composite"
    assert rows[0]["tool"] == "everysearch"


def test_search_dead_backend_contributes_nothing_not_a_crash():
    reg = _registry()
    reg["ghostly"] = object()  # not a valid Client target -> list fails
    srv = meta.build_meta_server(reg, {}, log)
    body = _call(srv, "search", {"query": "web"}).data
    assert body["searched"]["ghostly"] == 0
    assert any(r["backend"] == "exa" for r in body["matches"])


# --- get_schema ----------------------------------------------------------------


def test_get_schema_reflects_overrides():
    """The load-bearing property: get_schema reads what the proxy actually
    broadcasts (post-rename, post-param-rewrite), not raw backend text."""
    srv = meta.build_meta_server({"web": _rewritten_proxy()}, {}, log)
    body = _call(srv, "get_schema", {"backend": "web", "tool": "fetch_url"}).data
    assert body["name"] == "fetch_url"
    assert body["description"].startswith("Fetch one URL")
    props = body["input_schema"]["properties"]
    assert "url" in props and props["url"]["description"].startswith("The absolute")
    assert "u" not in props and "internal_flag" not in props  # renamed + hidden


def test_get_schema_original_name_is_gone():
    srv = meta.build_meta_server({"web": _rewritten_proxy()}, {}, log)
    body = _call(srv, "get_schema", {"backend": "web", "tool": "fetch_thing"}).data
    assert "no tool 'fetch_thing'" in body["error"]
    assert body["available"] == ["fetch_url"]


def test_get_schema_unknown_backend():
    srv = meta.build_meta_server(_registry(), {}, log)
    body = _call(srv, "get_schema", {"backend": "nope", "tool": "x"}).data
    assert "not mounted" in body["error"]
    assert body["mounted"] == ["exa", "gitnexus"]


# --- execute --------------------------------------------------------------------


def test_execute_happy_path_through_rewritten_proxy():
    srv = meta.build_meta_server({"web": _rewritten_proxy()}, {}, log)
    body = _call(
        srv,
        "execute",
        {"backend": "web", "tool": "fetch_url", "arguments": {"url": "http://a"}},
    ).data
    assert body["ok"] is True and body["is_error"] is False
    assert body["content"][0]["text"] == "fetched http://a"


def test_execute_unknown_tool_is_honest_error_not_crash():
    srv = meta.build_meta_server(_registry(), {}, log)
    body = _call(
        srv, "execute", {"backend": "exa", "tool": "nope", "arguments": {}}
    ).data
    # tool-level error: reported inside the payload, never a raised crash
    assert body["ok"] is False or body["is_error"] is True


def test_execute_unknown_backend_is_structured_error():
    srv = meta.build_meta_server(_registry(), {}, log)
    body = _call(
        srv, "execute", {"backend": "ghost", "tool": "x", "arguments": {}}
    ).data
    assert body["ok"] is False
    assert "not mounted" in body["error"]
    assert body["mounted"] == ["exa", "gitnexus"]


# --- endpoint mount gating ------------------------------------------------------


def _app(tmp_path, enabled: bool):
    raw = _raw(meta={"enabled": enabled})
    cfg = cl.GatewayConfig.model_validate(raw)
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    return server._build_app(cfg, log, {}, {}, {}, config_path=str(path))


def test_meta_endpoint_absent_when_disabled(tmp_path):
    with TestClient(_app(tmp_path, enabled=False)) as client:
        assert client.post("/meta/mcp", json={}).status_code == 404


def test_meta_endpoint_mounted_when_enabled(tmp_path):
    with TestClient(_app(tmp_path, enabled=True)) as client:
        r = client.post("/meta/mcp", json={})
        assert r.status_code != 404  # mounted (bad MCP body, but the route exists)
