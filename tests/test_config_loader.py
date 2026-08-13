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


def _draw_tool(draw, original: str) -> dict:
    tool: dict = {"original": original, "enabled": draw(st.booleans())}
    if draw(st.booleans()):
        tool["always_load"] = True
    if draw(st.booleans()):  # #162: per-tool output cap
        tool["max_result_chars"] = draw(st.integers(1, 10**7))
    if draw(st.booleans()):  # #286: per-tool description cap
        tool["description_max_bytes"] = draw(st.integers(512, 1_048_576))
    if draw(st.booleans()):
        tool["name"] = draw(ident)
    if draw(st.booleans()):
        tool["title"] = draw(free_text)
    if draw(st.booleans()):
        tool["description"] = draw(free_text)
    params = []
    for original_param in draw(st.lists(ident, max_size=3, unique=True)):
        param: dict = {"original": original_param, "hide": draw(st.booleans())}
        if draw(st.booleans()):
            param["name"] = draw(ident)
        if draw(st.booleans()):
            param["description"] = draw(free_text)
        params.append(param)
    if params:
        tool["params"] = params
    return tool


def _draw_backend(draw, name: str) -> dict:
    transport = draw(st.sampled_from(["http", "streamable-http", "sse", "stdio"]))
    backend: dict = {
        "name": name,
        "transport": transport,
        "stateless": draw(st.booleans()),
    }
    if draw(st.booleans()):
        backend["always_load"] = True
    if draw(st.booleans()):
        backend["instructions"] = draw(free_text)
    # #286: drawn limits fit every generated free_text value. Focused boundary
    # tests cover small limits and precedence.
    if draw(st.booleans()):
        backend["server_instructions_max_bytes"] = draw(st.integers(512, 1_048_576))
    if draw(st.booleans()):
        backend["tool_description_max_bytes"] = draw(st.integers(512, 1_048_576))
    if transport == "stdio":
        backend["command"] = draw(st.sampled_from(["/bin/x", "uvx"]))
        backend["args"] = draw(st.lists(ident, max_size=3))
    else:
        backend["url"] = draw(
            st.sampled_from(["https://h/mcp", "http://127.0.0.1:9/mcp"])
        )
        if draw(st.booleans()):
            backend["auth_header"] = "Authorization"
            backend["auth_value"] = "Bearer ${T}"
    tools = [
        _draw_tool(draw, original)
        for original in draw(st.lists(ident, max_size=3, unique=True))
    ]
    if tools:
        backend["tools"] = tools
    return backend


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
    backends = [_draw_backend(draw, name) for name in names]
    out: dict = {
        "host": "127.0.0.1",
        "port": draw(st.integers(1, 65535)),
        "log_file": "~/x.log",
        "backends": backends,
    }
    if draw(st.booleans()):  # optional gateway bearer token (#26), stored as a ref
        out["bearer_token"] = "${GW_TOKEN}"
    if draw(st.booleans()):  # #286: gateway metadata byte caps
        out["server_instructions_max_bytes"] = draw(st.integers(512, 1_048_576))
    if draw(st.booleans()):
        out["tool_description_max_bytes"] = draw(st.integers(512, 1_048_576))
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


def test_self_check_main_runs_without_printing_resolved_secrets(tmp_path):
    import pathlib
    import subprocess
    import sys

    secret = "codeql-regression-secret-value"
    secrets = tmp_path / "secrets.env"
    secrets.write_text(f"SELF_CHECK_TOKEN={secret}\n")
    config = tmp_path / "config.toml"
    config.write_text(
        """
[[backends]]
name = "secret_backend"
transport = "http"
url = "https://example.invalid/mcp"
auth_header = "Authorization"
auth_value = "Bearer ${SELF_CHECK_TOKEN}"
""".lstrip()
    )

    repo = pathlib.Path(__file__).resolve().parent.parent
    env = {**os.environ, "MCP_GATEWAY_SECRETS": str(secrets)}
    result = subprocess.run(
        [sys.executable, "-m", "mcp_gateway.config_loader", str(config)],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "loaded 1 backend(s)" in result.stdout
    assert secret not in result.stdout
    assert "proxy config:" not in result.stdout


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
    assert p.stat().st_mode & 0o777 == 0o600
    p.write_text("A=two\nB=three\n")
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 5))  # force a newer mtime
    assert cl.load_secrets() == {"A": "two", "B": "three"}


def test_load_secrets_rejects_symlink(monkeypatch, tmp_path):
    target = tmp_path / "actual.env"
    target.write_text("A=one\n")
    link = tmp_path / "secrets.env"
    link.symlink_to(target)
    monkeypatch.setenv("MCP_GATEWAY_SECRETS", str(link))

    with pytest.raises(cl.ConfigError, match="regular file"):
        cl.load_secrets()


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


def test_bearer_token_rejects_raw_secret():
    with pytest.raises(cl.ConfigError, match="raw credentials"):
        cl.GatewayConfig.model_validate({"bearer_token": "literal-token"})


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


def test_ensure_config_seeds_private_file(tmp_path):
    path = tmp_path / "nested" / "config.toml"

    cl.ensure_config(path)

    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600


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
    assert list(tmp_path.glob(".config.toml.*")) == []
    assert p.stat().st_mode & 0o777 == 0o600
    assert cl.to_raw(cl.load(p)) == cl.to_raw(cfg)


def test_save_rejects_symlink_target(tmp_path):
    target = tmp_path / "actual.toml"
    target.write_text("backends = []\n")
    link = tmp_path / "config.toml"
    link.symlink_to(target)

    with pytest.raises(cl.ConfigError, match="regular file"):
        cl.save(cl.GatewayConfig(), link)


