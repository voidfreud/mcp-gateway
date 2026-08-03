"""Focused contract tests for first-class Virtual Tools."""

from __future__ import annotations

import anyio
import pytest
import structlog
from fastmcp import Client, FastMCP
from fastmcp.tools import ToolResult
from jsonschema import Draft202012Validator
from mcp.types import ImageContent, TextContent
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_gateway import admin
from mcp_gateway import config_loader as cl
from mcp_gateway import virtual_tools as vt

log = structlog.get_logger("test-virtual-tools")


def _raw_virtual(**changes) -> dict:
    tool = {
        "name": "fanout",
        "description": "Fan out a request.",
        "enabled": True,
        "inputs": [{"name": "query", "type": "string"}],
        "members": [
            {
                "backend_id": "backend-a",
                "tool_original": "search",
                "label": "alpha",
                "args": {"query": "query"},
            },
            {
                "backend_id": "backend-b",
                "tool_original": "search",
                "label": "beta",
                "args": {"query": "query"},
            },
        ],
    }
    tool.update(changes)
    return tool


def _cfg(tool: dict | None = None) -> cl.GatewayConfig:
    return cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "id": "backend-a",
                    "name": "a",
                    "transport": "http",
                    "url": "http://a/mcp",
                },
                {
                    "id": "backend-b",
                    "name": "b",
                    "transport": "http",
                    "url": "http://b/mcp",
                },
            ],
            "virtual_tools": [tool or _raw_virtual()],
        }
    )


def _confirm_llm_egress(raw: dict) -> dict:
    """Attach the explicit consent receipt required before an LLM tool is live."""

    raw["enabled"] = False
    draft = _cfg(raw).virtual_tools[0]
    raw["router"]["egress_consent_fingerprint"] = cl.llm_egress_consent_fingerprint(
        draft
    )
    raw["enabled"] = True
    return raw


def test_config_roundtrip_preserves_stable_bindings():
    import tomllib

    cfg = _cfg()
    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
    assert reparsed == cfg
    assert reparsed.virtual_tools[0].members[0].backend_id == "backend-a"


def _tool_with_input(input_config: dict) -> cl.VirtualTool:
    return cl.VirtualTool.model_validate(
        {
            "name": "schema_contract",
            "description": "Exercise one public input schema.",
            "inputs": [input_config],
            "members": [{"backend_id": "backend-a", "tool_original": "search"}],
        }
    )


@pytest.mark.parametrize(
    "input_config",
    [
        {"name": "value", "type": "string"},
        {"name": "value", "type": "string", "required": False},
        {"name": "value", "type": "string", "required": False, "default": "x"},
        {"name": "value", "type": "integer", "required": False, "default": 2},
        {"name": "value", "type": "number", "required": False, "default": 2.5},
        {"name": "value", "type": "boolean", "required": False, "default": False},
    ],
)
def test_virtual_input_schemas_validate_against_draft_2020_12(input_config):
    tool = _tool_with_input(input_config)
    schema = vt.input_schema(tool)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator.check_schema(vt.VIRTUAL_OUTPUT_SCHEMA)

    validator = Draft202012Validator(schema)
    assert validator.is_valid({}) is not input_config.get("required", True)
    assert not validator.is_valid({"value": None})
    property_schema = schema["properties"]["value"]
    if "default" in property_schema:
        assert Draft202012Validator(property_schema).is_valid(
            property_schema["default"]
        )


def test_optional_virtual_input_schema_matches_runtime_null_rejection():
    tool = _tool_with_input({"name": "value", "type": "string", "required": False})
    assert vt.input_schema(tool)["properties"]["value"] == {"type": "string"}
    with pytest.raises(ValueError, match="must be string"):
        vt._validate_arguments(tool, {"value": None})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_virtual_defaults_and_static_arguments_are_rejected(value):
    with pytest.raises(cl.ConfigError, match="finite"):
        cl.VirtualInput(name="value", type="number", required=False, default=value)
    with pytest.raises(cl.ConfigError, match="finite"):
        cl.ParamOverride(original="value", default=value)
    with pytest.raises(cl.ConfigError, match="finite"):
        cl.VirtualMember(
            backend_id="backend-a",
            tool_original="search",
            static_args={"value": value},
        )


def test_backend_name_virtual_is_permanently_reserved():
    with pytest.raises(cl.ConfigError, match="reserved"):
        cl.GatewayConfig.model_validate(
            {
                "backends": [
                    {
                        "name": "virtual",
                        "transport": "http",
                        "url": "http://x/mcp",
                    }
                ]
            }
        )


