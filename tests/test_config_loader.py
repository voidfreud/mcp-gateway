"""Tests for config_loader — TOML round-trip (property-based), env expansion,
name prefixing, and durable save."""

from __future__ import annotations

import os
import string
import tomllib

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from mcp_gateway import config_loader as cl

# --- strategies ------------------------------------------------------------

# Identifiers used for backend/tool/param names (the safe-identifier charset).
ident = st.text(
    alphabet=string.ascii_letters + string.digits + "_-", min_size=1, max_size=15
)

# Free text for titles/descriptions: realistic + adversarial (unicode, quotes,
# backslashes, brackets, '=', '#', and newlines/tabs) but no other control chars.
free_text = st.text(
    alphabet=st.one_of(
        st.characters(exclude_categories=("Cc", "Cs")),
        st.sampled_from("\n\t\"'\\[]=#${}"),
    ),
    max_size=60,
)


@st.composite
def gw_config_dict(draw) -> dict:
    names = draw(
        st.lists(
            ident.filter(lambda name: name not in cl.RESERVED_BACKEND_NAMES),
            min_size=1,
            max_size=4,
            unique=True,
        )
    )
    backends = []
    for nm in names:
        transport = draw(st.sampled_from(["http", "streamable-http", "sse", "stdio"]))
        b: dict = {"name": nm, "transport": transport, "stateless": draw(st.booleans())}
        if draw(st.booleans()):
            b["always_load"] = True
        if draw(st.booleans()):
            b["instructions"] = draw(free_text)
        if transport == "stdio":
            b["command"] = draw(st.sampled_from(["/bin/x", "uvx"]))
            b["args"] = draw(st.lists(ident, max_size=3))
        else:  # http / streamable-http / sse — all url-based
            b["url"] = draw(
                st.sampled_from(["https://h/mcp", "http://127.0.0.1:9/mcp"])
            )
            if draw(st.booleans()):
                b["auth_header"] = "Authorization"
                b["auth_value"] = "Bearer ${T}"
        tools = []
        for to in draw(st.lists(ident, max_size=3, unique=True)):
            t: dict = {"original": to, "enabled": draw(st.booleans())}
            if draw(st.booleans()):
                t["always_load"] = True
            if draw(st.booleans()):  # #162: per-tool output cap
                t["max_result_chars"] = draw(st.integers(1, 10**7))
            if draw(st.booleans()):
                t["name"] = draw(ident)
            if draw(st.booleans()):
                t["title"] = draw(free_text)
            if draw(st.booleans()):
                t["description"] = draw(free_text)
            params = []
            for po in draw(st.lists(ident, max_size=3, unique=True)):
                p: dict = {"original": po, "hide": draw(st.booleans())}
                if draw(st.booleans()):
                    p["name"] = draw(ident)
                if draw(st.booleans()):
                    p["description"] = draw(free_text)
                params.append(p)
            if params:
                t["params"] = params
            tools.append(t)
        if tools:
            b["tools"] = tools
        backends.append(b)
    out: dict = {
        "host": "127.0.0.1",
        "port": draw(st.integers(1, 65535)),
        "log_file": "~/x.log",
        "backends": backends,
    }
    if draw(st.booleans()):  # optional gateway bearer token (#26), stored as a ref
        out["bearer_token"] = "${GW_TOKEN}"
    return out


# --- round-trip property ---------------------------------------------------


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(gw_config_dict())
def test_toml_roundtrip_is_stable(raw):
    """For ANY valid config, dump_toml -> parse -> model preserves to_raw."""
    cfg = cl.GatewayConfig.model_validate(raw)
    toml_str = cl.dump_toml(cfg)
    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(toml_str))
    assert cl.to_raw(cfg) == cl.to_raw(reparsed)


def test_roundtrip_handles_tricky_description():
    raw = {
        "backends": [
            {
                "name": "b",
                "transport": "http",
                "url": "https://h/mcp",
                "tools": [
                    {
                        "original": "t",
                        "description": (
                            'has "quotes", a \\backslash,\nand a newline = ok'
                        ),
                    }
                ],
            }
        ]
    }
    cfg = cl.GatewayConfig.model_validate(raw)
    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
    assert (
        reparsed.backends[0].tools[0].description
        == raw["backends"][0]["tools"][0]["description"]
    )


def test_self_check_main_runs_clean():
    # #80: the documented `python -m mcp_gateway.config_loader <cfg>`
    # self-check must not
    # crash (it used to call build_transforms with a missing arg).
    import pathlib
    import subprocess
    import sys

    repo = pathlib.Path(__file__).resolve().parent.parent
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "mcp_gateway.config_loader",
            "src/mcp_gateway/config.default.toml",
        ],
        cwd=repo,
        check=False,  # returncode asserted below with stderr attached
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "backend(s)" in r.stdout


# --- env expansion ---------------------------------------------------------


def test_expand_env_substitutes(monkeypatch):
    monkeypatch.setenv("MY_TOK", "secret123")
    assert cl.expand_env("Bearer ${MY_TOK}") == "Bearer secret123"


def test_expand_env_missing_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("NOPE_VAR", raising=False)
    monkeypatch.setenv("MCP_GATEWAY_SECRETS", str(tmp_path / "absent.env"))
    with pytest.raises(cl.ConfigError):
        cl.expand_env("${NOPE_VAR}")


