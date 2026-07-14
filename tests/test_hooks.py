"""Per-tool behavior hooks (#16): loading, the validate/post_process contract,
composition with renames + hidden params, fail-closed load errors, config
round-trip, and the admin surface."""

import anyio
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from mcp_gateway import admin
from mcp_gateway import config_loader as cl
from mcp_gateway import hooks as hooks_mod

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def hooks_dir(tmp_path, monkeypatch):
    """A throwaway hooks dir, selected via MCP_GATEWAY_HOOKS; module cache
    reset so one test's import can't leak into another."""
    d = tmp_path / "hooks"
    d.mkdir()
    monkeypatch.setenv("MCP_GATEWAY_HOOKS", str(d))
    monkeypatch.setattr(hooks_mod, "_module_cache", {})
    return d


GOOD_HOOKS = """
def check(args):
    if args.get("query") == "bad":
        raise ValueError("query must not be 'bad'")

async def check_async(args):
    if args.get("query") == "bad":
        raise ValueError("async says no")

def trim(result):
    for c in result.content:
        if getattr(c, "text", None):
            c.text = c.text[:5]
    return result

async def trim_async(result):
    return trim(result)

not_callable = 42
"""


def _write(hooks_dir, name="myhooks", body=GOOD_HOOKS):
    (hooks_dir / f"{name}.py").write_text(body)


def _cfg(tool_override: dict) -> cl.GatewayConfig:
    return cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "stdio",
                    "command": "/bin/x",
                    "tools": [tool_override],
                }
            ]
        }
    )


def _server():
    m = FastMCP("b")

    @m.tool
    def search(query: str, secret: str = "") -> str:
        """Search things."""
        return f"query={query} secret={secret} tail"

    return m


def _call(server, tool, args):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(tool, args)

    return anyio.run(go)


def _transformed_server(tool_override: dict):
    cfg = _cfg(tool_override)
    transforms, _ = cl.build_transforms(cfg, cfg.backends[0])
    server = _server()
    server.add_transform(transforms)
    return server


# ---------------------------------------------------------------------------
# hooks dir resolution + loading
# ---------------------------------------------------------------------------


def test_hooks_dir_env_override(hooks_dir):
    assert hooks_mod.hooks_dir() == hooks_dir


def test_hooks_dir_precedence(tmp_path, monkeypatch):
    # no env: a repo-local ./hooks dir wins; else the XDG-style default
    monkeypatch.delenv("MCP_GATEWAY_HOOKS", raising=False)
    monkeypatch.chdir(tmp_path)
    assert hooks_mod.hooks_dir() == (
        hooks_mod.Path(hooks_mod.DEFAULT_HOOKS_DIR).expanduser()
    )
    (tmp_path / "hooks").mkdir()
    assert hooks_mod.hooks_dir() == hooks_mod.Path("hooks")


def test_load_hook_good(hooks_dir):
    _write(hooks_dir)
    assert callable(hooks_mod.load_hook("myhooks:check"))
    assert callable(hooks_mod.load_hook("myhooks.py:check"))  # .py tolerated
    assert callable(hooks_mod.load_hook("myhooks:check_async"))


@pytest.mark.parametrize(
    "spec",
    ["", "nofunc", "../evil:f", "a/b:f", "mod:", ":f", "mod:f:g", "mod:f()"],
)
def test_load_hook_rejects_malformed_specs(hooks_dir, spec):
    with pytest.raises(hooks_mod.HookError, match="invalid hook spec"):
        hooks_mod.load_hook(spec)


def test_load_hook_missing_module(hooks_dir):
    with pytest.raises(hooks_mod.HookError, match="not found"):
        hooks_mod.load_hook("nope:f")


def test_load_hook_missing_function(hooks_dir):
    _write(hooks_dir)
    with pytest.raises(hooks_mod.HookError, match="no function"):
        hooks_mod.load_hook("myhooks:absent")


def test_load_hook_non_callable_attr(hooks_dir):
    _write(hooks_dir)
    with pytest.raises(hooks_mod.HookError, match="no function"):
        hooks_mod.load_hook("myhooks:not_callable")


def test_load_hook_import_error(hooks_dir):
    _write(hooks_dir, "broken", "raise RuntimeError('boom at import')\n")
    with pytest.raises(hooks_mod.HookError, match="failed to import"):
        hooks_mod.load_hook("broken:f")