def test_save_replace_failure_preserves_existing_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    original = b"backends = []\n"
    path.write_bytes(original)
    path.chmod(0o600)

    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(cl.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        cl.save(cl.GatewayConfig(port=9200), path)

    assert path.read_bytes() == original
    assert list(tmp_path.glob(".config.toml.*")) == []


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


def test_remote_backend_entry_includes_auth_header(monkeypatch):
    monkeypatch.setenv("REMOTE_TOKEN", "tok")
    b = cl.Backend(
        name="b",
        transport="sse",
        url="https://h/sse",
        auth_header="Authorization",
        auth_value="Bearer ${REMOTE_TOKEN}",
    )
    assert cl.backend_entry(b) == {
        "url": "https://h/sse",
        "transport": "sse",
        "headers": {"Authorization": "Bearer tok"},
    }


def test_remote_backend_rejects_raw_auth_value():
    with pytest.raises(cl.ConfigError, match="raw credentials"):
        _http_backend(
            auth_header="Authorization",
            auth_value="Bearer literal-token",
        )


@pytest.mark.parametrize("value", ["${T}", "Bearer ${T}", "Basic ${T}", "Token ${T}"])
def test_auth_value_accepts_safe_templates(value):
    b = _http_backend(auth_header="Authorization", auth_value=value)
    assert b.auth_value == value


@pytest.mark.parametrize(
    "value",
    [
        "Bearer raw-secret ${HOME}",  # contains-ref bypass
        "${A} ${B}",
        "Bearer ${A} ${B}",
        "raw-secret",
        "Bearer",
    ],
)
def test_auth_value_rejects_unsafe_templates(value):
    with pytest.raises(cl.ConfigError, match="raw credentials"):
        _http_backend(auth_header="Authorization", auth_value=value)


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


def test_legacy_pair_wins_over_headers_on_clash(monkeypatch):
    monkeypatch.setenv("PAIR_TOKEN", "from-pair")
    monkeypatch.setenv("DICT_TOKEN", "from-dict")
    e = cl.backend_entry(
        _http_backend(
            headers={"Authorization": "${DICT_TOKEN}", "X-C": "keep"},
            auth_header="Authorization",
            auth_value="${PAIR_TOKEN}",
        )
    )
    assert e["headers"] == {"Authorization": "from-pair", "X-C": "keep"}


def test_oauth_passes_through():
    e = cl.backend_entry(_http_backend(auth="oauth"))
    assert e["auth"] == "oauth"
    with pytest.raises(ValidationError):
        _http_backend(auth="basic")  # only "oauth" is a valid literal


def test_headers_helper_merges_lowest_precedence(monkeypatch):
    monkeypatch.setenv("DICT_AUTH", "dict-auth")
    helper = "echo '" + '{"X-H": "helper", "Authorization": "helper-auth"}' + "'"
    e = cl.backend_entry(
        _http_backend(
            headers_helper=helper,
            headers={"Authorization": "${DICT_AUTH}"},
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


# --- credential-like header/env values must be safe credential templates -----
# (CLI-CREDENTIAL-ARGV-003) The ``auth_value`` rule now covers headers/env at
# the model boundary: a value under a credential-like key must be exactly
# ${ENV_VAR} or one public scheme (Bearer/Basic/Token) followed by one
# reference — any raw literal, or a raw secret mixed with an unrelated ${REF},
# fails validation (TOML load, Admin API backend add, and CLI --file all flow
# through Backend).

_CREDENTIAL_LIKE_KEYS = [
    "Authorization",
    "Proxy-Authorization",
    "Cookie",
    "X-API-Key",
    "apikey",
    "API_KEY",
    "GITHUB_TOKEN",
    "DB_PASSWORD",
    "passwd",
    "client_secret",
    "private_key",
    "credentials",
    "DATABASE_URL",
    "X-Access-Key",
    "REDIS_URL",
    "MONGODB_URI",
    "DSN",
    "CONNECTION_STRING",
]


@pytest.mark.parametrize("key", _CREDENTIAL_LIKE_KEYS)
def test_credential_like_headers_accept_env_refs(key):
    b = _http_backend(headers={key: "Bearer ${T}"})
    assert b.headers[key] == "Bearer ${T}"  # stored verbatim, never expanded


@pytest.mark.parametrize("key", _CREDENTIAL_LIKE_KEYS)
def test_credential_like_headers_reject_raw_literals(key):
    with pytest.raises(cl.ConfigError, match="raw credentials"):
        _http_backend(headers={key: "hunter2"})


@pytest.mark.parametrize("key", _CREDENTIAL_LIKE_KEYS)
def test_credential_like_env_accept_refs_and_reject_raw(key):
    kw = {"name": "b", "transport": "stdio", "command": "/bin/x"}
    b = cl.Backend.model_validate({**kw, "env": {key: "${T}"}})
    assert b.env[key] == "${T}"
    with pytest.raises(cl.ConfigError, match="raw credentials"):
        cl.Backend.model_validate({**kw, "env": {key: "hunter2"}})


@pytest.mark.parametrize(
    "value",
    [
        "${A}",  # bare ref
        "Bearer ${A}",  # public scheme + one ref
        "Basic ${A}",
        "Token ${A}",
        "bearer ${A}",  # scheme case-insensitive
        "  ${A}  ",  # incidental outer whitespace
    ],
)
def test_credential_like_headers_accept_safe_templates(value):
    b = _http_backend(headers={"Authorization": value})
    assert b.headers["Authorization"] == value  # stored verbatim


@pytest.mark.parametrize(
    "value",
    [
        "raw-secret",  # plain literal
        "Bearer raw-secret ${HOME}",  # contains-ref bypass: raw + unrelated ref
        "${A} ${B}",  # ref mixed with a second ref
        "Bearer ${A} ${B}",
        "${A} literal",  # ref + literal mix
        "Bearer",  # scheme with no ref
        "Bearer${A}",  # scheme without the separator space
        "Basic ${A}x",  # trailing literal after the ref
    ],
)
def test_credential_like_headers_reject_unsafe_templates(value):
    with pytest.raises(cl.ConfigError, match="raw credentials"):
        _http_backend(headers={"Authorization": value})


@pytest.mark.parametrize("value", ["${T}", "Bearer ${T}", "Basic ${T}", "Token ${T}"])
def test_credential_like_env_accept_safe_templates(value):
    b = cl.Backend.model_validate(
        {
            "name": "b",
            "transport": "stdio",
            "command": "/bin/x",
            "env": {"API_TOKEN": value},
        }
    )
    assert b.env["API_TOKEN"] == value


def test_credential_like_env_rejects_raw_plus_ref_bypass():
    with pytest.raises(cl.ConfigError, match="raw credentials"):
        cl.Backend.model_validate(
            {
                "name": "b",
                "transport": "stdio",
                "command": "/bin/x",
                "env": {"API_TOKEN": "sk-live raw ${HOME}"},
            }
        )


@pytest.mark.parametrize(
    "key",
    [
        "PASSWORD_STORE_DIR",
        "PASSWORD_FILE",
        "TOKEN_CACHE_DIR",
        "CLIENT_SECRET_PATH",
        "GITHUB_TOKEN_FILE",
        "DSN_DIR",
    ],
)
def test_env_credential_store_paths_may_be_literal(key):
    # These keys name WHERE a credential lives, not the credential itself: a
    # literal filesystem path is valid nonsecret metadata (correctness
    # review), so no ${ENV_VAR} is required.
    b = cl.Backend.model_validate(
        {
            "name": "b",
            "transport": "stdio",
            "command": "/bin/x",
            "env": {key: "/home/u/.secrets/cred"},
        }
    )
    assert b.env[key] == "/home/u/.secrets/cred"


@pytest.mark.parametrize(
    "key", ["PASSWORD", "TOKEN_CACHE", "PASSWORD_FILE_BAK", "API_KEY"]
)
def test_env_credential_store_path_suffix_is_exact(key):
    # the exemption is suffix-exact on the normalized name: a plain credential
    # key or a non-matching suffix still rejects a raw literal
    with pytest.raises(cl.ConfigError, match="raw credentials"):
        cl.Backend.model_validate(
            {
                "name": "b",
                "transport": "stdio",
                "command": "/bin/x",
                "env": {key: "hunter2"},
            }
        )


def test_headers_stay_strict_for_path_suffixed_names():
    # headers use the GENERIC classifier: a path-suffixed credential name in
    # a header is still credential-like (headers carry credentials, not paths)
    with pytest.raises(cl.ConfigError, match="raw credentials"):
        _http_backend(headers={"X-Password-File": "/tmp/creds"})
    b = _http_backend(headers={"X-Password-File": "${CRED_FILE}"})
    assert b.headers["X-Password-File"] == "${CRED_FILE}"


def test_composite_database_url_multiref_rejected():
    # operators store the FULL URL as one ${DATABASE_URL} secret ref;
    # composing it from several refs is intentionally rejected
    with pytest.raises(cl.ConfigError, match="raw credentials"):
        cl.Backend.model_validate(
            {
                "name": "b",
                "transport": "stdio",
                "command": "/bin/x",
                "env": {"DATABASE_URL": "${DB_USER}:${DB_PASS}@${DB_HOST}/db"},
            }
        )


@pytest.mark.parametrize(
    "value",
    [
        "raw-postgres-credential",
        "raw-redis-credential",
        "raw-mongodb-credential",
    ],
)
def test_container_connection_literals_rejected(value):
    # URL-style credentials are the classic container leak: a raw DSN/URI
    # must be rejected just like any other raw secret.
    kw = {"name": "b", "transport": "stdio", "command": "/bin/x"}
    with pytest.raises(cl.ConfigError, match="raw credentials"):
        cl.Backend.model_validate({**kw, "env": {"DATABASE_URL": value}})
    with pytest.raises(cl.ConfigError, match="raw credentials"):
        cl.Backend.model_validate({**kw, "env": {"DSN": value}})
    with pytest.raises(cl.ConfigError, match="raw credentials"):
        _http_backend(headers={"X-Access-Key": value})


def test_credential_error_names_field_and_key_never_value():
    with pytest.raises(cl.ConfigError, match=r"header 'Authorization'") as exc:
        _http_backend(headers={"Authorization": "hunter2"})
    assert "hunter2" not in str(exc.value)
    with pytest.raises(cl.ConfigError, match=r"env 'API_TOKEN'") as exc:
        cl.Backend.model_validate(
            {
                "name": "b",
                "transport": "stdio",
                "command": "/bin/x",
                "env": {"API_TOKEN": "raw-secret"},
            }
        )
    assert "raw-secret" not in str(exc.value)


def test_nonsecret_literals_remain_allowed():
    # HOME/LANG and X-Tenant/X-Client-Id are NOT credential-like: plain
    # literals must keep working (no false positives on ordinary names).
    b = cl.Backend.model_validate(
        {
            "name": "b",
            "transport": "stdio",
            "command": "/bin/x",
            "env": {"HOME": "/tmp/envd-home", "LANG": "en_US.UTF-8"},
        }
    )
    assert b.env == {"HOME": "/tmp/envd-home", "LANG": "en_US.UTF-8"}
    h = _http_backend(headers={"X-Tenant": "acme", "X-Client-Id": "widget-42"})
    assert h.headers == {"X-Tenant": "acme", "X-Client-Id": "widget-42"}


def test_credential_refs_stored_verbatim_and_roundtrip_toml():
    # refs are never expanded or rewritten during validation
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "headers": {
                        "Authorization": "Bearer ${A}",
                        "X-API-Key": "${K}",
                    },
                },
                {
                    "name": "c",
                    "transport": "stdio",
                    "command": "/bin/c",
                    "env": {"API_TOKEN": "${ENVD_TOKEN}", "HOME": "/tmp/x"},
                },
            ]
        }
    )
    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
    assert reparsed.backends[0].headers == {
        "Authorization": "Bearer ${A}",
        "X-API-Key": "${K}",
    }
    assert reparsed.backends[1].env == {
        "API_TOKEN": "${ENVD_TOKEN}",
        "HOME": "/tmp/x",
    }