def test_llm_router_requires_env_reference_and_egress_acknowledgement():
    raw = _raw_virtual(
        dispatch="llm",
        router={"api_key": "secret", "egress_acknowledged": True},
    )
    with pytest.raises(cl.ConfigError, match="ENV_VAR"):
        _cfg(raw)
    raw["router"] = {"api_key": "${ROUTER_KEY}"}
    with pytest.raises(cl.ConfigError, match="egress acknowledgement"):
        _cfg(raw)
    raw["router"]["egress_acknowledged"] = True
    raw["enabled"] = True
    with pytest.raises(cl.ConfigError, match="consent fingerprint"):
        _cfg(raw)


def test_active_llm_consent_fingerprint_is_persisted_and_binds_egress_contract():
    import tomllib

    raw = _confirm_llm_egress(
        _raw_virtual(
            dispatch="llm",
            router={
                "api_key": "${ROUTER_KEY}",
                "model": "openai/gpt-4o-mini",
                "conditions": "Prefer the geographically closest source.",
                "egress_acknowledged": True,
            },
        )
    )
    cfg = _cfg(raw)
    router = cfg.virtual_tools[0].router
    assert router is not None
    assert (
        cl.to_raw(cfg)["virtual_tools"][0]["router"]["egress_consent_fingerprint"]
        == router.egress_consent_fingerprint
    )
    assert cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg))) == cfg

    changed = _raw_virtual(
        dispatch="llm",
        router=dict(raw["router"]),
        inputs=[{"name": "query", "type": "string", "description": "private"}],
    )
    with pytest.raises(cl.ConfigError, match="consent fingerprint"):
        _cfg(changed)


@pytest.mark.parametrize(
    "pattern, message",
    [
        ("x" * (cl.MAX_VIRTUAL_ROUTE_PATTERN_CHARS + 1), "exceeds"),
        ("(?=python)python", "lookarounds"),
        (r"(python)\1", "backreferences"),
        (r"(python+)+", "nested quantifiers"),
    ],
)
def test_keyword_patterns_reject_unsafe_or_unbounded_forms(pattern, message):
    raw = _raw_virtual()
    raw["members"][0]["route_patterns"] = [pattern]
    with pytest.raises(cl.ConfigError, match=message):
        _cfg(raw)


def test_keyword_patterns_and_routing_input_caps_are_bounded():
    raw = _raw_virtual(routing_input_max_chars=4096)
    raw["members"][0]["route_patterns"] = ["python|code", r"docs?$"]
    assert _cfg(raw).virtual_tools[0].routing_input_max_chars == 4096
    assert cl.routing_input_text({"query": "a" * 20}, 20) == "a" * 20
    with pytest.raises(ValueError, match="20-character"):
        cl.routing_input_text({"query": "a" * 21}, 20)
    with pytest.raises(ValueError, match="greater than or equal"):
        _cfg(_raw_virtual(routing_input_max_chars=63))


def test_original_bindings_resolve_through_current_effective_names():
    cfg = _cfg()
    cfg.backends[0].tools = [
        cl.ToolOverride(
            original="search",
            name="web_search",
            params=[cl.ParamOverride(original="query", name="question")],
        )
    ]
    backend, tool, params = vt.resolve_member(cfg.virtual_tools[0].members[0], cfg)
    assert backend.name == "a"
    assert tool == "web_search"
    assert params == {"query": "question"}


def test_hidden_bound_parameter_is_unresolved():
    cfg = _cfg()
    cfg.backends[0].tools = [
        cl.ToolOverride(
            original="search",
            params=[cl.ParamOverride(original="query", hide=True, default="fixed")],
        )
    ]
    with pytest.raises(cl.ConfigError, match="hidden parameter"):
        vt.resolve_member(cfg.virtual_tools[0].members[0], cfg)


def test_keyword_dispatch_selects_hits_and_explicit_fallback():
    raw = _raw_virtual(
        dispatch="keyword",
        router={"fallback": "beta"},
    )
    raw["members"][0]["route_patterns"] = ["python|code"]
    raw["members"][1]["route_patterns"] = ["weather"]
    tool = _cfg(raw).virtual_tools[0]
    selected = anyio.run(vt.select_members, tool, {"query": "Python help"}, log)
    assert [vt.member_label(member) for member in selected] == ["alpha"]
    fallback = anyio.run(vt.select_members, tool, {"query": "unmatched"}, log)
    assert [vt.member_label(member) for member in fallback] == ["beta"]


