"""Composite tools (#14): config models, fan-out/merge, partial failure,
schema synthesis, the #21 dispatch seam, and the admin list/toggle routes."""

from __future__ import annotations

import anyio
import pytest
import structlog
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_gateway import admin, composite
from mcp_gateway import config_loader as cl

log = structlog.get_logger("test")


# --- config model helpers ----------------------------------------------------


def _base_raw(**over) -> dict:
    raw = {
        "backends": [
            {"name": "exa", "transport": "http", "url": "http://x/mcp"},
            {"name": "tavily", "transport": "http", "url": "http://y/mcp"},
        ],
        "composites": [
            {
                "name": "web_search",
                "description": "Search the web via every provider at once.",
                "params": [
                    {"name": "query", "type": "string", "description": "the query"},
                    {
                        "name": "limit",
                        "type": "integer",
                        "required": False,
                        "default": 5,
                    },
                ],
                "members": [
                    {
                        "backend": "exa",
                        "tool": "search_web",
                        "args": {"query": "query"},
                    },
                    {
                        "backend": "tavily",
                        "tool": "search_web_filtered",
                        "label": "tavily",
                        "args": {"query": "query", "max_results": "limit"},
                        "static_args": {"search_depth": "basic"},
                        "timeout": 10,
                    },
                ],
            }
        ],
    }
    raw.update(over)
    return raw


def _comp(cfg: cl.GatewayConfig) -> cl.Composite:
    return cfg.composites[0]


# --- config models -----------------------------------------------------------


def test_composite_config_parses():
    cfg = cl.GatewayConfig.model_validate(_base_raw())
    c = _comp(cfg)
    assert c.name == "web_search"
    assert c.enabled and c.strategy == "all"
    assert [p.name for p in c.params] == ["query", "limit"]
    assert c.members[1].static_args == {"search_depth": "basic"}
    assert c.members[1].timeout == 10


def test_composite_toml_roundtrip():
    cfg = cl.GatewayConfig.model_validate(_base_raw())
    import tomllib

    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
    assert reparsed == cfg


def test_composite_config_without_composites_still_loads():
    raw = _base_raw()
    del raw["composites"]
    cfg = cl.GatewayConfig.model_validate(raw)
    assert cfg.composites == []


def test_duplicate_composite_names_rejected():
    raw = _base_raw()
    raw["composites"].append(dict(raw["composites"][0]))
    with pytest.raises(cl.ConfigError, match="duplicate composite"):
        cl.GatewayConfig.model_validate(raw)


def test_member_unknown_backend_rejected():
    raw = _base_raw()
    raw["composites"][0]["members"][0]["backend"] = "ghost"
    with pytest.raises(cl.ConfigError, match="unknown backend 'ghost'"):
        cl.GatewayConfig.model_validate(raw)


def test_member_undeclared_param_mapping_rejected():
    raw = _base_raw()
    raw["composites"][0]["members"][0]["args"] = {"query": "nope"}
    with pytest.raises(cl.ConfigError, match="undeclared composite param"):
        cl.GatewayConfig.model_validate(raw)


def test_required_param_with_default_rejected():
    raw = _base_raw()
    raw["composites"][0]["params"][0]["default"] = "x"
    with pytest.raises(cl.ConfigError, match="required param cannot take"):
        cl.GatewayConfig.model_validate(raw)


def test_no_members_rejected():
    raw = _base_raw()
    raw["composites"][0]["members"] = []
    with pytest.raises(cl.ConfigError, match="no members"):
        cl.GatewayConfig.model_validate(raw)


def test_args_static_args_overlap_rejected():
    raw = _base_raw()
    raw["composites"][0]["members"][0]["static_args"] = {"query": "fixed"}
    with pytest.raises(cl.ConfigError, match="both args and static_args"):
        cl.GatewayConfig.model_validate(raw)


def test_backend_named_composite_reserved_only_with_composites():
    raw = _base_raw()
    raw["backends"].append(
        {"name": "composite", "transport": "http", "url": "http://z/mcp"}
    )
    with pytest.raises(cl.ConfigError, match="reserved"):
        cl.GatewayConfig.model_validate(raw)
    # without composites the name stays legal (legacy configs keep loading)
    del raw["composites"]
    cl.GatewayConfig.model_validate(raw)


# --- fan-out / merge ---------------------------------------------------------