def test_expand_env_falls_back_to_secrets_file(monkeypatch, tmp_path):
    monkeypatch.delenv("FILE_TOK", raising=False)
    secrets = tmp_path / "secrets.env"
    secrets.write_text(
        "# gateway secrets\n"
        "\n"
        'export FILE_TOK="from-file"\n'
        "OTHER='single'\n"
        "PLAIN=bare value\n"
        "not a kv line\n"
    )
    monkeypatch.setenv("MCP_GATEWAY_SECRETS", str(secrets))
    assert cl.expand_env("Bearer ${FILE_TOK}") == "Bearer from-file"
    assert cl.load_secrets() == {
        "FILE_TOK": "from-file",
        "OTHER": "single",
        "PLAIN": "bare value",
    }


def test_expand_env_environ_wins_over_secrets_file(monkeypatch, tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("DUP_TOK=file\n")
    monkeypatch.setenv("MCP_GATEWAY_SECRETS", str(secrets))
    monkeypatch.setenv("DUP_TOK", "env")
    assert cl.expand_env("${DUP_TOK}") == "env"


def test_expand_env_secrets_not_leaked_to_environ(monkeypatch, tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("LEAK_TOK=hush\n")
    monkeypatch.setenv("MCP_GATEWAY_SECRETS", str(secrets))
    monkeypatch.delenv("LEAK_TOK", raising=False)
    assert cl.expand_env("${LEAK_TOK}") == "hush"
    assert "LEAK_TOK" not in os.environ


def test_expand_env_required_returns_expanded_value(monkeypatch):
    monkeypatch.setenv("REQ_TOK", "secret123")
    assert cl.expand_env_required("${REQ_TOK}", "bearer_token") == "secret123"


def test_expand_env_required_rejects_empty_expansion(monkeypatch):
    # A configured-but-empty ${VAR} must fail loudly: downstream an empty
    # token is indistinguishable from "no token" and auth is silently off.
    monkeypatch.setenv("EMPTY_TOK", "")
    with pytest.raises(cl.ConfigError, match="expands to an empty string"):
        cl.expand_env_required("${EMPTY_TOK}", "bearer_token")


def test_load_secrets_reloads_on_mtime_change(monkeypatch, tmp_path):
    # #105: cached by (path, mtime) — a fresh edit (newer mtime) is still picked
    # up without a restart, so the cache never serves a stale secret.
    p = tmp_path / "s.env"
    p.write_text("A=one\n")
    monkeypatch.setenv("MCP_GATEWAY_SECRETS", str(p))
    assert cl.load_secrets() == {"A": "one"}
    p.write_text("A=two\nB=three\n")
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 5))  # force a newer mtime
    assert cl.load_secrets() == {"A": "two", "B": "three"}


@pytest.mark.anyio
async def test_tool_transform_preserves_root_schema_definitions():
    from fastmcp.tools.function_tool import FunctionTool

    def lookup(address: dict | None = None) -> str:
        return str(address)

    tool = FunctionTool.from_function(lookup, name="lookup")
    definitions = {
        "address": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
        }
    }
    tool.parameters = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": definitions,
        "type": "object",
        "properties": {"address": {"$ref": "#/$defs/address"}},
        "additionalProperties": False,
    }
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "stdio",
                    "command": "/bin/x",
                    "tools": [{"original": "lookup", "description": "Rewritten"}],
                }
            ]
        }
    )
    transform, _ = cl.build_transforms(cfg, cfg.backends[0])

    transformed = (await transform.list_tools([tool]))[0]

    assert transformed.parameters["$defs"] == definitions
    assert transformed.parameters["properties"]["address"]


# --- eager / always_load meta ----------------------------------------------


def test_per_tool_always_load_sets_meta():
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "stdio",
                    "command": "/bin/x",
                    "tools": [{"original": "t", "always_load": True}],
                }
            ]
        }
    )
    tr, _ = cl.build_transforms(cfg, cfg.backends[0])
    assert tr._transforms["t"].meta == cl.ALWAYS_LOAD_META


# --- #91: pinning MERGES captured _meta, never clobbers it ------------------


def _pinned_cfg(per_backend=False):
    tool = {"original": "t"} if per_backend else {"original": "t", "always_load": True}
    b = {"name": "b", "transport": "stdio", "command": "/bin/x", "tools": [tool]}
    if per_backend:
        b["always_load"] = True
    return cl.GatewayConfig.model_validate({"backends": [b]})


def test_pin_merges_captured_meta_and_keeps_reserved_key():
    cfg = _pinned_cfg()
    captured = {
        "b": {
            "t": {
                "io.modelcontextprotocol/related-task": "task-1",  # spec-reserved
                "com.example/trace": "abc",
            }
        }
    }
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], {"b": ["t"]}, captured)
    meta = tr._transforms["t"].meta
    assert meta["anthropic/alwaysLoad"] is True
    assert meta["io.modelcontextprotocol/related-task"] == "task-1"  # NOT dropped
    assert meta["com.example/trace"] == "abc"


def test_pin_per_backend_unoverridden_also_merges_meta():
    cfg = _pinned_cfg(per_backend=True)
    captured = {"b": {"t": {"io.modelcontextprotocol/related-task": "task-9"}}}
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], {"b": ["t"]}, captured)
    meta = tr._transforms["t"].meta
    assert meta["anthropic/alwaysLoad"] is True
    assert meta["io.modelcontextprotocol/related-task"] == "task-9"


def test_pin_without_captured_meta_is_flag_only():
    # no captured meta -> pin sets just the flag (pre-fix behaviour preserved)
    cfg = _pinned_cfg()
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], {"b": ["t"]})
    assert tr._transforms["t"].meta == cl.ALWAYS_LOAD_META