def test_raw_credential_header_rejected_from_toml(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        'host = "127.0.0.1"\n'
        "[[backends]]\n"
        'name = "b"\n'
        'transport = "http"\n'
        'url = "https://h/mcp"\n'
        'headers = { Authorization = "Bearer hunter2" }\n'
    )
    with pytest.raises(cl.ConfigError, match="raw credentials"):
        cl.load(str(p))


def test_raw_credential_env_rejected_from_toml(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        'host = "127.0.0.1"\n'
        "[[backends]]\n"
        'name = "b"\n'
        'transport = "stdio"\n'
        'command = "/bin/x"\n'
        'env = { API_TOKEN = "raw-secret" }\n'
    )
    with pytest.raises(cl.ConfigError, match="raw credentials"):
        cl.load(str(p))


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


# --- #286: configurable metadata byte limits -------------------------------


def _empty_cfg(**kw) -> cl.GatewayConfig:
    return cl.GatewayConfig.model_validate({"backends": [], **kw})


def _limit_backend(**kw) -> cl.Backend:
    return cl.Backend.model_validate(
        {"name": "b", "transport": "http", "url": "https://h/mcp", **kw}
    )


def _limit_virtual(**kw) -> cl.VirtualTool:
    return cl.VirtualTool.model_validate(
        {
            "name": "v",
            "description": "d",
            "members": [{"backend_id": "x", "tool_original": "s"}],
            **kw,
        }
    )


def test_metadata_limits_defaults_and_are_omitted_from_toml():
    cfg = _empty_cfg()
    assert (
        cfg.server_instructions_max_bytes
        == cl.DEFAULT_SERVER_INSTRUCTIONS_MAX_BYTES
        == 2048
    )
    assert cfg.tool_description_max_bytes is None  # unbounded by default
    raw = cl.to_raw(cfg)
    assert "server_instructions_max_bytes" not in raw
    assert "tool_description_max_bytes" not in raw


def test_utf8_byte_len_counts_bytes_not_characters():
    assert cl.utf8_byte_len("abc") == 3
    assert cl.utf8_byte_len("é") == 2  # 2-byte codepoint
    assert cl.utf8_byte_len("中") == 3  # 3-byte codepoint
    assert cl.utf8_byte_len("🎉") == 4  # 4-byte codepoint
    assert cl.utf8_byte_len("aé中🎉") == 1 + 2 + 3 + 4


def test_utf8_truncate_never_splits_a_codepoint():
    text = "aé中🎉"  # 1+2+3+4 = 10 UTF-8 bytes
    assert cl.utf8_truncate(text, 10) == text  # exact fit is unchanged
    assert cl.utf8_truncate(text, 20) == text  # over-limit is a no-op
    assert cl.utf8_truncate(text, 9) == "aé中"  # 🎉 (4B) dropped whole
    assert cl.utf8_truncate(text, 6) == "aé中"  # 中 (3B) starts at byte 4
    assert cl.utf8_truncate(text, 5) == "aé"  # partial 中 never emitted
    assert cl.utf8_truncate(text, 3) == "aé"  # é ends at byte 3
    assert cl.utf8_truncate(text, 2) == "a"  # byte 2 is inside é — dropped
    assert cl.utf8_truncate(text, 1) == "a"
    for n in range(1, 10):
        cut = cl.utf8_truncate(text, n)
        assert cl.utf8_byte_len(cut) <= n
        assert cut == text[: len(cut)]  # always a prefix (code-point aligned)