def _registry(fail_tavily: bool = False, slow_tavily: float = 0.0) -> dict:
    """Two in-memory 'proxies' (FastMCP servers work as Client targets — same
    in-process path the gateway's registry provides)."""
    exa = FastMCP(name="exa")

    @exa.tool
    def search_web(query: str) -> str:
        return f"exa results for {query}"

    tavily = FastMCP(name="tavily")

    @tavily.tool
    async def search_web_filtered(
        query: str, max_results: int = 3, search_depth: str = "std"
    ) -> str:
        if slow_tavily:
            import asyncio

            await asyncio.sleep(slow_tavily)
        if fail_tavily:
            raise ValueError("boom")
        return f"tavily {query} n={max_results} depth={search_depth}"

    return {"exa": exa, "tavily": tavily}


def _cfg(**over) -> cl.GatewayConfig:
    return cl.GatewayConfig.model_validate(_base_raw(**over))


def test_fanout_merges_all_members_with_arg_mapping():
    comp = _comp(_cfg())
    merged = anyio.run(
        composite.run_composite, comp, {"query": "q1", "limit": 7}, _registry(), log
    )
    assert "## exa/search_web — ok" in merged
    assert "exa results for q1" in merged
    assert "## tavily — ok" in merged  # label wins
    assert "tavily q1 n=7 depth=basic" in merged  # mapped + static arg


def test_omitted_optional_param_uses_member_default():
    comp = _comp(_cfg())
    merged = anyio.run(composite.run_composite, comp, {"query": "q"}, _registry(), log)
    assert "n=3" in merged  # member tool's own default applied


def test_partial_failure_reports_member_and_keeps_result():
    comp = _comp(_cfg())
    merged = anyio.run(
        composite.run_composite,
        comp,
        {"query": "q"},
        _registry(fail_tavily=True),
        log,
    )
    assert "exa results for q" in merged
    assert "## tavily — error" in merged
    assert "boom" in merged


def test_unmounted_backend_is_a_member_error_not_a_crash():
    comp = _comp(_cfg())
    reg = _registry()
    del reg["tavily"]
    merged = anyio.run(composite.run_composite, comp, {"query": "q"}, reg, log)
    assert "exa results for q" in merged
    assert "'tavily' is not mounted" in merged


def test_member_timeout_reports_timeout_status():
    raw = _base_raw()
    raw["composites"][0]["members"][1]["timeout"] = 0.05
    comp = _comp(cl.GatewayConfig.model_validate(raw))
    merged = anyio.run(
        composite.run_composite,
        comp,
        {"query": "q"},
        _registry(slow_tavily=1.0),
        log,
    )
    assert "## tavily — timeout" in merged
    assert "no result within 0.05s" in merged
    assert "exa results for q" in merged


def test_all_members_failed_raises_tool_error():
    comp = _comp(_cfg())
    with pytest.raises(ToolError, match="all 2 member"):
        anyio.run(composite.run_composite, comp, {"query": "q"}, {}, log)


# --- dispatch seam (#21) -----------------------------------------------------


def test_strategy_all_selects_every_member():
    comp = _comp(_cfg())
    assert composite.select_members(comp, {"query": "q"}) == comp.members


def test_strategy_seam_is_pluggable(monkeypatch):
    """A future router registers a strategy that picks a per-call SUBSET."""
    comp = _comp(_cfg())
    monkeypatch.setitem(composite.STRATEGIES, "all", lambda c, args: [c.members[0]])
    merged = anyio.run(composite.run_composite, comp, {"query": "q"}, _registry(), log)
    assert "exa results for q" in merged
    assert "tavily" not in merged


# --- tool/server build -------------------------------------------------------


def test_built_tool_schema_matches_config():
    comp = _comp(_cfg())
    tool = composite.build_composite_tool(comp, {}, log)
    schema = tool.parameters
    assert schema["required"] == ["query"]
    assert schema["properties"]["query"]["description"] == "the query"
    assert schema["properties"]["limit"] == {"default": 5, "type": "integer"}


def test_optional_param_without_default_emits_valid_schema():
    """Review finding: the ordinary 'required = false, no default' pattern used
    to emit {"type": "string", "default": null} — self-contradictory JSON
    Schema. The type must admit null when the default is null."""
    raw = _base_raw()
    raw["composites"][0]["params"].append({"name": "lang", "required": False})
    comp = _comp(cl.GatewayConfig.model_validate(raw))
    schema = composite.build_composite_tool(comp, {}, log).parameters
    lang = schema["properties"]["lang"]
    assert lang["default"] is None
    types = lang.get("anyOf") or [lang]
    assert {"type": "null"} in types  # null is a legal value for the type
    assert "lang" not in schema["required"]