def test_pin_flag_wins_on_key_clash():
    cfg = _pinned_cfg()
    captured = {"b": {"t": {"anthropic/alwaysLoad": False}}}
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], {"b": ["t"]}, captured)
    assert tr._transforms["t"].meta["anthropic/alwaysLoad"] is True


def test_no_always_load_means_no_meta():
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "stdio",
                    "command": "/bin/x",
                    "tools": [{"original": "t", "name": "tt"}],
                }
            ]
        }
    )
    tr, _ = cl.build_transforms(cfg, cfg.backends[0])
    assert tr._transforms["t"].meta is None


def test_per_backend_always_load_pins_all_tools():
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "stdio",
                    "command": "/bin/x",
                    "always_load": True,
                }
            ]
        }
    )
    tr, _ = cl.build_transforms(
        cfg, cfg.backends[0], all_tools={"b": ["t1", "t2", "t3"]}
    )
    assert set(tr._transforms) == {"t1", "t2", "t3"}
    assert all(t.meta == cl.ALWAYS_LOAD_META for t in tr._transforms.values())


# --- #162: per-tool output cap (anthropic/maxResultSizeChars) ---------------


def _cap_cfg(**tool_kw):
    return cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "stdio",
                    "command": "/bin/x",
                    "tools": [{"original": "t", **tool_kw}],
                }
            ]
        }
    )


def test_max_result_chars_sets_meta():
    cfg = _cap_cfg(max_result_chars=50000)
    tr, _ = cl.build_transforms(cfg, cfg.backends[0])
    assert tr._transforms["t"].meta == {cl.MAX_RESULT_CHARS_META_KEY: 50000}


def test_max_result_chars_composes_with_pin_and_captured_meta():
    # cap + pin land in ONE merged meta dict on top of the captured original
    cfg = _cap_cfg(max_result_chars=123, always_load=True, name="tt")
    captured = {"b": {"t": {"io.modelcontextprotocol/related-task": "task-1"}}}
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], {"b": ["t"]}, captured)
    meta = tr._transforms["t"].meta
    assert meta["anthropic/alwaysLoad"] is True
    assert meta[cl.MAX_RESULT_CHARS_META_KEY] == 123
    assert meta["io.modelcontextprotocol/related-task"] == "task-1"
    assert tr._transforms["t"].name == "tt"  # rename rides the same transform


def test_disabled_backend_beats_max_result_chars():
    cfg = _cap_cfg(max_result_chars=123)
    cfg.backends[0].enabled = False
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["t"]})
    assert tr._transforms["t"].enabled is False
    assert tr._transforms["t"].meta is None


def test_max_result_chars_toml_roundtrip():
    cfg = _cap_cfg(max_result_chars=99000)
    raw = cl.to_raw(cfg)
    assert raw["backends"][0]["tools"][0]["max_result_chars"] == 99000
    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
    assert reparsed.backends[0].tools[0].max_result_chars == 99000


def test_max_result_chars_unset_is_omitted_from_toml():
    raw = cl.to_raw(_cap_cfg(name="tt"))
    assert "max_result_chars" not in raw["backends"][0]["tools"][0]


@pytest.mark.parametrize("bad", [0, -5, True])
def test_max_result_chars_rejects_nonsense(bad):
    with pytest.raises((cl.ConfigError, ValidationError)):
        _cap_cfg(max_result_chars=bad)


# --- backend-level enable/disable (#38) + display name (#42) ----------------


def _one_backend(**kw):
    return cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "stdio", "command": "/bin/x", **kw}]}
    )


def test_disabled_backend_disables_all_live_tools():
    cfg = _one_backend(enabled=False)
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["t1", "t2"]})
    assert set(tr._transforms) == {"t1", "t2"}
    assert all(t.enabled is False for t in tr._transforms.values())


def test_disabled_backend_overrides_per_tool_enabled():
    cfg = _one_backend(enabled=False, tools=[{"original": "t1", "enabled": True}])
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["t1"]})
    assert tr._transforms["t1"].enabled is False


def test_disabled_backend_beats_always_load():
    # disabled wins: tools stay off and are NOT pinned eager
    cfg = _one_backend(enabled=False, always_load=True)
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["t1"]})
    assert tr._transforms["t1"].enabled is False
    assert tr._transforms["t1"].meta is None


def test_disabled_backend_beats_per_tool_always_load():
    # #116: a PER-TOOL eager pin (an override entry, not the per-backend pin) on a
    # disabled backend must yield a tool that is off AND not eager. Before the fix
    # this path set the alwaysLoad meta without a b.enabled gate, emitting the
    # contradictory {enabled: False, meta: alwaysLoad}.
    cfg = _one_backend(enabled=False, tools=[{"original": "t1", "always_load": True}])
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["t1"]})
    assert tr._transforms["t1"].enabled is False
    assert tr._transforms["t1"].meta is None


def test_enabled_backend_no_override_leaves_tool_untouched():
    cfg = _one_backend()  # enabled defaults True, no overrides, no always_load
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["t1"]})
    assert "t1" not in tr._transforms  # passes through, broadcast as-is


def test_enabled_false_and_display_name_survive_roundtrip():
    cfg = _one_backend(enabled=False, display_name="Nice Label")
    raw = cl.to_raw(cfg)
    assert raw["backends"][0]["enabled"] is False
    assert raw["backends"][0]["display_name"] == "Nice Label"
    reparsed = cl.GatewayConfig.model_validate(raw)
    assert reparsed.backends[0].enabled is False
    assert reparsed.backends[0].display_name == "Nice Label"