def test_sanitize_captured_text_replaces_surrogates_and_counts():
    # clean text passes through unchanged with a zero count, never copied
    assert cl.sanitize_captured_text("") == ("", 0)
    assert cl.sanitize_captured_text("clean text") == ("clean text", 0)
    assert cl.sanitize_captured_text("éé") == ("éé", 0)
    # every lone surrogate becomes exactly one U+FFFD and is counted
    text, replaced = cl.sanitize_captured_text("\ud800x\udc00")
    assert text == "\ufffdx\ufffd"
    assert replaced == 2
    # never raises, and the result is always valid UTF-8
    text.encode("utf-8")
    assert cl.sanitize_captured_text("a\ud800") == ("a\ufffd", 1)


@pytest.mark.parametrize("bad", [True, False, 0, -1, 1_048_577, "2048", 2.5])
def test_gateway_metadata_limits_reject_nonsense(bad):
    for key in ("server_instructions_max_bytes", "tool_description_max_bytes"):
        with pytest.raises((cl.ConfigError, ValidationError)):
            _empty_cfg(**{key: bad})


def test_gateway_instructions_limit_cannot_be_none():
    # the instructions cap is never unbounded: None at the gateway is invalid
    with pytest.raises((cl.ConfigError, ValidationError)):
        _empty_cfg(server_instructions_max_bytes=None)


@pytest.mark.parametrize(
    "key", ["server_instructions_max_bytes", "tool_description_max_bytes"]
)
@pytest.mark.parametrize("bad", [True, False, 0, -1, 1_048_577])
def test_backend_metadata_limits_reject_nonsense(key, bad):
    with pytest.raises((cl.ConfigError, ValidationError)):
        _limit_backend(**{key: bad})


@pytest.mark.parametrize("bad", [True, False, 0, -1, 1_048_577])
def test_tool_description_limit_rejects_nonsense(bad):
    with pytest.raises((cl.ConfigError, ValidationError)):
        cl.ToolOverride.model_validate({"original": "t", "description_max_bytes": bad})


@pytest.mark.parametrize("bad", [True, False, 0, -1, 1_048_577])
def test_virtual_description_limit_rejects_nonsense(bad):
    with pytest.raises((cl.ConfigError, ValidationError)):
        _limit_virtual(description_max_bytes=bad)


def test_metadata_limit_error_never_echoes_long_string_value():
    # a rejected limit value is not echoed back into the error text (it could
    # be an operator's half-pasted blob): only the input TYPE is reported
    raw = "x" * 8192
    for key in ("server_instructions_max_bytes", "tool_description_max_bytes"):
        with pytest.raises(cl.ConfigError) as exc:
            _empty_cfg(**{key: raw})
        assert raw not in str(exc.value)
        assert "got str" in str(exc.value)


def test_metadata_limit_error_never_echoes_huge_integer_value():
    # an out-of-range integer is reported by its allowed range alone, without
    # reflecting the rejected value
    huge = 10**100  # far beyond MAX_METADATA_LIMIT_BYTES
    for key in ("server_instructions_max_bytes", "tool_description_max_bytes"):
        with pytest.raises(cl.ConfigError) as exc:
            _empty_cfg(**{key: huge})
        assert str(huge) not in str(exc.value)
        assert f"between 1 and {cl.MAX_METADATA_LIMIT_BYTES}" in str(exc.value)


def test_authored_instructions_boundary_n_and_n_plus_1():
    limit = 100
    fit = "é" * 50  # exactly 100 UTF-8 bytes
    over = fit + "x"  # 101 bytes
    cl.GatewayConfig.model_validate(
        {
            "server_instructions_max_bytes": limit,
            "backends": [
                {
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "instructions": fit,
                }
            ],
        }
    )
    with pytest.raises(cl.ConfigError, match="instructions are 101 UTF-8 bytes"):
        cl.GatewayConfig.model_validate(
            {
                "server_instructions_max_bytes": limit,
                "backends": [
                    {
                        "name": "b",
                        "transport": "http",
                        "url": "https://h/mcp",
                        "instructions": over,
                    }
                ],
            }
        )


def test_authored_tool_description_boundary_n_and_n_plus_1():
    limit = 40
    fit = "é" * 20  # exactly 40 bytes
    over = fit + "x"  # 41 bytes

    def cfg(description):
        return cl.GatewayConfig.model_validate(
            {
                "tool_description_max_bytes": limit,
                "backends": [
                    {
                        "name": "b",
                        "transport": "http",
                        "url": "https://h/mcp",
                        "tools": [{"original": "t", "description": description}],
                    }
                ],
            }
        )

    assert cfg(fit).backends[0].tools[0].description == fit
    with pytest.raises(cl.ConfigError, match="description is 41 UTF-8 bytes"):
        cfg(over)


def test_instructions_limit_precedence_backend_over_gateway():
    cfg = cl.GatewayConfig.model_validate(
        {
            "server_instructions_max_bytes": 10,
            "backends": [
                {
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "server_instructions_max_bytes": 100,
                    "instructions": "x" * 50,  # fits backend cap, not gateway
                }
            ],
        }
    )
    assert cl.effective_server_instructions_limit(cfg, cfg.backends[0]) == 100
    assert cl.effective_server_instructions_limit(cfg, "b") == 100


def test_tool_description_limit_precedence_tool_over_backend_over_gateway():
    cfg = cl.GatewayConfig.model_validate(
        {
            "tool_description_max_bytes": 50,
            "backends": [
                {
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "tool_description_max_bytes": 100,
                    "tools": [
                        {"original": "t1", "description_max_bytes": 200},
                        {"original": "t2"},
                    ],
                }
            ],
        }
    )
    b = cfg.backends[0]
    # tool cap wins over backend and gateway
    assert cl.effective_tool_description_limit(cfg, b, b.tools[0]) == 200
    assert cl.effective_tool_description_limit(cfg, "b", "t1") == 200
    # tool None inherits the backend cap
    assert cl.effective_tool_description_limit(cfg, "b", "t2") == 100
    # a tool with no override entry uses backend then gateway
    assert cl.effective_tool_description_limit(cfg, "b", "unlisted") == 100


def test_tool_description_limit_none_everywhere_is_unbounded():
    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "http", "url": "https://h/mcp"}]}
    )
    assert cl.effective_tool_description_limit(cfg, "b", "anything") is None
    # no caps at any level -> an arbitrarily long authored description is valid
    cfg2 = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "tools": [{"original": "t", "description": "x" * 5000}],
                }
            ],
        }
    )
    assert cfg2.backends[0].tools[0].description == "x" * 5000


def test_backend_cap_beats_gateway_for_authored_instructions():
    cfg = cl.GatewayConfig.model_validate(
        {
            "server_instructions_max_bytes": 10,
            "backends": [
                {
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "server_instructions_max_bytes": 100,
                    "instructions": "x" * 50,
                }
            ],
        }
    )
    assert cfg.backends[0].instructions == "x" * 50


def test_backend_instructions_gateway_cap_rejects_authored_over_limit():
    with pytest.raises(cl.ConfigError, match="effective cap is 10 bytes"):
        cl.GatewayConfig.model_validate(
            {
                "server_instructions_max_bytes": 10,
                "backends": [
                    {
                        "name": "b",
                        "transport": "http",
                        "url": "https://h/mcp",
                        "instructions": "x" * 50,
                    }
                ],
            }
        )