def test_param_default_must_match_declared_type():
    raw = _base_raw()
    raw["composites"][0]["params"].append(
        {"name": "n", "type": "integer", "required": False, "default": "five"}
    )
    with pytest.raises(cl.ConfigError, match="does not match type"):
        cl.GatewayConfig.model_validate(raw)


def test_param_bool_default_rejected_for_integer_type():
    # isinstance(True, int) is True in Python but not in JSON Schema
    raw = _base_raw()
    raw["composites"][0]["params"].append(
        {"name": "n", "type": "integer", "required": False, "default": True}
    )
    with pytest.raises(cl.ConfigError, match="does not match type"):
        cl.GatewayConfig.model_validate(raw)


def test_member_tool_name_must_not_be_blank():
    raw = _base_raw()
    raw["composites"][0]["members"][0]["tool"] = "  "
    with pytest.raises(cl.ConfigError, match="must not be empty"):
        cl.GatewayConfig.model_validate(raw)


def test_always_load_pins_composite_tool():
    raw = _base_raw()
    raw["composites"][0]["always_load"] = True
    comp = _comp(cl.GatewayConfig.model_validate(raw))
    tool = composite.build_composite_tool(comp, {}, log)
    assert tool.meta["anthropic/alwaysLoad"] is True


def test_end_to_end_composite_call_through_server():
    """Claude's view: list + call the composite tool over the MCP protocol."""
    cfg = _cfg()
    server = composite.build_composite_server(cfg, _registry(), log)

    async def go():
        async with Client(server) as c:
            tools = await c.list_tools()
            assert [t.name for t in tools] == ["web_search"]
            assert tools[0].description.startswith("Search the web")
            res = await c.call_tool("web_search", {"query": "q9"})
            return res.content[0].text

    merged = anyio.run(go)
    assert "exa results for q9" in merged
    assert "tavily q9" in merged


def test_disabled_composite_not_served():
    raw = _base_raw()
    raw["composites"][0]["enabled"] = False
    server = composite.build_composite_server(
        cl.GatewayConfig.model_validate(raw), {}, log
    )

    async def go():
        async with Client(server) as c:
            return await c.list_tools()

    assert anyio.run(go) == []


# --- admin routes ------------------------------------------------------------


@pytest.fixture
def admin_app(tmp_path):
    cfg = _cfg()
    path = tmp_path / "config.toml"
    cl.save(cfg, path)
    app = Starlette()
    hooks = {"composite_server": composite.build_composite_server(cfg, {}, log)}
    admin.register(app, str(path), log, {}, {}, hooks)
    return app, path, hooks


def test_admin_lists_composites(admin_app):
    app, _path, _hooks = admin_app
    res = TestClient(app).get("/admin/api/composites")
    assert res.status_code == 200
    body = res.json()
    assert body["mounted"] is True
    (c,) = body["composites"]
    assert c["name"] == "web_search" and c["enabled"] is True
    assert c["strategy"] == "all"
    assert [m["label"] for m in c["members"]] == ["exa/search_web", "tavily"]


def test_admin_toggle_persists_and_hot_applies(admin_app):
    app, path, hooks = admin_app
    client = TestClient(app)
    res = client.post(
        "/admin/api/composite/web_search/enabled", json={"enabled": False}
    )
    assert res.status_code == 200
    assert res.json()["reloaded"] == "hot"
    assert cl.load(path).composites[0].enabled is False

    async def names():
        async with Client(hooks["composite_server"]) as c:
            return [t.name for t in await c.list_tools()]

    assert anyio.run(names) == []
    # re-enable round-trips
    client.post("/admin/api/composite/web_search/enabled", json={"enabled": True})
    assert cl.load(path).composites[0].enabled is True
    assert anyio.run(names) == ["web_search"]


def test_admin_toggle_unknown_composite_400(admin_app):
    app, _path, _hooks = admin_app
    res = TestClient(app).post(
        "/admin/api/composite/nope/enabled", json={"enabled": True}
    )
    assert res.status_code == 400


def test_admin_toggle_validates_body(admin_app):
    app, _path, _hooks = admin_app
    res = TestClient(app).post(
        "/admin/api/composite/web_search/enabled", json={"enabled": "yes"}
    )
    assert res.status_code == 400