def test_defaults_omit_enabled_and_display_name():
    raw = cl.to_raw(_one_backend())  # enabled True + display_name None are defaults
    assert "enabled" not in raw["backends"][0]
    assert "display_name" not in raw["backends"][0]


# --- per-backend instructions ----------------------------------------------
# Each backend endpoint carries only its own server instructions (its override
# else the captured original), so each gets Claude Code's full ~2KB budget (#29).


def _instr_cfg(backends):
    return cl.GatewayConfig.model_validate({"backends": backends})


def _http(name, instructions=None):
    b = {"name": name, "transport": "http", "url": "https://h/mcp"}
    if instructions is not None:
        b["instructions"] = instructions
    return b


def test_backend_instructions_override_wins():
    cfg = _instr_cfg([_http("a", instructions="OVERRIDE")])
    # the per-backend override beats the captured original
    assert cl.backend_instructions(cfg.backends[0], {"a": "captured"}) == "OVERRIDE"


def test_backend_instructions_uses_captured_when_no_override():
    cfg = _instr_cfg([_http("a")])
    assert cl.backend_instructions(cfg.backends[0], {"a": "Use A."}) == "Use A."


def test_backend_instructions_none_when_neither():
    cfg = _instr_cfg([_http("a")])
    assert cl.backend_instructions(cfg.backends[0], {"a": None}) is None


def test_backend_instructions_strips_and_empty_is_none():
    cfg = _instr_cfg([_http("a")])
    assert cl.backend_instructions(cfg.backends[0], {"a": "   "}) is None
    cfg2 = _instr_cfg([_http("a", instructions="  spaced  ")])
    assert cl.backend_instructions(cfg2.backends[0], {"a": None}) == "spaced"


def test_backend_instructions_survive_toml_roundtrip():
    cfg = _instr_cfg([_http("a", instructions="line1\nline2 = ok")])
    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
    assert reparsed.backends[0].instructions == cfg.backends[0].instructions


# --- gateway bearer token (#26) ---------------------------------------------
# The stored VALUE is a ${ENV} ref (like every secret); server._build_app
# resolves it once at startup via expand_env — see tests/test_server.py.


def test_bearer_token_roundtrips_toml():
    cfg = cl.GatewayConfig.model_validate(
        {
            "bearer_token": "${MCP_GATEWAY_TOKEN}",
            "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
        }
    )
    assert cl.to_raw(cfg)["bearer_token"] == "${MCP_GATEWAY_TOKEN}"
    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
    assert reparsed.bearer_token == "${MCP_GATEWAY_TOKEN}"


def test_bearer_token_omitted_when_unset():
    # default None -> the key never lands in config.toml (config stays minimal)
    cfg = _one_backend()
    assert cfg.bearer_token is None
    assert "bearer_token" not in cl.to_raw(cfg)
    assert "bearer_token" not in cl.dump_toml(cfg)


# --- non-loopback bind requires the bearer token (#18) ----------------------


def _cfg_with_host(host: str, **kw):
    return cl.GatewayConfig.model_validate(
        {
            "host": host,
            "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
            **kw,
        }
    )


LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "::1", "[::1]", "127.1.2.3"]
EXPOSED_HOSTS = ["0.0.0.0", "100.99.233.103", "::", "mymac.tailnet.ts.net"]


@pytest.mark.parametrize("host", LOOPBACK_HOSTS)
def test_loopback_hosts_need_no_token(host):
    assert _cfg_with_host(host).bearer_token is None


@pytest.mark.parametrize("host", EXPOSED_HOSTS)
def test_non_loopback_host_without_token_is_refused(host):
    with pytest.raises(cl.ConfigError, match="requires bearer_token"):
        _cfg_with_host(host)


def test_non_loopback_host_with_token_is_allowed():
    cfg = _cfg_with_host("100.99.233.103", bearer_token="${MCP_GATEWAY_TOKEN}")
    assert cfg.host == "100.99.233.103"
    # and it round-trips like any other gateway-level field
    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
    assert reparsed.host == "100.99.233.103"


# --- durable save ----------------------------------------------------------


def test_save_then_load_roundtrips(tmp_path):
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "stdio",
                    "command": "/bin/x",
                    "tools": [{"original": "t", "name": "tt", "description": "d"}],
                }
            ]
        }
    )
    p = tmp_path / "config.toml"
    cl.save(cfg, p)
    assert p.is_file()
    assert not (tmp_path / "config.toml.tmp").exists()  # temp cleaned up
    assert cl.to_raw(cl.load(p)) == cl.to_raw(cfg)


# --- validation ------------------------------------------------------------


def test_http_backend_requires_url():
    with pytest.raises(cl.ConfigError):
        cl.GatewayConfig.model_validate(
            {"backends": [{"name": "b", "transport": "http"}]}
        )


def test_stdio_backend_requires_command():
    with pytest.raises(cl.ConfigError):
        cl.GatewayConfig.model_validate(
            {"backends": [{"name": "b", "transport": "stdio"}]}
        )


# --- sse / streamable-http transports (issue #5) ---------------------------
# FastMCP's RemoteMCPServer.transport accepts {"http","streamable-http","sse"}
# (sse -> SSETransport; http/streamable-http -> StreamableHttpTransport), so we
# pass the transport string through verbatim; all three are url-based.