def test_metadata_limits_roundtrip_toml():
    cfg = cl.GatewayConfig.model_validate(
        {
            "server_instructions_max_bytes": 4096,
            "tool_description_max_bytes": 1024,
            "backends": [
                {
                    "id": "x",
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "server_instructions_max_bytes": 3000,
                    "tool_description_max_bytes": 999,
                    "tools": [{"original": "t", "description_max_bytes": 500}],
                }
            ],
            "virtual_tools": [
                {
                    "name": "v",
                    "description": "d",
                    "description_max_bytes": 250,
                    "members": [{"backend_id": "x", "tool_original": "s"}],
                }
            ],
        }
    )
    reparsed = cl.GatewayConfig.model_validate(tomllib.loads(cl.dump_toml(cfg)))
    assert cl.to_raw(reparsed) == cl.to_raw(cfg)
    assert reparsed.server_instructions_max_bytes == 4096
    assert reparsed.tool_description_max_bytes == 1024
    b = reparsed.backends[0]
    assert b.server_instructions_max_bytes == 3000
    assert b.tool_description_max_bytes == 999
    assert b.tools[0].description_max_bytes == 500
    assert reparsed.virtual_tools[0].description_max_bytes == 250


def test_metadata_limits_omitted_when_defaults_or_none():
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "id": "x",
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "tools": [{"original": "t"}],
                }
            ],
            "virtual_tools": [
                {
                    "name": "v",
                    "description": "d",
                    "members": [{"backend_id": "x", "tool_original": "s"}],
                }
            ],
        }
    )
    raw = cl.to_raw(cfg)
    assert "server_instructions_max_bytes" not in raw  # default 2048
    assert "tool_description_max_bytes" not in raw  # None (unbounded)
    backend = raw["backends"][0]
    assert "server_instructions_max_bytes" not in backend
    assert "tool_description_max_bytes" not in backend
    assert "description_max_bytes" not in backend["tools"][0]
    assert "description_max_bytes" not in raw["virtual_tools"][0]


def test_backend_instructions_truncates_captured_at_utf8_boundary():
    cfg = cl.GatewayConfig.model_validate(
        {
            "server_instructions_max_bytes": 5,
            "backends": [{"name": "b", "transport": "http", "url": "https://h/mcp"}],
        }
    )
    out = cl.backend_instructions(cfg.backends[0], {"b": "ééééé"}, cfg)  # 10 bytes
    assert out == "éé"  # 4 bytes — the partial é is dropped, never split
    assert cl.utf8_byte_len(out) <= 5


def test_backend_instructions_warns_when_captured_truncated():
    from structlog.testing import capture_logs

    cfg = cl.GatewayConfig.model_validate(
        {
            "server_instructions_max_bytes": 5,
            "backends": [{"name": "b", "transport": "http", "url": "https://h/mcp"}],
        }
    )
    with capture_logs() as events:
        cl.backend_instructions(cfg.backends[0], {"b": "ééééé"}, cfg)
    warnings = [e for e in events if e["event"] == "server_instructions_truncated"]
    assert len(warnings) == 1
    assert warnings[0]["backend"] == "b"
    assert warnings[0]["limit_bytes"] == 5
    assert warnings[0]["captured_bytes"] == 10
    assert "é" not in str(warnings[0])  # the text itself is never logged


def test_backend_instructions_truncation_warns_once_per_session():
    from structlog.testing import capture_logs

    cfg = cl.GatewayConfig.model_validate(
        {
            "server_instructions_max_bytes": 5,
            "backends": [{"name": "b", "transport": "http", "url": "https://h/mcp"}],
        }
    )
    with capture_logs() as events:
        for _ in range(3):  # every call still truncates; only the warning dedups
            out = cl.backend_instructions(cfg.backends[0], {"b": "ééééé"}, cfg)
            assert out == "éé"  # 4 bytes — the partial é is dropped, never split

    warnings = [e for e in events if e["event"] == "server_instructions_truncated"]
    # three calls, one dedup signature (backend, cap, captured size) per
    # config session -> exactly one warning
    assert len(warnings) == 1
    assert warnings[0]["limit_bytes"] == 5
    assert warnings[0]["captured_bytes"] == 10


def test_backend_instructions_truncation_rewarns_when_cap_changes():
    from structlog.testing import capture_logs

    cfg = cl.GatewayConfig.model_validate(
        {
            "server_instructions_max_bytes": 5,
            "backends": [{"name": "b", "transport": "http", "url": "https://h/mcp"}],
        }
    )
    with capture_logs() as events:
        cl.backend_instructions(cfg.backends[0], {"b": "ééééé"}, cfg)
        cl.backend_instructions(cfg.backends[0], {"b": "ééééé"}, cfg)
        cfg.server_instructions_max_bytes = 4  # changed cap = new signature
        cl.backend_instructions(cfg.backends[0], {"b": "ééééé"}, cfg)
        cl.backend_instructions(cfg.backends[0], {"b": "ééééé"}, cfg)

    warnings = [e for e in events if e["event"] == "server_instructions_truncated"]
    assert len(warnings) == 2
    assert [w["limit_bytes"] for w in warnings] == [5, 4]


def test_backend_instructions_truncation_rewarns_when_captured_text_changes():
    from structlog.testing import capture_logs

    cfg = cl.GatewayConfig.model_validate(
        {
            "server_instructions_max_bytes": 5,
            "backends": [{"name": "b", "transport": "http", "url": "https://h/mcp"}],
        }
    )
    with capture_logs() as events:
        cl.backend_instructions(cfg.backends[0], {"b": "ééééé"}, cfg)  # 10 bytes
        # SAME byte size, different text -> same signature, memo resets
        cl.backend_instructions(cfg.backends[0], {"b": "abcdefghij"}, cfg)
        cl.backend_instructions(cfg.backends[0], {"b": "abcdefghij"}, cfg)

    warnings = [e for e in events if e["event"] == "server_instructions_truncated"]
    assert len(warnings) == 2
    assert all(w["captured_bytes"] == 10 for w in warnings)


def test_backend_instructions_truncation_rewarns_per_config_session():
    from structlog.testing import capture_logs

    def make_cfg():
        return cl.GatewayConfig.model_validate(
            {
                "server_instructions_max_bytes": 5,
                "backends": [
                    {"name": "b", "transport": "http", "url": "https://h/mcp"}
                ],
            }
        )

    with capture_logs() as events:
        cfg1 = make_cfg()
        cl.backend_instructions(cfg1.backends[0], {"b": "ééééé"}, cfg1)
        cl.backend_instructions(cfg1.backends[0], {"b": "ééééé"}, cfg1)
        cfg2 = make_cfg()  # a fresh config is a fresh broadcast session
        cl.backend_instructions(cfg2.backends[0], {"b": "ééééé"}, cfg2)
        cl.backend_instructions(cfg2.backends[0], {"b": "ééééé"}, cfg2)

    warnings = [e for e in events if e["event"] == "server_instructions_truncated"]
    # one warning per session, not per signature globally
    assert len(warnings) == 2