def test_llm_router_failure_uses_local_fallback(monkeypatch):
    raw = _confirm_llm_egress(
        _raw_virtual(
            dispatch="llm",
            router={
                "api_key": "${ROUTER_KEY}",
                "egress_acknowledged": True,
                "fallback": "alpha",
            },
        )
    )
    tool = _cfg(raw).virtual_tools[0]

    class BrokenClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            raise RuntimeError("router unavailable")

    monkeypatch.setattr(vt.httpx, "AsyncClient", BrokenClient)
    monkeypatch.setattr(vt.cl, "expand_env", lambda _value: "resolved-secret")
    selected = anyio.run(vt.select_members, tool, {"query": "anything"}, log)
    assert [vt.member_label(member) for member in selected] == ["alpha"]


def test_aggregate_preserves_rich_blocks_and_structured_content():
    tool = _cfg().virtual_tools[0]
    result = vt.aggregate_results(
        tool,
        [
            {
                "member": "alpha",
                "status": "ok",
                "ms": 1.2,
                "content": [
                    TextContent(type="text", text="hello"),
                    ImageContent(type="image", data="aGVsbG8=", mimeType="image/png"),
                ],
                "structured": {"answer": 42},
            }
        ],
        ["alpha"],
    )
    assert [block.type for block in result.content] == ["text", "text", "image"]
    assert result.structured_content["members"][0]["result"] == {"answer": 42}
    assert result.is_error is False


def test_virtual_tool_advertises_envelope_schema_and_preserves_member_metadata():
    source = FastMCP("source")

    @source.tool(name="search")
    def search(query: str) -> ToolResult:
        return ToolResult(
            content=[TextContent(type="text", text=f"found {query}")],
            structured_content={"answer": query},
            meta={"source": "catalog-a", "trace_id": "trace-123"},
        )

    raw = _raw_virtual(
        members=[
            {
                "backend_id": "backend-a",
                "tool_original": "search",
                "label": "alpha",
                "args": {"query": "query"},
            }
        ]
    )
    cfg = _cfg(raw)
    server = vt.build_virtual_server(cfg, cfg, {"a": source}, log)

    async def exercise():
        async with Client(server) as client:
            listed = await client.list_tools()
            result = await client.call_tool("fanout", {"query": "needles"})
            return listed, result

    listed, result = anyio.run(exercise)
    advertised = next(item for item in listed if item.name == "fanout")
    assert advertised.outputSchema == vt.VIRTUAL_OUTPUT_SCHEMA
    member = result.structured_content["members"][0]
    assert member["result"] == {"answer": "needles"}
    assert member["meta"] == {"source": "catalog-a", "trace_id": "trace-123"}
    assert result.meta is not None
    assert "mcp-gateway/virtual" in result.meta


def test_aggregate_omits_oversize_member_metadata_within_strict_budget():
    tool = _cfg(_raw_virtual(max_result_bytes=1024)).virtual_tools[0]
    result = vt.aggregate_results(
        tool,
        [
            {
                "member": "alpha",
                "status": "ok",
                "content": [],
                "meta": {"large": "z" * 8_000},
            }
        ],
        ["alpha"],
    )
    assert vt._json_size(result) <= tool.max_result_bytes
    assert result.structured_content["members"][0].get("meta") is None
    omitted = result.structured_content["budget"]["omitted"]
    assert {item["kind"] for item in omitted} == {"meta"}


def test_aggregate_budget_omission_is_explicit():
    raw = _raw_virtual(max_result_bytes=1024)
    tool = _cfg(raw).virtual_tools[0]
    result = vt.aggregate_results(
        tool,
        [
            {
                "member": "alpha",
                "status": "ok",
                "content": [TextContent(type="text", text="x" * 5000)],
                "structured": {"large": "y" * 5000},
            }
        ],
        ["alpha"],
    )
    assert "output budget 1024 bytes" in result.content[-1].text
    omitted = result.structured_content["budget"]["omitted"]
    assert {item["kind"] for item in omitted} == {"content", "structured"}


def test_public_hyphenated_input_works_without_python_signature_aliasing():
    backend = FastMCP("source")

    @backend.tool(name="search")
    def search(query: str) -> str:
        return f"found {query}"

    raw = _raw_virtual(
        name="hyphen-tool",
        inputs=[{"name": "query-text", "type": "string"}],
        members=[
            {
                "backend_id": "backend-a",
                "tool_original": "search",
                "args": {"query": "query-text"},
            }
        ],
    )
    cfg = _cfg(raw)
    server = vt.build_virtual_server(cfg, cfg, {"a": backend}, log)

    async def exercise():
        async with Client(server) as client:
            listed = await client.list_tools()
            result = await client.call_tool(
                "hyphen-tool", {"query-text": "needles"}, raise_on_error=False
            )
            return listed, result

    listed, result = anyio.run(exercise)
    assert listed[0].inputSchema["properties"] == {"query-text": {"type": "string"}}
    assert result.is_error is False
    assert "found needles" in "\n".join(
        block.text for block in result.content if block.type == "text"
    )