def test_sse_backend_validates_and_maps():
    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "sse", "url": "https://h/sse"}]}
    )
    assert cfg.backends[0].transport == "sse"
    assert cl.backend_entry(cfg.backends[0]) == {
        "url": "https://h/sse",
        "transport": "sse",
    }


def test_streamable_http_backend_validates_and_maps():
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {"name": "b", "transport": "streamable-http", "url": "https://h/mcp"}
            ]
        }
    )
    assert cfg.backends[0].transport == "streamable-http"
    assert cl.backend_entry(cfg.backends[0]) == {
        "url": "https://h/mcp",
        "transport": "streamable-http",
    }


def test_remote_backend_entry_includes_auth_header():
    b = cl.Backend(
        name="b",
        transport="sse",
        url="https://h/sse",
        auth_header="Authorization",
        auth_value="Bearer tok",
    )
    assert cl.backend_entry(b) == {
        "url": "https://h/sse",
        "transport": "sse",
        "headers": {"Authorization": "Bearer tok"},
    }


def test_sse_backend_requires_url():
    with pytest.raises(cl.ConfigError):
        cl.GatewayConfig.model_validate(
            {"backends": [{"name": "b", "transport": "sse"}]}
        )


def test_streamable_http_backend_requires_url():
    with pytest.raises(cl.ConfigError):
        cl.GatewayConfig.model_validate(
            {"backends": [{"name": "b", "transport": "streamable-http"}]}
        )


def test_remote_transports_survive_toml_roundtrip():
    # to_raw must serialize http/streamable-http/sse as url-based (not stdio).
    for transport in ("http", "streamable-http", "sse"):
        cfg = cl.GatewayConfig.model_validate(
            {
                "backends": [
                    {"name": "b", "transport": transport, "url": "https://h/mcp"}
                ]
            }
        )
        reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
        assert reparsed.backends[0].transport == transport
        assert reparsed.backends[0].url == "https://h/mcp"
        assert reparsed.backends[0].command is None


def test_backend_timeouts_validate_and_survive_toml_roundtrip():
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "init_timeout": 1.25,
                    "request_timeout": 45,
                }
            ]
        }
    )
    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
    assert reparsed.backends[0].init_timeout == 1.25
    assert reparsed.backends[0].request_timeout == 45


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("init_timeout", 0),
        ("init_timeout", 301),
        ("request_timeout", 0),
        ("request_timeout", 3601),
        ("request_timeout", float("nan")),
    ],
)
def test_backend_timeouts_reject_unbounded_values(field, value):
    with pytest.raises(ValidationError):
        _http_backend(**{field: value})


def test_duplicate_backend_names_rejected():
    with pytest.raises(cl.ConfigError):
        cl.GatewayConfig.model_validate(
            {
                "backends": [
                    {"name": "dup", "transport": "http", "url": "https://h/mcp"},
                    {"name": "dup", "transport": "http", "url": "https://h/mcp"},
                ]
            }
        )


# --- backend auth beyond a single header (#6) --------------------------------


def _http_backend(**kw):
    return cl.Backend.model_validate(
        {"name": "b", "transport": "http", "url": "https://h/mcp", **kw}
    )


def test_multiple_headers_expand_env(monkeypatch):
    monkeypatch.setenv("TOK6", "sec")
    e = cl.backend_entry(_http_backend(headers={"X-A": "one", "X-B": "Bearer ${TOK6}"}))
    assert e["headers"] == {"X-A": "one", "X-B": "Bearer sec"}


def test_legacy_pair_wins_over_headers_on_clash():
    e = cl.backend_entry(
        _http_backend(
            headers={"Authorization": "from-dict", "X-C": "keep"},
            auth_header="Authorization",
            auth_value="from-pair",
        )
    )
    assert e["headers"] == {"Authorization": "from-pair", "X-C": "keep"}


def test_oauth_passes_through():
    e = cl.backend_entry(_http_backend(auth="oauth"))
    assert e["auth"] == "oauth"
    with pytest.raises(ValidationError):
        _http_backend(auth="basic")  # only "oauth" is a valid literal


def test_headers_helper_merges_lowest_precedence():
    helper = "echo '" + '{"X-H": "helper", "Authorization": "helper-auth"}' + "'"
    e = cl.backend_entry(
        _http_backend(
            headers_helper=helper,
            headers={"Authorization": "dict-auth"},
        )
    )
    assert e["headers"] == {"X-H": "helper", "Authorization": "dict-auth"}


def test_headers_helper_failures_are_loud():
    with pytest.raises(cl.ConfigError, match="failed"):
        cl.backend_entry(_http_backend(headers_helper="exit 3"))
    with pytest.raises(cl.ConfigError, match="JSON object"):
        cl.backend_entry(_http_backend(headers_helper="echo not-json"))
    with pytest.raises(cl.ConfigError, match="string headers"):
        cl.backend_entry(_http_backend(headers_helper="echo '[1,2]'"))


def test_headers_helper_list_form_runs_without_shell():
    # #81: a list is argv run without a shell — the safe form. echo prints the
    # JSON verbatim, so no shell features are needed to produce headers.
    e = cl.backend_entry(_http_backend(headers_helper=["echo", '{"X-H": "list-form"}']))
    assert e["headers"] == {"X-H": "list-form"}


