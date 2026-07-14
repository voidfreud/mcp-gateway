"""Composite tools (#14): config models, fan-out/merge, partial failure,
schema synthesis, the #21 dispatch seam, and the admin list/toggle routes."""

from __future__ import annotations

import json

import anyio
import httpx
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
    assert anyio.run(composite.select_members, comp, {"query": "q"}) == comp.members


def test_strategy_seam_is_pluggable(monkeypatch):
    """A registered strategy picks a per-call SUBSET (sync or async both work)."""
    comp = _comp(_cfg())
    monkeypatch.setitem(
        composite.STRATEGIES, "all", lambda c, args, ctx: [c.members[0]]
    )
    merged = anyio.run(composite.run_composite, comp, {"query": "q"}, _registry(), log)
    assert "exa results for q" in merged
    assert "tavily" not in merged


# --- smart routing (#21): config validation -----------------------------------


def _routed_raw(strategy: str = "keyword", **router) -> dict:
    """The base composite reshaped for routing tests: per-member patterns and
    route descriptions, plus an optional [composites.router] table."""
    raw = _base_raw()
    c = raw["composites"][0]
    c["strategy"] = strategy
    c["members"][0]["route_patterns"] = ["code", r"regex(es)?"]
    c["members"][0]["route_description"] = "use for code and API questions"
    c["members"][1]["route_patterns"] = ["news", "current events"]
    c["members"][1]["route_description"] = "use for news and current events"
    if router or strategy == "llm":
        c["router"] = {"api_key": "${OPENROUTER_KEY}", **router}
    return raw


def test_keyword_strategy_config_parses_and_roundtrips():
    import tomllib

    cfg = cl.GatewayConfig.model_validate(_routed_raw(fallback="tavily"))
    c = _comp(cfg)
    assert c.strategy == "keyword"
    assert c.members[0].route_patterns == ["code", r"regex(es)?"]
    assert c.router.fallback == "tavily"
    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
    assert reparsed == cfg


def test_llm_strategy_config_parses_with_defaults():
    cfg = cl.GatewayConfig.model_validate(_routed_raw("llm"))
    r = _comp(cfg).router
    assert r.api_key == "${OPENROUTER_KEY}"
    assert r.model == "openai/gpt-4o-mini"
    assert r.timeout == 3.0 and r.fallback == "all"


def test_llm_strategy_without_api_key_rejected():
    raw = _routed_raw("llm")
    del raw["composites"][0]["router"]["api_key"]
    with pytest.raises(cl.ConfigError, match="table with an api_key"):
        cl.GatewayConfig.model_validate(raw)
    del raw["composites"][0]["router"]
    with pytest.raises(cl.ConfigError, match="api_key"):
        cl.GatewayConfig.model_validate(raw)


def test_keyword_strategy_without_any_patterns_rejected():
    raw = _routed_raw()
    for m in raw["composites"][0]["members"]:
        m.pop("route_patterns", None)
    with pytest.raises(cl.ConfigError, match="route_patterns on at least one member"):
        cl.GatewayConfig.model_validate(raw)


def test_invalid_route_pattern_rejected():
    raw = _routed_raw()
    raw["composites"][0]["members"][0]["route_patterns"] = ["[unclosed"]
    with pytest.raises(cl.ConfigError, match="invalid route_pattern"):
        cl.GatewayConfig.model_validate(raw)


def test_unknown_fallback_label_rejected():
    raw = _routed_raw(fallback="ghost")
    with pytest.raises(cl.ConfigError, match="matches no member label"):
        cl.GatewayConfig.model_validate(raw)


def test_unknown_strategy_rejected():
    raw = _base_raw()
    raw["composites"][0]["strategy"] = "vibes"
    with pytest.raises(Exception, match="vibes"):
        cl.GatewayConfig.model_validate(raw)


# --- smart routing (#21): keyword strategy ------------------------------------


def _routed_comp(strategy: str = "keyword", **router) -> cl.Composite:
    return _comp(cl.GatewayConfig.model_validate(_routed_raw(strategy, **router)))


def test_keyword_routes_matching_member_only():
    comp = _routed_comp()
    merged = anyio.run(
        composite.run_composite,
        comp,
        {"query": "python regexes explained"},
        _registry(),
        log,
    )
    assert "exa results for" in merged
    assert "tavily" not in merged


def test_keyword_match_is_case_insensitive_and_can_pick_several():
    comp = _routed_comp()
    selected = anyio.run(composite.select_members, comp, {"query": "CODE in the NEWS"})
    assert selected == comp.members  # both matched


def test_keyword_no_match_falls_back_to_all():
    comp = _routed_comp()
    selected = anyio.run(composite.select_members, comp, {"query": "gardening tips"})
    assert selected == comp.members


def test_keyword_no_match_falls_back_to_designated_member():
    comp = _routed_comp(fallback="tavily")
    selected = anyio.run(composite.select_members, comp, {"query": "gardening tips"})
    assert [composite.member_label(m) for m in selected] == ["tavily"]


# --- smart routing (#21): llm strategy (HTTP layer always mocked) -------------