def test_strict_failure_policy_marks_partial_result_as_error():
    tool = _cfg(_raw_virtual(failure_policy="strict")).virtual_tools[0]
    result = vt.aggregate_results(
        tool,
        [
            {"member": "alpha", "status": "ok", "content": []},
            {"member": "beta", "status": "error", "error": "boom"},
        ],
        ["alpha", "beta"],
    )
    assert result.is_error is True


def _admin_fixture(tmp_path):
    source = FastMCP("source")

    @source.tool(name="search")
    def search(query: str) -> str:
        return f"result {query}"

    cfg = _cfg()
    cfg.virtual_tools = []
    path = tmp_path / "config.toml"
    cl.save(cfg, path)
    registry = {"a": source, "b": source}
    server = vt.build_virtual_server(cfg, lambda: cl.load(path), registry, log)
    hooks = {"virtual_server": server}
    app = Starlette()
    admin.register(app, str(path), log, registry, {}, hooks)
    return TestClient(app), path, server


def test_admin_draft_test_activate_and_remove_integrity(tmp_path):
    client, path, server = _admin_fixture(tmp_path)
    payload = _raw_virtual()
    created = client.post("/admin/api/virtual-tools", json=payload)
    assert created.status_code == 201
    assert cl.load(path).virtual_tools[0].enabled is False
    tested = client.post(
        "/admin/api/virtual-tools/fanout/test", json={"arguments": {"query": "q"}}
    )
    assert tested.status_code == 200 and tested.json()["ok"] is True
    activated = client.post("/admin/api/virtual-tools/fanout/activate")
    assert activated.status_code == 200
    assert cl.load(path).virtual_tools[0].enabled is True

    async def names():
        async with Client(server) as mcp:
            return [tool.name for tool in await mcp.list_tools()]

    assert anyio.run(names) == ["fanout"]
    blocked = client.delete("/admin/api/backend/a")
    assert blocked.status_code == 400
    assert "referenced" in blocked.json()["error"]


def test_failed_activation_does_not_persist_enabled_state(tmp_path):
    client, path, _server = _admin_fixture(tmp_path)
    payload = _raw_virtual()
    payload["members"][0]["tool_original"] = "missing"
    assert client.post("/admin/api/virtual-tools", json=payload).status_code == 201
    response = client.post("/admin/api/virtual-tools/fanout/activate")
    assert response.status_code == 400
    assert cl.load(path).virtual_tools[0].enabled is False


def test_consent_fingerprint_is_server_derived_at_activation(tmp_path):
    client, path, _server = _admin_fixture(tmp_path)
    payload = _raw_virtual(
        dispatch="llm",
        router={
            "model": "openai/gpt-4o-mini",
            "api_key": "${ROUTER_KEY}",
            "egress_acknowledged": True,
            "egress_consent_fingerprint": "a" * 64,
        },
        egress_consent_fingerprint="a" * 64,
    )
    assert client.post("/admin/api/virtual-tools", json=payload).status_code == 201
    assert client.post("/admin/api/virtual-tools/fanout/activate").status_code == 200
    listed = client.get("/admin/api/virtual-tools").json()["tools"][0]
    fingerprint = listed["consent_fingerprint"]
    assert fingerprint.startswith("sha256:") and len(fingerprint) == 71
    assert fingerprint != "a" * 64
    saved_router = cl.to_raw(cl.load(path))["virtual_tools"][0]["router"]
    assert saved_router["egress_consent_fingerprint"] == fingerprint


def test_put_of_active_tool_saves_disabled_draft_and_removes_live_tool(tmp_path):
    client, path, server = _admin_fixture(tmp_path)
    payload = _raw_virtual()
    assert client.post("/admin/api/virtual-tools", json=payload).status_code == 201
    assert client.post("/admin/api/virtual-tools/fanout/activate").status_code == 200
    broken = _raw_virtual()
    broken["members"][0]["args"] = {"missing_param": "query"}
    response = client.put("/admin/api/virtual-tools/fanout", json=broken)
    assert response.status_code == 200
    saved = cl.load(path).virtual_tools[0]
    assert saved.enabled is False
    assert saved.members[0].args == {"missing_param": "query"}

    async def names():
        async with Client(server) as mcp:
            return [tool.name for tool in await mcp.list_tools()]

    assert anyio.run(names) == []
    assert client.post("/admin/api/virtual-tools/fanout/activate").status_code == 400