def test_headers_helper_list_form_no_shell_interpretation():
    # The list form must NOT expand shell syntax — the token stays literal.
    e = cl.backend_entry(_http_backend(headers_helper=["echo", '{"X-H": "$(whoami)"}']))
    assert e["headers"] == {"X-H": "$(whoami)"}


def test_headers_helper_list_form_missing_binary_is_loud():
    # OSError (FileNotFoundError) from a missing executable must surface as a
    # clean ConfigError, not an uncaught crash (#81).
    with pytest.raises(cl.ConfigError, match="failed"):
        cl.backend_entry(_http_backend(headers_helper=["/no/such/binary-xyz"]))


def test_headers_helper_list_roundtrips_toml():
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "headers_helper": ["emit-headers", "--json"],
                }
            ]
        }
    )
    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
    assert reparsed.backends[0].headers_helper == ["emit-headers", "--json"]


# --- #81 backend-name validation ------------------------------------------


@pytest.mark.parametrize("good", ["b", "gateway-github", "a_b-1", "x" * 64])
def test_backend_name_accepts_safe_identifiers(good):
    cl.Backend.model_validate({"name": good, "transport": "stdio", "command": "/bin/x"})


@pytest.mark.parametrize(
    "bad", ["../evil", "a/b", "has space", "dot.name", "", "x" * 65, "admin!"]
)
def test_backend_name_rejects_unsafe(bad):
    with pytest.raises(cl.ConfigError, match="invalid backend name"):
        cl.Backend.model_validate(
            {"name": bad, "transport": "stdio", "command": "/bin/x"}
        )


@pytest.mark.parametrize("reserved", ["virtual", "admin", "health", "ready"])
def test_backend_name_reserved_is_rejected(reserved):
    # Backends mount at /<name> and unmount by path-string equality: a backend
    # named after a built-in route would shadow it, and removing that backend
    # would strip the built-in route itself. /health and /ready are also
    # bearer-auth exemptions, so a same-named backend would serve without auth.
    with pytest.raises(cl.ConfigError, match="reserved"):
        cl.GatewayConfig.model_validate(
            {
                "backends": [
                    {"name": reserved, "transport": "stdio", "command": "/bin/x"}
                ]
            }
        )


def test_empty_bearer_token_is_rejected_before_nonloopback_guard():
    with pytest.raises(cl.ConfigError, match="bearer_token must not be empty"):
        cl.GatewayConfig.model_validate({"host": "0.0.0.0", "bearer_token": ""})


def test_auth_fields_roundtrip_toml(monkeypatch):
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "headers": {"X-A": "${T}"},
                    "auth": "oauth",
                    "headers_helper": "emit-headers",
                }
            ]
        }
    )
    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
    b = reparsed.backends[0]
    assert b.headers == {"X-A": "${T}"}
    assert b.auth == "oauth"
    assert b.headers_helper == "emit-headers"


def test_disabled_backend_broadcasts_no_instructions():
    # #72: disabled -> nil, not just tool-less; override AND captured suppressed
    b = cl.Backend.model_validate(
        {
            "name": "b",
            "transport": "http",
            "url": "https://h/mcp",
            "enabled": False,
            "instructions": "my override",
        }
    )
    assert cl.backend_instructions(b, {"b": "captured blurb"}) is None
    b.enabled = True
    assert cl.backend_instructions(b, {"b": "captured blurb"}) == "my override"


# --- #35: injected param default -> ArgTransformConfig.default ---------------


def _cfg_with_param(param: dict) -> cl.GatewayConfig:
    return cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "stdio",
                    "command": "/bin/x",
                    "tools": [{"original": "t", "params": [param]}],
                }
            ]
        }
    )


def test_param_default_maps_to_arg_transform():
    cfg = _cfg_with_param({"original": "mode", "hide": True, "default": "loud"})
    tr, _ = cl.build_transforms(cfg, cfg.backends[0])
    arg = tr._transforms["t"].arguments["mode"]
    assert arg.hide is True and arg.default == "loud"


def test_param_without_default_leaves_arg_transform_unset():
    # `default` must be ABSENT (exclude_unset) — an explicit None would differ
    # from never-set in FastMCP's to_arg_transform.
    cfg = _cfg_with_param({"original": "mode", "hide": True})
    tr, _ = cl.build_transforms(cfg, cfg.backends[0])
    arg = tr._transforms["t"].arguments["mode"]
    assert "default" not in arg.model_dump(exclude_unset=True)


def test_param_default_survives_toml_roundtrip():
    for value in ("v", 3, 2.5, True):
        cfg = _cfg_with_param({"original": "p", "default": value})
        reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
        p = reparsed.backends[0].tools[0].params[0]
        assert p.default == value and type(p.default) is type(value)


def test_param_no_default_omitted_from_toml():
    cfg = _cfg_with_param({"original": "p", "hide": True})
    raw = cl.to_raw(cfg)
    assert "default" not in raw["backends"][0]["tools"][0]["params"][0]


# --- #43: introspect_interval knob -------------------------------------------


def test_introspect_interval_default_off_and_omitted():
    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}]}
    )
    assert cfg.introspect_interval == 0
    assert "introspect_interval" not in cl.to_raw(cfg)


def test_introspect_interval_roundtrips():
    cfg = cl.GatewayConfig.model_validate(
        {
            "introspect_interval": 900,
            "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
        }
    )
    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
    assert reparsed.introspect_interval == 900


def test_introspect_interval_rejects_negative():
    import pydantic

    with pytest.raises((cl.ConfigError, pydantic.ValidationError)):
        cl.GatewayConfig.model_validate(
            {
                "introspect_interval": -5,
                "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
            }
        )