def test_backend_instructions_never_truncates_authored_override():
    # an authored override over the cap is rejected at load, so the outgoing
    # function sees only validated text and passes it through untouched
    cfg = cl.GatewayConfig.model_validate(
        {
            "server_instructions_max_bytes": 100,
            "backends": [
                {
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "instructions": "é" * 50,
                }
            ],
        }
    )
    assert cl.backend_instructions(cfg.backends[0], {"b": "captured"}, cfg) == "é" * 50
    with pytest.raises(cl.ConfigError):
        cl.GatewayConfig.model_validate(
            {
                "server_instructions_max_bytes": 5,
                "backends": [
                    {
                        "name": "b",
                        "transport": "http",
                        "url": "https://h/mcp",
                        "instructions": "ééééé",
                    }
                ],
            }
        )


def test_backend_instructions_without_cfg_passes_captured_through():
    # backwards compatibility: callers that don't supply cfg get the old
    # behavior unless the backend itself pins a cap
    b = cl.Backend.model_validate(
        {"name": "b", "transport": "http", "url": "https://h/mcp"}
    )
    assert cl.backend_instructions(b, {"b": "long captured blurb"}) == (
        "long captured blurb"
    )
    # a backend-level cap still applies without cfg
    b2 = cl.Backend.model_validate(
        {
            "name": "b",
            "transport": "http",
            "url": "https://h/mcp",
            "server_instructions_max_bytes": 5,
        }
    )
    assert cl.backend_instructions(b2, {"b": "ééééé"}) == "éé"


@pytest.mark.anyio
async def test_transform_truncates_captured_description_at_utf8_boundary():
    from fastmcp.tools.function_tool import FunctionTool

    cfg = cl.GatewayConfig.model_validate(
        {
            "tool_description_max_bytes": 6,
            "backends": [{"name": "b", "transport": "http", "url": "https://h/mcp"}],
        }
    )
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["t"]})

    def fn(x: str) -> str:
        return x

    tool = FunctionTool.from_function(fn, name="t", description="éééé")  # 8 bytes
    [listed] = await tr.list_tools([tool])
    assert listed.description == "ééé"  # 6 bytes, no split codepoint
    assert cl.utf8_byte_len(listed.description) <= 6


@pytest.mark.anyio
async def test_transform_warns_when_captured_description_truncated():
    from fastmcp.tools.function_tool import FunctionTool
    from structlog.testing import capture_logs

    cfg = cl.GatewayConfig.model_validate(
        {
            "tool_description_max_bytes": 6,
            "backends": [{"name": "b", "transport": "http", "url": "https://h/mcp"}],
        }
    )
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["t"]})

    def fn(x: str) -> str:
        return x

    tool = FunctionTool.from_function(fn, name="t", description="éééé")
    with capture_logs() as events:
        await tr.list_tools([tool])
    warnings = [e for e in events if e["event"] == "tool_description_truncated"]
    assert len(warnings) == 1
    assert warnings[0]["tool"] == "t"
    assert warnings[0]["limit_bytes"] == 6
    assert warnings[0]["captured_bytes"] == 8
    assert "é" not in str(warnings[0])


@pytest.mark.anyio
async def test_transform_never_truncates_authored_description():
    from fastmcp.tools.function_tool import FunctionTool

    cfg = cl.GatewayConfig.model_validate(
        {
            "tool_description_max_bytes": 6,
            "backends": [
                {
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "tools": [{"original": "t", "description": "ééé"}],
                }
            ],
        }
    )
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["t"]})

    def fn(x: str) -> str:
        return x

    tool = FunctionTool.from_function(fn, name="t", description="upstream original")
    [listed] = await tr.list_tools([tool])
    assert listed.description == "ééé"  # the authored override, not captured text


@pytest.mark.anyio
async def test_transform_no_caps_passes_tools_through_unchanged():
    from fastmcp.tools.function_tool import FunctionTool

    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "http", "url": "https://h/mcp"}]}
    )
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["stale"]})

    def fn(x: str) -> str:
        return x

    tool = FunctionTool.from_function(
        fn, name="t", description="very long captured description " * 20
    )
    [listed] = await tr.list_tools([tool])
    assert listed is tool  # identical object: no copy, no truncation


@pytest.mark.anyio
async def test_transform_backend_cap_applies_to_unoverridden_tool():
    from fastmcp.tools.function_tool import FunctionTool

    cfg = cl.GatewayConfig.model_validate(
        {
            "tool_description_max_bytes": 100,
            "backends": [
                {
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "tool_description_max_bytes": 8,
                }
            ],
        }
    )
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["t"]})

    def fn(x: str) -> str:
        return x

    tool = FunctionTool.from_function(fn, name="t", description="éééééé")  # 12 bytes
    [listed] = await tr.list_tools([tool])
    assert listed.description == "éééé"  # 8 bytes
    assert cl.utf8_byte_len(listed.description) == 8


def test_transform_description_caps_only_explicit_tool_pins():
    # #286: the per-tool cap map holds ONLY explicit pins; tools inheriting
    # (None) and tools with no override entry resolve through the transform's
    # default fallback (backend > gateway), so the map never duplicates it.
    cfg = cl.GatewayConfig.model_validate(
        {
            "tool_description_max_bytes": 50,
            "backends": [
                {
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "tool_description_max_bytes": 100,
                    "tools": [
                        {"original": "t1", "description_max_bytes": 200},
                        {"original": "t2"},
                    ],
                }
            ],
        }
    )
    tr, _ = cl.build_transforms(
        cfg, cfg.backends[0], all_tools={"b": ["t1", "t2", "t3"]}
    )
    assert tr._description_caps == {"t1": 200}
    # t2 (inherits) and t3 (no override entry) fall back to the backend cap
    assert tr._default_description_cap == 100


@pytest.mark.anyio
async def test_transform_get_tool_truncates_captured_description():
    from fastmcp.tools.function_tool import FunctionTool

    cfg = cl.GatewayConfig.model_validate(
        {
            "tool_description_max_bytes": 6,
            "backends": [{"name": "b", "transport": "http", "url": "https://h/mcp"}],
        }
    )
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["t"]})

    def fn(x: str) -> str:
        return x

    tool = FunctionTool.from_function(fn, name="t", description="éééé")

    async def call_next(name, *, version=None):
        return tool if name == "t" else None

    got = await tr.get_tool("t", call_next)
    assert got.description == "ééé"


@pytest.mark.anyio
async def test_transform_caps_live_tool_missing_from_stale_catalog():
    from fastmcp.tools.function_tool import FunctionTool
    from structlog.testing import capture_logs

    cfg = cl.GatewayConfig.model_validate(
        {
            "tool_description_max_bytes": 100,
            "backends": [
                {
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "tool_description_max_bytes": 6,
                }
            ],
        }
    )
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["old"]})

    def fn(x: str) -> str:
        return x

    tool = FunctionTool.from_function(
        fn, name="new", description="éééé"
    )  # 8 bytes; absent from the stale captured catalog

    async def call_next(name, *, version=None):
        return tool if name == "new" else None

    with capture_logs() as events:
        [listed] = await tr.list_tools([tool])
        fetched = await tr.get_tool("new", call_next)

    assert listed.description == fetched.description == "ééé"
    warnings = [e for e in events if e["event"] == "tool_description_truncated"]
    # list + get share one dedup signature (backend, tool, cap, captured
    # size), so the broadcast session warns exactly once — not per call
    assert len(warnings) == 1
    assert all(
        e["backend"] == "b"
        and e["tool"] == "new"
        and e["limit_bytes"] == 6
        and e["captured_bytes"] == 8
        and e["truncated_bytes"] == 6
        for e in warnings
    )
    assert "é" not in str(warnings)


