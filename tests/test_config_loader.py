"""Tests for config_loader — TOML round-trip (property-based), env expansion,
name prefixing, and durable save."""

from __future__ import annotations

import os
import string

import pytest
import tomllib
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import config_loader as cl

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
    names = draw(st.lists(ident, min_size=1, max_size=4, unique=True))
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
                        "description": 'has "quotes", a \\backslash,\nand a newline = ok',
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
    # #80: the documented `uv run config_loader.py <cfg>` self-check must not
    # crash (it used to call build_transforms with a missing arg).
    import pathlib
    import subprocess
    import sys

    repo = pathlib.Path(__file__).resolve().parent.parent
    r = subprocess.run(
        [sys.executable, "config_loader.py", "config.default.toml"],
        cwd=repo,
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
    with pytest.raises(Exception):
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