# --- #A8: update-check toggle -------------------------------------------------


def test_update_check_default_true_and_omitted():
    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}]}
    )
    assert cfg.update_check is True
    assert "update_check" not in cl.to_raw(cfg)  # default True -> minimal TOML


def test_update_check_false_survives_toml_roundtrip():
    cfg = cl.GatewayConfig.model_validate(
        {
            "update_check": False,
            "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
        }
    )
    raw = cl.to_raw(cfg)
    assert raw["update_check"] is False  # the opt-out is serialized explicitly
    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
    assert reparsed.update_check is False
    assert "update_check = false" in cl.dump_toml(cfg)


def test_update_check_omitted_when_true_after_save_load(tmp_path):
    # an explicit true must NOT round-trip into a persisted opt-out line
    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}]}
    )
    cl.save(cfg, str(tmp_path / "config.toml"))
    reloaded = cl.load(tmp_path / "config.toml")
    assert reloaded.update_check is True
    assert "update_check" not in (tmp_path / "config.toml").read_text()


@pytest.mark.parametrize("bad", [1, 0, "yes", "false", "on", None])
def test_update_check_rejects_non_bool(bad):
    import pydantic

    with pytest.raises((cl.ConfigError, pydantic.ValidationError)):
        cl.GatewayConfig.model_validate(
            {
                "update_check": bad,
                "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
            }
        )


# --- structured logging knobs ------------------------------------------------


def test_logging_defaults_are_bounded_and_omitted_from_minimal_toml():
    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}]}
    )
    assert cfg.log_level == "INFO"
    assert cfg.log_max_bytes == 5 * 1024 * 1024
    assert cfg.log_backup_count == 5
    raw = cl.to_raw(cfg)
    assert "log_level" not in raw
    assert "log_max_bytes" not in raw
    assert "log_backup_count" not in raw


def test_logging_settings_roundtrip():
    cfg = cl.GatewayConfig.model_validate(
        {
            "log_level": "DEBUG",
            "log_max_bytes": 131072,
            "log_backup_count": 2,
            "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
        }
    )
    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
    assert reparsed.log_level == "DEBUG"
    assert reparsed.log_max_bytes == 131072
    assert reparsed.log_backup_count == 2


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("log_level", "verbose"),
        ("log_max_bytes", 1024),
        ("log_backup_count", 0),
    ],
)
def test_logging_settings_reject_invalid_values(key, value):
    import pydantic

    with pytest.raises((cl.ConfigError, pydantic.ValidationError)):
        cl.GatewayConfig.model_validate(
            {
                key: value,
                "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
            }
        )


# --- #157: baseline_max_age knob ----------------------------------------------


def test_baseline_max_age_defaults_to_24h_and_omitted():
    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}]}
    )
    assert cfg.baseline_max_age == cl.DEFAULT_BASELINE_MAX_AGE == 86_400
    # default is not persisted (config stays minimal)
    assert "baseline_max_age" not in cl.to_raw(cfg)


@pytest.mark.parametrize("value", [0, 3600, 604_800])
def test_baseline_max_age_non_default_roundtrips(value):
    # 0 (gate off) and any custom age must survive a UI save (to_raw -> reload)
    cfg = cl.GatewayConfig.model_validate(
        {
            "baseline_max_age": value,
            "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
        }
    )
    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
    assert reparsed.baseline_max_age == value


def test_baseline_max_age_rejects_negative():
    import pydantic

    with pytest.raises((cl.ConfigError, pydantic.ValidationError)):
        cl.GatewayConfig.model_validate(
            {
                "baseline_max_age": -1,
                "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
            }
        )


# --- #15 resource + prompt overrides ----------------------------------------


def _rp_backend(resources=None, prompts=None, enabled=True) -> cl.Backend:
    return cl.Backend.model_validate(
        {
            "name": "b",
            "transport": "stdio",
            "command": "/bin/x",
            "enabled": enabled,
            "resources": resources or [],
            "prompts": prompts or [],
        }
    )


def test_resource_prompt_overrides_roundtrip_toml():
    import tomllib as _tomllib

    cfg = cl.GatewayConfig(
        backends=[
            _rp_backend(
                resources=[
                    {
                        "uri": "file://a.txt",
                        "name": "A",
                        "title": "T",
                        "description": "D",
                    },
                    {"uri": "res://{id}", "enabled": False},
                ],
                prompts=[
                    {
                        "original": "p",
                        "name": "better_p",
                        "description": "pd",
                        "args": [{"original": "q", "description": "qd"}],
                    }
                ],
            )
        ]
    )
    reparsed = cl.GatewayConfig.model_validate(_tomllib.loads(cl.dump_toml(cfg)))
    assert cl.to_raw(reparsed) == cl.to_raw(cfg)
    b = reparsed.backends[0]
    assert b.resources[0].name == "A"
    assert b.resources[1].enabled is False
    assert b.prompts[0].args[0].description == "qd"


def test_prompt_arg_override_without_description_not_persisted():
    cfg = cl.GatewayConfig(
        backends=[
            _rp_backend(
                prompts=[{"original": "p", "name": "x", "args": [{"original": "q"}]}]
            )
        ]
    )
    raw = cl.to_raw(cfg)
    assert "args" not in raw["backends"][0]["prompts"][0]


def test_build_resource_prompt_transform_none_when_nothing_to_do():
    assert cl.build_resource_prompt_transform(_rp_backend()) is None