def test_legacy_mutations_cannot_strand_an_active_virtual_tool(tmp_path, monkeypatch):
    defaults_dir = tmp_path / "defaults"
    monkeypatch.setattr(admin, "DEFAULTS_DIR", defaults_dir)
    for backend in ("a", "b"):
        admin.save_defaults(
            {
                "backend": backend,
                "tools": [
                    {
                        "original": "search",
                        "description": "Search",
                        "params": [{"original": "query", "required": True}],
                    }
                ],
            }
        )
    client, path, _server = _admin_fixture(tmp_path)
    created = client.post("/admin/api/virtual-tools", json=_raw_virtual())
    assert created.status_code == 201
    assert client.post("/admin/api/virtual-tools/fanout/activate").status_code == 200

    disabled_tool = client.put(
        "/admin/api/override",
        json={
            "backend": "a",
            "tool_original": "search",
            "override": {"enabled": False},
        },
    )
    assert disabled_tool.status_code == 400
    assert "active Virtual Tool" in disabled_tool.json()["error"]

    hidden_param = client.put(
        "/admin/api/override",
        json={
            "backend": "a",
            "tool_original": "search",
            "override": {
                "params": [{"original": "query", "hide": True, "default": "fixed"}],
            },
        },
    )
    assert hidden_param.status_code == 400
    assert "cannot hide" in hidden_param.json()["error"]

    disabled_backend = client.post(
        "/admin/api/backend/a/enabled", json={"value": False}
    )
    assert disabled_backend.status_code == 400
    assert cl.load(path).backends[0].enabled is True


def test_keyword_routing_text_is_capped_before_regex_evaluation():
    raw = _raw_virtual(
        dispatch="keyword", router={"fallback": "beta"}, routing_input_max_chars=8192
    )
    raw["members"][0]["route_patterns"] = ["needle"]
    raw["members"][1]["route_patterns"] = ["weather"]
    tool = _cfg(raw).virtual_tools[0]
    arguments = {"query": "x" * 9_000 + "needle"}
    assert len(vt._route_text(tool, arguments)) == tool.routing_input_max_chars
    selected = anyio.run(vt.select_members, tool, arguments, log)
    assert [vt.member_label(member) for member in selected] == ["beta"]


def test_aggregate_strictly_bounds_final_serialized_tool_result():
    tool = _cfg(_raw_virtual(max_result_bytes=1024)).virtual_tools[0]
    result = vt.aggregate_results(
        tool,
        [
            {
                "member": "alpha" * 100,
                "status": "error",
                "error": "boom" * 3_000,
                "content": [TextContent(type="text", text="x" * 8_000)],
                "structured": {"large": "y" * 8_000},
            }
        ],
        ["alpha" * 100],
    )
    assert vt._json_size(result) <= tool.max_result_bytes
    assert "omitted" in result.content[-1].text
    budget = result.structured_content["budget"]
    assert budget["omitted_count"] >= 1
    assert len(budget["omitted"]) <= vt._OMISSION_DETAIL_LIMIT
    assert result.meta["mcp-gateway/virtual"] == budget


def test_replace_tools_stages_without_mutating_old_provider(monkeypatch):
    old_cfg = _cfg()
    server = vt.build_virtual_server(old_cfg, old_cfg, {}, log)
    old_components = server.local_provider._components
    second = _raw_virtual(name="second")
    candidate = cl.GatewayConfig.model_validate(
        old_cfg.model_copy(
            update={
                "virtual_tools": [
                    old_cfg.virtual_tools[0],
                    cl.VirtualTool.model_validate(second),
                ]
            },
            deep=True,
        ).model_dump()
    )
    original = vt.build_virtual_tool

    def fail_second(tool, *args, **kwargs):
        if tool.name == "second":
            raise RuntimeError("staging failed")
        return original(tool, *args, **kwargs)

    monkeypatch.setattr(vt, "build_virtual_tool", fail_second)
    with pytest.raises(RuntimeError, match="staging failed"):
        vt.replace_tools(server, candidate, candidate, {}, log)
    assert server.local_provider._components is old_components
    assert [component.name for component in old_components.values()] == ["fanout"]