def _llm_ctx() -> composite.RouteContext:
    return composite.RouteContext(api_key="sk-or-test", log=log)


def test_llm_routes_the_chosen_subset(monkeypatch):
    """Full HTTP layer via httpx.MockTransport: URL, auth header, payload,
    and the reply's member subset all verified — OpenRouter never called."""
    comp = _routed_comp("llm")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '["tavily"]'}}]},
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        composite.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    selected = anyio.run(composite.select_members, comp, {"query": "q"}, _llm_ctx())
    assert [composite.member_label(m) for m in selected] == ["tavily"]
    assert seen["url"] == composite.OPENROUTER_URL
    assert seen["auth"] == "Bearer sk-or-test"
    assert seen["payload"]["model"] == "openai/gpt-4o-mini"
    prompt = seen["payload"]["messages"][0]["content"]
    assert "use for news and current events" in prompt  # route_description
    assert '"q"' in prompt  # call args reach the router


def test_llm_prompt_carries_conditions_text(monkeypatch):
    comp = _routed_comp("llm", conditions="Prefer the cheapest single member.")
    assert "Prefer the cheapest single member." in composite._router_prompt(
        comp, {"query": "q"}
    )


def test_llm_timeout_falls_back_to_all(monkeypatch):
    comp = _routed_comp("llm", timeout=0.05)

    async def slow(router, api_key, prompt):
        import asyncio

        await asyncio.sleep(1.0)
        return "[]"

    monkeypatch.setattr(composite, "_post_router", slow)
    selected = anyio.run(composite.select_members, comp, {"query": "q"}, _llm_ctx())
    assert selected == comp.members


def test_llm_http_error_falls_back(monkeypatch):
    comp = _routed_comp("llm")

    async def boom(router, api_key, prompt):
        raise httpx.HTTPStatusError(
            "502",
            request=httpx.Request("POST", composite.OPENROUTER_URL),
            response=httpx.Response(502),
        )

    monkeypatch.setattr(composite, "_post_router", boom)
    selected = anyio.run(composite.select_members, comp, {"query": "q"}, _llm_ctx())
    assert selected == comp.members


@pytest.mark.parametrize(
    "garbage",
    [
        "sure, I'd route this to tavily!",  # no JSON array at all
        '{"member": "tavily"}',  # JSON but not an array
        "[1, 2, 3]",  # array, wrong element type
        '["ghost-member"]',  # valid shape, unknown label
        "[unterminated",  # broken JSON
    ],
)
def test_llm_garbage_reply_falls_back(monkeypatch, garbage):
    comp = _routed_comp("llm")

    async def reply(router, api_key, prompt):
        return garbage

    monkeypatch.setattr(composite, "_post_router", reply)
    selected = anyio.run(composite.select_members, comp, {"query": "q"}, _llm_ctx())
    assert selected == comp.members


def test_llm_failure_honors_designated_fallback(monkeypatch):
    comp = _routed_comp("llm", fallback="tavily")

    async def reply(router, api_key, prompt):
        return "no idea"

    monkeypatch.setattr(composite, "_post_router", reply)
    selected = anyio.run(composite.select_members, comp, {"query": "q"}, _llm_ctx())
    assert [composite.member_label(m) for m in selected] == ["tavily"]


def test_llm_reply_with_prose_around_array_parses():
    labels = composite._parse_router_reply('Routing:\n```json\n["a", "b"]\n```')
    assert labels == ["a", "b"]


def test_build_tool_resolves_api_key_at_boot(monkeypatch):
    """The ${ENV} ref resolves ONCE in build_composite_tool (boot), and the
    resolved key reaches the router request — end-to-end through the tool."""
    monkeypatch.setenv("OPENROUTER_KEY", "sk-or-resolved")
    comp = _routed_comp("llm")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '["exa/search_web"]'}}]}
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        composite.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    tool = composite.build_composite_tool(comp, _registry(), log)

    async def go():
        server = FastMCP(name="t")
        server.add_tool(tool)
        async with Client(server) as c:
            res = await c.call_tool("web_search", {"query": "q"})
            return res.content[0].text

    merged = anyio.run(go)
    assert seen["auth"] == "Bearer sk-or-resolved"
    assert "exa results for q" in merged
    assert "tavily" not in merged


def test_build_tool_missing_secret_fails_loudly(monkeypatch):
    monkeypatch.delenv("OPENROUTER_KEY", raising=False)
    monkeypatch.setenv("MCP_GATEWAY_SECRETS", "/nonexistent/secrets.env")
    comp = _routed_comp("llm")
    with pytest.raises(cl.ConfigError, match="OPENROUTER_KEY"):
        composite.build_composite_tool(comp, {}, log)


# --- tool/server build -------------------------------------------------------


def test_built_tool_schema_matches_config():
    comp = _comp(_cfg())
    tool = composite.build_composite_tool(comp, {}, log)
    schema = tool.parameters
    assert schema["required"] == ["query"]
    assert schema["properties"]["query"]["description"] == "the query"
    assert schema["properties"]["limit"] == {"default": 5, "type": "integer"}


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