def test_build_resource_prompt_transform_exists_for_disabled_backend():
    # a disabled backend hides everything (defense in depth, mirrors #38)
    assert cl.build_resource_prompt_transform(_rp_backend(enabled=False)) is not None


def test_duplicate_prompt_target_names_raise_value_error():
    b = _rp_backend(
        prompts=[{"original": "a", "name": "x"}, {"original": "b2", "name": "x"}]
    )
    with pytest.raises(ValueError, match="duplicate target name"):
        cl.build_resource_prompt_transform(b)


@pytest.fixture
def rp_fixtures():
    from fastmcp.prompts.base import Prompt, PromptArgument
    from fastmcp.resources.types import TextResource

    resources = [
        TextResource(uri="file://a.txt", name="orig-a", description="da", text="x"),
        TextResource(uri="file://hide.txt", name="h", text="x"),
        TextResource(uri="file://pass.txt", name="p", text="x"),
    ]
    prompts = [
        Prompt(
            name="p1",
            description="old",
            arguments=[PromptArgument(name="q", description="oldarg", required=True)],
        ),
        Prompt(name="p2"),
        Prompt(name="p3"),
    ]
    return resources, prompts


@pytest.fixture
def rp_transform():
    b = _rp_backend(
        resources=[
            {"uri": "file://a.txt", "name": "A", "title": "TA", "description": "DA"},
            {"uri": "file://hide.txt", "enabled": False},
        ],
        prompts=[
            {
                "original": "p1",
                "name": "better_p1",
                "description": "newpd",
                "args": [{"original": "q", "description": "newarg"}],
            },
            {"original": "p2", "enabled": False},
        ],
    )
    t = cl.build_resource_prompt_transform(b)
    assert t is not None
    return t


@pytest.mark.anyio
async def test_list_resources_rewrites_hides_and_passes_through(
    rp_transform, rp_fixtures
):
    resources, _ = rp_fixtures
    out = await rp_transform.list_resources(resources)
    by_uri = {str(r.uri).rstrip("/"): r for r in out}
    assert set(by_uri) == {"file://a.txt", "file://pass.txt"}  # hidden dropped
    a = by_uri["file://a.txt"]
    assert (a.name, a.title, a.description) == ("A", "TA", "DA")
    assert by_uri["file://pass.txt"].name == "p"  # untouched


@pytest.mark.anyio
async def test_get_resource_applies_override_and_blocks_hidden(
    rp_transform, rp_fixtures
):
    resources, _ = rp_fixtures
    lookup = {str(r.uri).rstrip("/"): r for r in resources}

    async def call_next(uri, *, version=None):
        return lookup.get(uri.rstrip("/"))

    got = await rp_transform.get_resource("file://a.txt", call_next)
    assert got is not None and got.name == "A" and got.description == "DA"
    assert await rp_transform.get_resource("file://hide.txt", call_next) is None


@pytest.mark.anyio
async def test_list_resource_templates_rewrites_by_uri_template():
    from fastmcp.resources.template import ResourceTemplate

    b = _rp_backend(resources=[{"uri": "res://{id}", "description": "tuned"}])
    t = cl.build_resource_prompt_transform(b)
    tmpl = ResourceTemplate.model_construct(
        uri_template="res://{id}", name="t", parameters={}
    )
    out = await t.list_resource_templates([tmpl])
    assert out[0].description == "tuned"


@pytest.mark.anyio
async def test_list_prompts_rename_hide_and_arg_descriptions(rp_transform, rp_fixtures):
    _, prompts = rp_fixtures
    out = await rp_transform.list_prompts(prompts)
    names = [p.name for p in out]
    assert names == ["better_p1", "p3"]  # p2 hidden, p1 renamed
    p1 = out[0]
    assert p1.description == "newpd"
    assert p1.arguments[0].description == "newarg"
    assert p1.arguments[0].required is True  # untouched
    assert p1.arguments[0].name == "q"  # arg names never renamed


@pytest.mark.anyio
async def test_list_prompts_rejects_collision_with_new_live_prompt(
    rp_transform, rp_fixtures
):
    from fastmcp.prompts.base import Prompt

    _, prompts = rp_fixtures
    prompts.append(Prompt(name="better_p1"))
    with pytest.raises(ValueError, match="prompt broadcast name"):
        await rp_transform.list_prompts(prompts)


@pytest.mark.anyio
async def test_get_prompt_reverse_maps_renames(rp_transform, rp_fixtures):
    _, prompts = rp_fixtures
    lookup = {p.name: p for p in prompts}
    calls = []

    async def call_next(name, *, version=None):
        calls.append(name)
        return lookup.get(name)

    got = await rp_transform.get_prompt("better_p1", call_next)
    assert calls == ["p1"]  # reverse-mapped to the backend original
    assert got is not None and got.name == "better_p1"
    # a renamed prompt no longer answers to its original name
    assert await rp_transform.get_prompt("p1", call_next) is None
    # hidden prompt is blocked, passthrough untouched
    assert await rp_transform.get_prompt("p2", call_next) is None
    assert (await rp_transform.get_prompt("p3", call_next)).name == "p3"


@pytest.mark.anyio
async def test_disabled_backend_hides_all_resources_and_prompts(rp_fixtures):
    resources, prompts = rp_fixtures
    t = cl.build_resource_prompt_transform(_rp_backend(enabled=False))
    assert await t.list_resources(resources) == []
    assert await t.list_prompts(prompts) == []