def test_load_hook_picks_up_edited_file(hooks_dir):
    import os

    path = hooks_dir / "myhooks.py"
    _write(hooks_dir)
    hooks_mod.load_hook("myhooks:check")
    path.write_text(GOOD_HOOKS + "\ndef extra(args):\n    return None\n")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    assert callable(hooks_mod.load_hook("myhooks:extra"))


# ---------------------------------------------------------------------------
# config: spec validation + TOML round-trip
# ---------------------------------------------------------------------------


def test_config_rejects_malformed_hook_spec():
    with pytest.raises(Exception, match="invalid hook spec"):
        _cfg({"original": "search", "validate": "../evil:f"})


def test_config_toml_round_trip(tmp_path):
    cfg = _cfg(
        {
            "original": "search",
            "validate": "myhooks:check",
            "post_process": "myhooks:trim",
        }
    )
    text = cl.dump_toml(cfg)
    assert 'validate = "myhooks:check"' in text
    assert 'post_process = "myhooks:trim"' in text
    path = tmp_path / "config.toml"
    cl.save(cfg, path)
    loaded = cl.load(path)
    t = loaded.backends[0].tools[0]
    assert t.validate_ == "myhooks:check"
    assert t.post_process == "myhooks:trim"


def test_config_without_hooks_serializes_no_hook_keys():
    cfg = _cfg({"original": "search", "name": "find"})
    text = cl.dump_toml(cfg)
    assert "validate" not in text and "post_process" not in text


# ---------------------------------------------------------------------------
# live behavior through a transformed FastMCP server
# ---------------------------------------------------------------------------


def test_validate_rejection_surfaces_as_tool_error(hooks_dir):
    _write(hooks_dir)
    server = _transformed_server({"original": "search", "validate": "myhooks:check"})
    with pytest.raises(ToolError, match="query must not be 'bad'"):
        _call(server, "search", {"query": "bad"})


def test_validate_passes_clean_calls_through(hooks_dir):
    _write(hooks_dir)
    server = _transformed_server({"original": "search", "validate": "myhooks:check"})
    r = _call(server, "search", {"query": "ok"})
    assert r.content[0].text == "query=ok secret= tail"


def test_post_process_transforms_result(hooks_dir):
    _write(hooks_dir)
    server = _transformed_server({"original": "search", "post_process": "myhooks:trim"})
    r = _call(server, "search", {"query": "hello"})
    assert r.content[0].text == "query"  # trimmed to 5 chars


def test_async_hook_variants(hooks_dir):
    _write(hooks_dir)
    server = _transformed_server(
        {
            "original": "search",
            "validate": "myhooks:check_async",
            "post_process": "myhooks:trim_async",
        }
    )
    r = _call(server, "search", {"query": "hello"})
    assert r.content[0].text == "query"
    with pytest.raises(ToolError, match="async says no"):
        _call(server, "search", {"query": "bad"})


def test_hooks_compose_with_rename_and_hidden_param(hooks_dir):
    """validate sees EXPOSED names (post-rename, hidden absent); the backend
    still receives original names plus the injected hidden default."""
    _write(
        hooks_dir,
        body=(
            "seen = {}\n"
            "def check(args):\n"
            "    seen.update(args)\n"
            "    if 'query' in args or 'secret' in args:\n"
            "        raise ValueError('saw backend-side names')\n"
            "    if args.get('q') == 'bad':\n"
            "        raise ValueError('nope')\n"
        ),
    )
    server = _transformed_server(
        {
            "original": "search",
            "validate": "myhooks:check",
            "params": [
                {"original": "query", "name": "q"},
                {"original": "secret", "hide": True, "default": "s3"},
            ],
        }
    )
    r = _call(server, "search", {"q": "hello"})
    # backend got original names + the injected hidden value
    assert r.content[0].text == "query=hello secret=s3 tail"
    with pytest.raises(ToolError, match="nope"):
        _call(server, "search", {"q": "bad"})


def test_hooked_tool_keeps_broadcast_schema(hooks_dir):
    """Adding hooks must not change what the tool broadcasts (schema parity
    with a hook-less transform)."""
    _write(hooks_dir)

    def snapshot(override):
        server = _transformed_server(override)

        async def go():
            async with Client(server) as c:
                t = [x for x in await c.list_tools() if x.name == "search"][0]
                return t.inputSchema, t.outputSchema, t.description

        return anyio.run(go)

    plain = snapshot(
        {"original": "search", "params": [{"original": "query", "name": "q"}]}
    )
    hooked = snapshot(
        {
            "original": "search",
            "validate": "myhooks:check",
            "post_process": "myhooks:trim",
            "params": [{"original": "query", "name": "q"}],
        }
    )
    assert hooked == plain


