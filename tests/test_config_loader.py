"""Tests for config_loader — TOML round-trip (property-based), env expansion,
name prefixing, and durable save."""

from __future__ import annotations

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
        transport = draw(st.sampled_from(["http", "stdio"]))
        b: dict = {"name": nm, "transport": transport, "stateless": draw(st.booleans())}
        if transport == "http":
            b["url"] = draw(
                st.sampled_from(["https://h/mcp", "http://127.0.0.1:9/mcp"])
            )
            if draw(st.booleans()):
                b["auth_header"] = "Authorization"
                b["auth_value"] = "Bearer ${T}"
        else:
            b["command"] = draw(st.sampled_from(["/bin/x", "uvx"]))
            b["args"] = draw(st.lists(ident, max_size=3))
        tools = []
        for to in draw(st.lists(ident, max_size=3, unique=True)):
            t: dict = {"original": to, "enabled": draw(st.booleans())}
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
    return {
        "host": "127.0.0.1",
        "port": draw(st.integers(1, 65535)),
        "log_file": "~/x.log",
        "backends": backends,
    }


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


# --- env expansion ---------------------------------------------------------


def test_expand_env_substitutes(monkeypatch):
    monkeypatch.setenv("MY_TOK", "secret123")
    assert cl.expand_env("Bearer ${MY_TOK}") == "Bearer secret123"


def test_expand_env_missing_raises(monkeypatch):
    monkeypatch.delenv("NOPE_VAR", raising=False)
    with pytest.raises(cl.ConfigError):
        cl.expand_env("${NOPE_VAR}")


# --- name prefixing (exposed_name) -----------------------------------------


def _cfg(n_backends):
    return cl.GatewayConfig.model_validate(
        {
            "backends": [
                {"name": f"b{i}", "transport": "http", "url": "https://h/mcp"}
                for i in range(n_backends)
            ]
        }
    )


def test_exposed_name_single_backend_is_bare():
    cfg = _cfg(1)
    assert cl.exposed_name(cfg, cfg.backends[0], "tool") == "tool"


def test_exposed_name_multi_backend_is_prefixed():
    cfg = _cfg(2)
    assert cl.exposed_name(cfg, cfg.backends[0], "tool") == "b0_tool"


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