@pytest.mark.anyio
async def test_transform_truncation_warns_once_across_repeated_list_get():
    from fastmcp.tools.function_tool import FunctionTool
    from structlog.testing import capture_logs

    cfg = cl.GatewayConfig.model_validate(
        {
            "tool_description_max_bytes": 6,
            "backends": [{"name": "b", "transport": "http", "url": "https://h/mcp"}],
        }
    )
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["t"]})

    def fn(x: str) -> str:
        return x

    tool = FunctionTool.from_function(fn, name="t", description="éééé")  # 8 bytes

    async def call_next(name, *, version=None):
        return tool if name == "t" else None

    with capture_logs() as events:
        for _ in range(3):  # every call still truncates; only the warning dedups
            [listed] = await tr.list_tools([tool])
            assert listed.description == "ééé"
            fetched = await tr.get_tool("t", call_next)
            assert fetched.description == "ééé"

    warnings = [e for e in events if e["event"] == "tool_description_truncated"]
    # three list + three get rounds, one dedup signature -> exactly one warning
    assert len(warnings) == 1
    assert warnings[0]["tool"] == "t"
    assert warnings[0]["limit_bytes"] == 6
    assert warnings[0]["captured_bytes"] == 8


@pytest.mark.anyio
async def test_transform_truncation_rewarns_when_cap_changes():
    from fastmcp.tools.function_tool import FunctionTool
    from structlog.testing import capture_logs

    cfg = cl.GatewayConfig.model_validate(
        {
            "tool_description_max_bytes": 100,
            "backends": [
                {
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "tool_description_max_bytes": 6,
                }
            ],
        }
    )
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["t"]})

    def fn(x: str) -> str:
        return x

    tool = FunctionTool.from_function(fn, name="t", description="éééé")  # 8 bytes
    with capture_logs() as events:
        await tr.list_tools([tool])
        await tr.list_tools([tool])
        # a changed effective cap is a new signature -> the memo re-warns
        tr._default_description_cap = 4
        await tr.list_tools([tool])
        await tr.list_tools([tool])

    warnings = [e for e in events if e["event"] == "tool_description_truncated"]
    assert len(warnings) == 2
    assert [w["limit_bytes"] for w in warnings] == [6, 4]
    assert warnings[1]["truncated_bytes"] == 4


@pytest.mark.anyio
async def test_transform_truncation_rewarns_when_captured_text_changes():
    from fastmcp.tools.function_tool import FunctionTool
    from structlog.testing import capture_logs

    cfg = cl.GatewayConfig.model_validate(
        {
            "tool_description_max_bytes": 6,
            "backends": [{"name": "b", "transport": "http", "url": "https://h/mcp"}],
        }
    )
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["t"]})

    def make_tool(desc: str):
        return FunctionTool.from_function(lambda x: x, name="t", description=desc)

    with capture_logs() as events:
        await tr.list_tools([make_tool("éééé")])  # 8 bytes
        # SAME byte size, different text -> same signature, memo resets
        await tr.list_tools([make_tool("abcdefgh")])  # also 8 bytes
        await tr.list_tools([make_tool("abcdefgh")])

    warnings = [e for e in events if e["event"] == "tool_description_truncated"]
    # the changed captured text re-warns exactly once, despite the identical
    # (backend, tool, cap, captured_bytes) signature
    assert len(warnings) == 2
    assert all(e["captured_bytes"] == 8 for e in warnings)
    assert [e["tool"] for e in warnings] == ["t", "t"]


@pytest.mark.anyio
async def test_transform_renamed_tool_truncates_to_per_tool_cap_in_list_and_get():
    # A finite per-tool cap rides a RENAME: the cap lookup keys the RAW source
    # name (the tool's pre-transform identity), not the broadcast name, so
    # the renamed output still truncates to the per-tool cap.
    from fastmcp.tools.function_tool import FunctionTool
    from structlog.testing import capture_logs

    cfg = cl.GatewayConfig.model_validate(
        {
            "tool_description_max_bytes": 100,
            "backends": [
                {
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "tools": [
                        {"original": "old", "name": "new", "description_max_bytes": 6}
                    ],
                }
            ],
        }
    )
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["old"]})

    def fn(x: str) -> str:
        return x

    tool = FunctionTool.from_function(fn, name="old", description="éééé")  # 8 bytes

    async def call_next(name, *, version=None):
        return tool if name == "old" else None

    with capture_logs() as events:
        [listed] = await tr.list_tools([tool])
        fetched = await tr.get_tool("new", call_next)

    assert listed.name == fetched.name == "new"  # the rename still applies
    assert listed.description == fetched.description == "ééé"  # 6 bytes
    warnings = [e for e in events if e["event"] == "tool_description_truncated"]
    # list + get share one dedup signature; identity stays the ORIGINAL name
    assert len(warnings) == 1
    assert all(
        e["backend"] == "b"
        and e["tool"] == "old"
        and e["limit_bytes"] == 6
        and e["captured_bytes"] == 8
        and e["truncated_bytes"] == 6
        for e in warnings
    )
    assert "é" not in str(warnings)


@pytest.mark.anyio
async def test_transform_stale_rename_target_uses_default_cap():
    # A dangling override `old -> new` (the catalog renamed `old` away) must
    # not hijack a NATIVE live tool named `new`: a reverse lookup from the
    # broadcast name misattributes it to the stale entry, skipping capping
    # via the stale authored description and applying the stale per-tool cap
    # instead of the backend/global fallback.
    from fastmcp.tools.function_tool import FunctionTool
    from structlog.testing import capture_logs

    cfg = cl.GatewayConfig.model_validate(
        {
            "tool_description_max_bytes": 100,
            "backends": [
                {
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "tool_description_max_bytes": 6,
                    "tools": [
                        {
                            "original": "old",
                            "name": "new",
                            "description": "ééééééé",  # 14 bytes stale authored text
                            "description_max_bytes": 500,  # stale per-tool cap
                        }
                    ],
                }
            ],
        }
    )
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["old"]})

    def fn(x: str) -> str:
        return x

    tool = FunctionTool.from_function(
        fn, name="new", description="éééé"
    )  # 8 bytes; native, absent from the stale captured catalog

    async def call_next(name, *, version=None):
        # the transform's get path resolves the stale ORIGINAL name; the live
        # catalog answers with its native `new` tool (the name collision)
        return tool if name == "old" else None

    with capture_logs() as events:
        [listed] = await tr.list_tools([tool])
        fetched = await tr.get_tool("new", call_next)

    # the default (backend) cap applies — never the stale per-tool cap, and
    # never skipped by the stale override's authored description
    assert listed.description == fetched.description == "ééé"  # 6 bytes
    warnings = [e for e in events if e["event"] == "tool_description_truncated"]
    # list (8 captured bytes) and get (14 captured bytes) are distinct
    # signatures; both key the NATIVE tool name
    assert len(warnings) == 2
    assert all(
        e["backend"] == "b"
        and e["tool"] == "new"
        and e["limit_bytes"] == 6
        and e["truncated_bytes"] == 6
        for e in warnings
    )
    assert sorted(w["captured_bytes"] for w in warnings) == [8, 14]
    assert "é" not in str(warnings)