def test_broken_hook_fails_closed_per_tool(hooks_dir):
    """A hook that can't load must not break the transform build (the mount)
    or other tools — but every call to the hooked tool errors loudly."""
    server = _server()  # hooks dir is empty: myhooks.py does not exist

    @server.tool
    def other(x: str) -> str:
        return "other:" + x

    cfg = _cfg({"original": "search", "validate": "myhooks:check"})
    transforms, _ = cl.build_transforms(cfg, cfg.backends[0])  # must not raise
    server.add_transform(transforms)
    with pytest.raises(ToolError, match="behavior hook failed to load"):
        _call(server, "search", {"query": "x"})
    assert _call(server, "other", {"x": "y"}).content[0].text == "other:y"


def test_validate_exception_other_than_valueerror_still_fails_call(hooks_dir):
    _write(hooks_dir, body="def check(args):\n    raise RuntimeError('oops')\n")
    server = _transformed_server({"original": "search", "validate": "myhooks:check"})
    with pytest.raises(Exception):  # noqa: B017 — any tool-level failure is fine
        _call(server, "search", {"query": "x"})


# ---------------------------------------------------------------------------
# admin surface
# ---------------------------------------------------------------------------


def _seed_defaults(monkeypatch, tmp_path):
    d = tmp_path / "defaults"
    monkeypatch.setattr(admin, "DEFAULTS_DIR", d)
    d.mkdir(parents=True, exist_ok=True)
    (d / "b.json").write_text(
        '{"backend": "b", "instructions": null, "tools": '
        '[{"original": "search", "description": "d", "params": []}]}'
    )


def test_admin_save_preserves_hand_authored_hooks(hooks_dir, tmp_path, monkeypatch):
    _seed_defaults(monkeypatch, tmp_path)
    cfg = _cfg(
        {
            "original": "search",
            "validate": "myhooks:check",
            "post_process": "myhooks:trim",
        }
    )
    admin.apply_tool_override(
        cfg,
        "b",
        {"tool_original": "search", "override": {"description": "tuned text"}},
    )
    t = cfg.backends[0].tools[0]
    assert t.description == "tuned text"
    assert t.validate_ == "myhooks:check"
    assert t.post_process == "myhooks:trim"


def test_admin_save_keeps_hook_only_override_entry(hooks_dir, tmp_path, monkeypatch):
    """An override whose ONLY content is hooks must survive a no-diff UI save
    (has_override counts hooks)."""
    _seed_defaults(monkeypatch, tmp_path)
    cfg = _cfg({"original": "search", "validate": "myhooks:check"})
    admin.apply_tool_override(
        cfg, "b", {"tool_original": "search", "override": {"description": ""}}
    )
    assert len(cfg.backends[0].tools) == 1
    assert cfg.backends[0].tools[0].validate_ == "myhooks:check"


def test_state_reports_hooks_and_load_errors(hooks_dir, tmp_path, monkeypatch):
    _seed_defaults(monkeypatch, tmp_path)
    _write(hooks_dir)
    cfg = _cfg(
        {
            "original": "search",
            "validate": "myhooks:check",
            "post_process": "nope:missing",
        }
    )
    tool = admin.build_state(cfg)["backends"][0]["tools"][0]
    assert tool["validate"] == "myhooks:check"
    assert tool["post_process"] == "nope:missing"
    assert "not found" in tool["hook_error"]


def test_state_hook_error_none_when_hooks_load(hooks_dir, tmp_path, monkeypatch):
    _seed_defaults(monkeypatch, tmp_path)
    _write(hooks_dir)
    cfg = _cfg({"original": "search", "validate": "myhooks:check"})
    tool = admin.build_state(cfg)["backends"][0]["tools"][0]
    assert tool["hook_error"] is None
    # and a plain tool reports no hooks at all
    cfg2 = _cfg({"original": "search", "name": "find"})
    tool2 = admin.build_state(cfg2)["backends"][0]["tools"][0]
    assert tool2["validate"] is None and tool2["hook_error"] is None