# --- #286: captured-text surrogate sanitization -----------------------------


@pytest.mark.anyio
async def test_transform_sanitizes_captured_description_at_cap_boundary():
    # "\ud800" + "🎉" (4-byte emoji): sanitized = U+FFFD (3B) + emoji = 7 bytes,
    # exactly at the cap — tools/list stays serviceable, no truncation.
    from fastmcp.tools.function_tool import FunctionTool
    from structlog.testing import capture_logs

    cfg = cl.GatewayConfig.model_validate(
        {
            "tool_description_max_bytes": 7,
            "backends": [{"name": "b", "transport": "http", "url": "https://h/mcp"}],
        }
    )
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["t"]})

    def fn(x: str) -> str:
        return x

    tool = FunctionTool.from_function(fn, name="t", description="\ud800🎉")
    with capture_logs() as events:
        [listed] = await tr.list_tools([tool])
    assert listed.description == "\ufffd🎉"
    listed.description.encode("utf-8")  # always valid UTF-8, never raises
    assert "\ud800" not in listed.description
    sanitized = [e for e in events if e["event"] == "captured_metadata_sanitized"]
    assert len(sanitized) == 1
    w = sanitized[0]
    assert w["backend"] == "b"
    assert w["kind"] == "tool_description"
    assert w["tool"] == "t"
    assert w["sanitized_scalars"] == 1
    assert w["limit_bytes"] == 7
    assert w["captured_bytes"] == 7  # the sanitized byte length
    assert "truncated_bytes" not in w  # no truncation at the boundary
    assert [e for e in events if e["event"] == "tool_description_truncated"] == []
    # content-free: neither the surrogate nor its replacement is ever logged
    assert "\ud800" not in str(w)
    assert "\ufffd" not in str(w)


@pytest.mark.anyio
async def test_transform_sanitizes_captured_description_with_null_global_cap():
    # gateway cap None = unbounded: text passes through sanitized, and the
    # warning carries no limit_bytes
    from fastmcp.tools.function_tool import FunctionTool
    from structlog.testing import capture_logs

    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "http", "url": "https://h/mcp"}]}
    )
    tr, _ = cl.build_transforms(cfg, cfg.backends[0], all_tools={"b": ["t"]})

    def fn(x: str) -> str:
        return x

    tool = FunctionTool.from_function(fn, name="t", description="x\udfffy")
    with capture_logs() as events:
        [listed] = await tr.list_tools([tool])
    assert listed.description == "x\ufffdy"
    listed.description.encode("utf-8")
    assert "\udfff" not in listed.description
    sanitized = [e for e in events if e["event"] == "captured_metadata_sanitized"]
    assert len(sanitized) == 1
    w = sanitized[0]
    assert w["kind"] == "tool_description"
    assert w["tool"] == "t"
    assert w["sanitized_scalars"] == 1
    assert w["captured_bytes"] == 5  # x + U+FFFD + y
    assert "limit_bytes" not in w  # no cap applies
    assert "truncated_bytes" not in w
    assert "\udfff" not in str(w)
    assert "\ufffd" not in str(w)


def test_backend_instructions_sanitizes_then_truncates_captured():
    # "\ud800" + "é"*5 sanitizes to U+FFFD (3B) + 10B = 13 bytes; cap 5 keeps
    # the U+FFFD and the first é whole (3B + 2B = exactly 5), so the cut lands
    # on a UTF-8 boundary and truncated_bytes <= cap with valid UTF-8
    from structlog.testing import capture_logs

    cfg = cl.GatewayConfig.model_validate(
        {
            "server_instructions_max_bytes": 5,
            "backends": [{"name": "b", "transport": "http", "url": "https://h/mcp"}],
        }
    )
    with capture_logs() as events:
        out = cl.backend_instructions(cfg.backends[0], {"b": "\ud800" + "é" * 5}, cfg)
    assert out == "\ufffdé"
    out.encode("utf-8")  # no raw UnicodeEncodeError, valid UTF-8
    assert cl.utf8_byte_len(out) <= 5
    sanitized = [e for e in events if e["event"] == "captured_metadata_sanitized"]
    assert len(sanitized) == 1
    w = sanitized[0]
    assert w["backend"] == "b"
    assert w["kind"] == "server_instructions"
    assert "tool" not in w  # tool only exists for tool_description
    assert w["sanitized_scalars"] == 1
    assert w["limit_bytes"] == 5
    assert w["captured_bytes"] == 13  # the sanitized byte length
    assert w["truncated_bytes"] == 5
    assert w["truncated_bytes"] <= w["limit_bytes"]
    assert "\ud800" not in str(w)
    assert "\ufffd" not in str(w)
    trunc = [e for e in events if e["event"] == "server_instructions_truncated"]
    assert len(trunc) == 1
    assert trunc[0]["captured_bytes"] == 13
    assert trunc[0]["truncated_bytes"] == 5


def test_backend_instructions_sanitizes_captured_without_cap():
    # no cfg and no backend cap -> no truncation, sanitization still applies
    from structlog.testing import capture_logs

    b = cl.Backend.model_validate(
        {"name": "b", "transport": "http", "url": "https://h/mcp"}
    )
    with capture_logs() as events:
        out = cl.backend_instructions(b, {"b": "\ud800x"})
    assert out == "\ufffdx"
    out.encode("utf-8")
    sanitized = [e for e in events if e["event"] == "captured_metadata_sanitized"]
    assert len(sanitized) == 1
    w = sanitized[0]
    assert w["kind"] == "server_instructions"
    assert w["sanitized_scalars"] == 1
    assert w["captured_bytes"] == 4  # U+FFFD + x
    assert "limit_bytes" not in w
    assert "truncated_bytes" not in w
    assert [e for e in events if e["event"] == "server_instructions_truncated"] == []


@pytest.mark.parametrize(
    "build, field, position",
    [
        (
            lambda: cl.Backend.model_validate(
                {
                    "name": "b",
                    "transport": "http",
                    "url": "https://h/mcp",
                    "instructions": "\ud800",
                }
            ),
            "instructions",
            0,
        ),
        (
            lambda: cl.ToolOverride.model_validate(
                {"original": "t", "description": "ab\udc00"}
            ),
            "description",
            2,
        ),
        (
            lambda: cl.VirtualTool.model_validate(
                {
                    "name": "v",
                    "description": "x\ud83d",
                    "members": [{"backend_id": "x", "tool_original": "s"}],
                }
            ),
            "description",
            1,
        ),
    ],
)
def test_authored_metadata_rejects_unpaired_surrogates_content_free(
    build, field, position
):
    # direct model construction: authored text must be valid UTF-8 — a lone
    # surrogate fails with a ConfigError naming only the field and position
    with pytest.raises(cl.ConfigError) as exc:
        build()
    msg = str(exc.value)
    assert field in msg
    assert f"position {position}" in msg
    # never the content: no surrogate, no U+FFFD, no raw UnicodeEncodeError
    for bad in ("\ud800", "\udc00", "\ud83d", "\udfff", "\ufffd"):
        assert bad not in msg


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
