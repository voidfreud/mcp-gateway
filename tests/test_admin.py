"""Tests for admin pure logic — override-vs-default diffing, name validation,
apply_tool_override (store iff differs), and build_state merge."""

from __future__ import annotations

import json
import re
import string
import time

import anyio
import pytest
import structlog
from hypothesis import given
from hypothesis import strategies as st

from mcp_gateway import admin
from mcp_gateway import config_loader as cl

ident = st.text(
    alphabet=string.ascii_letters + string.digits + "_-", min_size=1, max_size=15
)


# --- _override_vs_default --------------------------------------------------


@pytest.mark.parametrize(
    "value,default,expected",
    [
        ("", "x", None),
        ("   ", "x", None),
        ("abc", "abc", None),
        ("  abc  ", "abc", None),  # stripped equals default -> inherit
        ("abc", "xyz", "abc"),
        ("abc", None, "abc"),
        (None, "x", None),
    ],
)
def test_override_vs_default_examples(value, default, expected):
    assert admin._override_vs_default(value, default) == expected


@given(s=st.text(max_size=40))
def test_override_equal_to_default_always_inherits(s):
    """A field left at its (stripped) default never stores an override."""
    assert admin._override_vs_default(s, s.strip() or None) is None


@given(s=st.text(min_size=1, max_size=40).filter(lambda x: x.strip()))
def test_override_differing_returns_cleaned(s):
    assert admin._override_vs_default(s, None) == s.strip()


# --- _validate_name --------------------------------------------------------


@given(name=ident)
def test_validate_accepts_safe_identifiers(name):
    admin._validate_name(name, "tool name")  # must not raise


def test_validate_allows_none():
    admin._validate_name(None, "tool name")  # no override -> fine


@pytest.mark.parametrize(
    "bad", ["has space", "slash/name", "dot.name", "bang!", "", "uni·code"]
)
def test_validate_rejects_unsafe(bad):
    with pytest.raises(cl.ConfigError):
        admin._validate_name(bad, "tool name")


def test_validate_length_cap_64():
    # #41: 64 is the cap Claude Code / MCP expect for tool names
    admin._validate_name("a" * 64, "tool name")  # at the cap -> fine
    with pytest.raises(cl.ConfigError):
        admin._validate_name("a" * 65, "tool name")


# --- #95 invalid-unicode guard --------------------------------------------


def test_clean_and_validate_reject_lone_surrogate():
    # A lone surrogate reaches the server via the JSON API (json.loads accepts
    # \ud83d) but crashes the UTF-8 write in config_loader.save. Reject at the
    # boundary with ConfigError (-> 400) instead of a 500.
    with pytest.raises(cl.ConfigError):
        admin._validate_text("bad \ud83d here", "description")
    with pytest.raises(cl.ConfigError):
        admin._clean("x\ud83dy")
    with pytest.raises(cl.ConfigError):
        admin._override_vs_default("\ud83d", None)


def test_clean_accepts_valid_emoji():
    # the exact 4-byte char that #80 mangled must pass through untouched
    assert admin._clean("Available tools\U0001f928") == "Available tools\U0001f928"


# --- #93 instructions byte cap --------------------------------------------


def test_set_instructions_rejects_over_cap(defaults_dir):
    cfg = _single_cfg()
    with pytest.raises(cl.ConfigError):
        admin.set_instructions(cfg, "b", "a" * (admin.INSTRUCTIONS_MAX_BYTES + 1))


def test_set_instructions_accepts_at_cap(defaults_dir):
    cfg = _single_cfg()
    admin.set_instructions(cfg, "b", "a" * admin.INSTRUCTIONS_MAX_BYTES)
    assert cfg.backends[0].instructions == "a" * admin.INSTRUCTIONS_MAX_BYTES


def test_set_instructions_cap_counts_utf8_bytes_not_chars(defaults_dir):
    # 512 * 4-byte char = 2048 bytes exactly -> at cap; one more char -> over
    at_cap = "\U0001f928" * (admin.INSTRUCTIONS_MAX_BYTES // 4)
    admin.set_instructions(_single_cfg(), "b", at_cap)  # must not raise
    with pytest.raises(cl.ConfigError):
        admin.set_instructions(_single_cfg(), "b", at_cap + "\U0001f928")


# --- #81 FastMCP private-attr tripwire -------------------------------------


def test_fastmcp_proxy_exposes_transforms_list():
    # admin.hot_reload mutates proxy._transforms directly (a private FastMCP
    # attr). If a FastMCP upgrade renames/removes it, fail loudly HERE instead of
    # silently breaking live hot-reload (#81).
    from fastmcp.server import create_proxy

    b = cl.Backend(name="b", transport="stdio", command="/bin/x")
    proxy = create_proxy(cl.to_proxy_config_one(b), name="mcp-gateway-b")
    assert isinstance(getattr(proxy, "_transforms", None), list)
    # add_transform must append to that same list — the exact mechanism hot_reload
    # relies on (holder swap = remove old, add new).
    before = len(proxy._transforms)
    transforms, _ = cl.build_transforms(_single_cfg(), b, {})
    proxy.add_transform(transforms)
    assert len(proxy._transforms) == before + 1


# --- #91 all_meta_from_defaults --------------------------------------------


def test_all_meta_from_defaults_extracts_tool_meta(defaults_dir):
    _write_defaults(
        defaults_dir, "b", "t", meta={"io.modelcontextprotocol/related-task": "x"}
    )
    m = admin.all_meta_from_defaults(_single_cfg())
    assert m == {"b": {"t": {"io.modelcontextprotocol/related-task": "x"}}}


def test_all_meta_from_defaults_omits_tools_without_meta(defaults_dir):
    _write_defaults(defaults_dir, "b", "t")  # no meta captured
    assert admin.all_meta_from_defaults(_single_cfg()) == {}


# --- apply_tool_override (needs a defaults file) ---------------------------


@pytest.fixture
def defaults_dir(tmp_path, monkeypatch):
    d = tmp_path / "defaults"
    d.mkdir()
    monkeypatch.setattr(admin, "DEFAULTS_DIR", d)
    return d


def _write_defaults(
    d,
    backend,
    tool,
    desc="orig desc",
    params=None,
    output_schema=None,
    meta=None,
    annotations=None,
):
    t = {
        "original": tool,
        "title": None,
        "description": desc,
        "params": params or [],
    }
    # Read-only schema surface (issue #2): capture_defaults stores these keys only
    # when the backend advertises them, so the stub mirrors that — keys present
    # only when passed (older defaults files lack them entirely).
    if output_schema is not None:
        t["output_schema"] = output_schema
    if meta is not None:
        t["meta"] = meta
    if annotations is not None:
        t["annotations"] = annotations
    (d / f"{backend}.json").write_text(json.dumps({"backend": backend, "tools": [t]}))


def _single_cfg(backend="b", tool="t"):
    return cl.GatewayConfig.model_validate(
        {"backends": [{"name": backend, "transport": "stdio", "command": "/bin/x"}]}
    )


def test_client_config_handles_all_transports():
    # admin import/introspection must build the client config for sse and
    # streamable-http like the live proxy does — not mis-treat them as stdio (#5).
    for transport in ("http", "streamable-http", "sse"):
        b = cl.Backend(name="b", transport=transport, url="https://h/mcp")
        assert admin._client_config(b) == {
            "mcpServers": {"b": {"url": "https://h/mcp", "transport": transport}}
        }
    stdio = cl.Backend(name="s", transport="stdio", command="/bin/x", args=["mcp"])
    assert admin._client_config(stdio) == {
        "mcpServers": {
            "s": {"command": "/bin/x", "args": ["mcp"], "transport": "stdio"}
        }
    }


def test_apply_stores_override_when_changed(defaults_dir):
    _write_defaults(defaults_dir, "b", "t", desc="orig desc")
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg,
        "b",
        {
            "tool_original": "t",
            "override": {
                "name": "renamed",
                "description": "new",
                "enabled": True,
                "params": [],
            },
        },
    )
    assert len(cfg.backends[0].tools) == 1
    ov = cfg.backends[0].tools[0]
    assert ov.name == "renamed" and ov.description == "new"


def test_apply_stores_nothing_when_equal_to_default(defaults_dir):
    # single backend -> default tool name is the bare original "t"
    _write_defaults(defaults_dir, "b", "t", desc="orig desc")
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg,
        "b",
        {
            "tool_original": "t",
            "override": {
                "name": "t",
                "description": "orig desc",
                "enabled": True,
                "params": [],
            },
        },
    )
    assert cfg.backends[0].tools == []  # nothing stored — minimal config


def test_apply_disable_is_an_override(defaults_dir):
    _write_defaults(defaults_dir, "b", "t")
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg,
        "b",
        {"tool_original": "t", "override": {"enabled": False, "params": []}},
    )
    assert cfg.backends[0].tools[0].enabled is False


def test_apply_partial_put_preserves_absent_fields(defaults_dir):
    # #139: the incident shape — UI disables a tool, a scripted PUT for the same
    # tool carries only {description}; enabled must survive, not reset to True.
    _write_defaults(defaults_dir, "b", "t", desc="orig desc")
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg, "b", {"tool_original": "t", "override": {"enabled": False, "params": []}}
    )
    admin.apply_tool_override(
        cfg, "b", {"tool_original": "t", "override": {"description": "new"}}
    )
    ov = cfg.backends[0].tools[0]
    assert ov.enabled is False and ov.description == "new"


def test_apply_partial_put_preserves_params_and_pin(defaults_dir):
    # #139 merge semantics: absent params/always_load keys keep the stored values.
    _write_defaults(
        defaults_dir, "b", "t", params=[{"original": "p", "description": "d"}]
    )
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg,
        "b",
        {
            "tool_original": "t",
            "override": {
                "always_load": True,
                "params": [{"original": "p", "description": "better"}],
            },
        },
    )
    admin.apply_tool_override(
        cfg, "b", {"tool_original": "t", "override": {"name": "renamed"}}
    )
    ov = cfg.backends[0].tools[0]
    assert ov.name == "renamed"
    assert ov.always_load is True
    assert [p.description for p in ov.params] == ["better"]


def test_apply_explicit_default_still_resets(defaults_dir):
    # Merge is for ABSENT keys only: sending the default value explicitly
    # still clears the stored override field.
    _write_defaults(defaults_dir, "b", "t", desc="orig desc")
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg, "b", {"tool_original": "t", "override": {"description": "new"}}
    )
    admin.apply_tool_override(
        cfg, "b", {"tool_original": "t", "override": {"description": "orig desc"}}
    )
    assert cfg.backends[0].tools == []  # back to minimal config


def test_export_import_round_trips_zero_loss(defaults_dir):
    # #136: export → import onto a clean cfg → identical stored settings.
    _write_defaults(defaults_dir, "b", "t", desc="orig", params=[{"original": "p"}])
    cfg = _single_cfg()
    cfg.backends[0].instructions = "custom instructions"
    cfg.backends[0].always_load = True
    admin.apply_tool_override(
        cfg,
        "b",
        {
            "tool_original": "t",
            "override": {
                "name": "renamed",
                "description": "better",
                "always_load": True,
                "params": [{"original": "p", "description": "pd", "hide": False}],
            },
        },
    )
    bundle = admin.export_settings(cfg)
    clean = _single_cfg()
    affected, errors = admin.import_settings(clean, bundle, mode="replace")
    assert errors == [] and affected == ["b"]
    assert admin.export_settings(clean) == bundle


def test_import_is_reported_per_item_and_rejects_unknowns(defaults_dir):
    _write_defaults(defaults_dir, "b", "t")
    cfg = _single_cfg()
    bundle = {
        "kind": admin.EXPORT_KIND,
        "version": 1,
        "backends": {
            "ghost": {"instructions": "x"},
            "b": {"tools": {"nope": {"name": "y"}, "t": {"name": "ok_name"}}},
        },
    }
    affected, errors = admin.import_settings(cfg, bundle)
    assert len(errors) == 2
    assert any("ghost" in e for e in errors)
    assert any("b/nope" in e for e in errors)
    # caller contract: errors non-empty -> cfg is discarded, so partial
    # application inside the throwaway cfg is fine ("t" did apply here).


def test_import_merge_vs_replace(defaults_dir):
    _write_defaults(defaults_dir, "b", "t", desc="orig")
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg, "b", {"tool_original": "t", "override": {"enabled": False, "params": []}}
    )
    bundle = {"backends": {"b": {"tools": {"t": {"description": "new"}}}}}
    merged = cfg.model_copy(deep=True)
    admin.import_settings(merged, bundle, mode="merge")
    ov = merged.backends[0].tools[0]
    assert ov.enabled is False and ov.description == "new"  # disable survives
    replaced = cfg.model_copy(deep=True)
    admin.import_settings(replaced, bundle, mode="replace")
    ov = replaced.backends[0].tools[0]
    assert ov.enabled is True and ov.description == "new"  # exactly the bundle


def test_import_ignores_backend_topology(defaults_dir):
    # enabled/transport/auth never come from a bundle — settings only.
    _write_defaults(defaults_dir, "b", "t")
    cfg = _single_cfg()
    bundle = {"backends": {"b": {"enabled": False, "tools": {"t": {"name": "n"}}}}}
    affected, errors = admin.import_settings(cfg, bundle)
    assert errors == [] and cfg.backends[0].enabled is True
    assert cfg.backends[0].tools[0].name == "n"


def test_apply_rejects_invalid_name(defaults_dir):
    _write_defaults(defaults_dir, "b", "t")
    cfg = _single_cfg()
    with pytest.raises(cl.ConfigError):
        admin.apply_tool_override(
            cfg,
            "b",
            {
                "tool_original": "t",
                "override": {"name": "bad name", "enabled": True, "params": []},
            },
        )


def test_apply_always_load_alone_is_an_override(defaults_dir):
    # pinning a tool eager, with no text change, must still store the override
    _write_defaults(defaults_dir, "b", "t", desc="orig desc")
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg,
        "b",
        {
            "tool_original": "t",
            "override": {
                "name": "t",
                "description": "orig desc",
                "enabled": True,
                "always_load": True,
                "params": [],
            },
        },
    )
    assert len(cfg.backends[0].tools) == 1
    assert cfg.backends[0].tools[0].always_load is True


def test_apply_max_result_chars_roundtrip(defaults_dir):
    # #162: cap alone is an override; sending null clears it back to minimal.
    _write_defaults(defaults_dir, "b", "t")
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg,
        "b",
        {"tool_original": "t", "override": {"max_result_chars": 80000}},
    )
    assert cfg.backends[0].tools[0].max_result_chars == 80000
    admin.apply_tool_override(
        cfg,
        "b",
        {"tool_original": "t", "override": {"max_result_chars": None}},
    )
    assert cfg.backends[0].tools == []  # back to minimal config


def test_apply_partial_put_preserves_max_result_chars(defaults_dir):
    # #139 merge semantics: an absent max_result_chars key keeps the stored cap.
    _write_defaults(defaults_dir, "b", "t")
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg, "b", {"tool_original": "t", "override": {"max_result_chars": 4096}}
    )
    admin.apply_tool_override(
        cfg, "b", {"tool_original": "t", "override": {"name": "renamed"}}
    )
    ov = cfg.backends[0].tools[0]
    assert ov.name == "renamed" and ov.max_result_chars == 4096


@pytest.mark.parametrize("bad", [0, -1, "abc", 1.5, True, [1]])
def test_apply_rejects_bad_max_result_chars(defaults_dir, bad):
    _write_defaults(defaults_dir, "b", "t")
    cfg = _single_cfg()
    with pytest.raises(cl.ConfigError):
        admin.apply_tool_override(
            cfg, "b", {"tool_original": "t", "override": {"max_result_chars": bad}}
        )
    assert cfg.backends[0].tools == []  # nothing persisted


def test_export_import_round_trips_max_result_chars(defaults_dir):
    _write_defaults(defaults_dir, "b", "t")
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg, "b", {"tool_original": "t", "override": {"max_result_chars": 12345}}
    )
    bundle = admin.export_settings(cfg)
    assert bundle["backends"]["b"]["tools"]["t"]["max_result_chars"] == 12345
    clean = _single_cfg()
    affected, errors = admin.import_settings(clean, bundle, mode="replace")
    assert errors == []
    assert clean.backends[0].tools[0].max_result_chars == 12345


def test_apply_param_hide_stored(defaults_dir):
    _write_defaults(
        defaults_dir, "b", "t", params=[{"original": "p", "description": "pd"}]
    )
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg,
        "b",
        {
            "tool_original": "t",
            "override": {
                "enabled": True,
                "params": [
                    {"original": "p", "name": None, "description": None, "hide": True}
                ],
            },
        },
    )
    assert cfg.backends[0].tools[0].params[0].hide is True


# --- required-param awareness + guard (issue #4) ---------------------------
# capture_defaults needs a live backend, so we stub the captured-defaults file
# (with per-param `required`, exactly the shape capture_defaults now writes) and
# exercise the readers — build_state surfacing it + apply_tool_override's guard.


def test_build_state_surfaces_param_required(defaults_dir):
    _write_defaults(
        defaults_dir,
        "b",
        "t",
        params=[
            {"original": "repoName", "description": "the repo", "required": True},
            {"original": "page", "description": "page no.", "required": False},
        ],
    )
    cfg = _single_cfg()
    params = admin.build_state(cfg)["backends"][0]["tools"][0]["params"]
    by_name = {p["original"]: p for p in params}
    assert by_name["repoName"]["required"] is True
    assert by_name["page"]["required"] is False


def test_apply_rejects_hiding_required_param(defaults_dir):
    _write_defaults(
        defaults_dir,
        "b",
        "t",
        params=[{"original": "repoName", "description": "d", "required": True}],
    )
    cfg = _single_cfg()
    with pytest.raises(cl.ConfigError, match="required by the backend"):
        admin.apply_tool_override(
            cfg,
            "b",
            {
                "tool_original": "t",
                "override": {
                    "enabled": True,
                    "params": [
                        {
                            "original": "repoName",
                            "name": None,
                            "description": None,
                            "hide": True,
                        }
                    ],
                },
            },
        )
    assert cfg.backends[0].tools == []  # rejected -> nothing stored


def test_apply_hide_non_required_param_ok(defaults_dir):
    # hiding an explicitly NON-required param still works
    _write_defaults(
        defaults_dir,
        "b",
        "t",
        params=[{"original": "page", "description": "d", "required": False}],
    )
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg,
        "b",
        {
            "tool_original": "t",
            "override": {
                "enabled": True,
                "params": [
                    {
                        "original": "page",
                        "name": None,
                        "description": None,
                        "hide": True,
                    }
                ],
            },
        },
    )
    assert cfg.backends[0].tools[0].params[0].hide is True


def test_apply_required_param_not_hidden_ok(defaults_dir):
    # a required param can still be edited (renamed) as long as it isn't hidden
    _write_defaults(
        defaults_dir,
        "b",
        "t",
        params=[{"original": "repoName", "description": "d", "required": True}],
    )
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg,
        "b",
        {
            "tool_original": "t",
            "override": {
                "enabled": True,
                "params": [
                    {
                        "original": "repoName",
                        "name": "repo",
                        "description": None,
                        "hide": False,
                    }
                ],
            },
        },
    )
    p = cfg.backends[0].tools[0].params[0]
    assert p.name == "repo" and p.hide is False


# --- #35: injected param default — hide a required param safely --------------


def _param_payload(param="repoName", **fields):
    return {
        "tool_original": "t",
        "override": {
            "enabled": True,
            "params": [
                {"original": param, "name": None, "description": None, **fields}
            ],
        },
    }


def test_apply_hide_required_with_default_ok(defaults_dir):
    _write_defaults(
        defaults_dir,
        "b",
        "t",
        params=[{"original": "repoName", "description": "d", "required": True}],
    )
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg, "b", _param_payload(hide=True, default="acme/widgets")
    )
    p = cfg.backends[0].tools[0].params[0]
    assert p.hide is True and p.default == "acme/widgets"


def test_apply_hide_required_without_default_names_the_fix(defaults_dir):
    _write_defaults(
        defaults_dir,
        "b",
        "t",
        params=[{"original": "repoName", "description": "d", "required": True}],
    )
    cfg = _single_cfg()
    with pytest.raises(cl.ConfigError, match="injected default"):
        admin.apply_tool_override(cfg, "b", _param_payload(hide=True))


def test_apply_default_alone_is_an_override(defaults_dir):
    # a default WITHOUT hide is stored too (the param becomes optional for
    # Claude; the backend gets the value when Claude omits it)
    _write_defaults(
        defaults_dir,
        "b",
        "t",
        params=[{"original": "page", "description": "d", "required": False}],
    )
    cfg = _single_cfg()
    admin.apply_tool_override(cfg, "b", _param_payload("page", hide=False, default=3))
    p = cfg.backends[0].tools[0].params[0]
    assert p.default == 3 and p.hide is False


def test_apply_empty_string_default_stores_nothing(defaults_dir):
    # the UI's cleared field sends "" -> no injection, no override entry
    _write_defaults(
        defaults_dir,
        "b",
        "t",
        params=[{"original": "page", "description": "d", "required": False}],
    )
    cfg = _single_cfg()
    admin.apply_tool_override(cfg, "b", _param_payload("page", hide=False, default=""))
    assert cfg.backends[0].tools == []


def test_apply_non_scalar_default_rejected(defaults_dir):
    _write_defaults(
        defaults_dir,
        "b",
        "t",
        params=[{"original": "page", "description": "d", "required": False}],
    )
    cfg = _single_cfg()
    with pytest.raises(cl.ConfigError, match="string, number, or boolean"):
        admin.apply_tool_override(
            cfg, "b", _param_payload("page", hide=False, default=[1, 2])
        )
    assert cfg.backends[0].tools == []


def test_build_state_surfaces_param_default(defaults_dir):
    _write_defaults(
        defaults_dir,
        "b",
        "t",
        params=[{"original": "page", "description": "d", "required": False}],
    )
    cfg = _single_cfg()
    admin.apply_tool_override(cfg, "b", _param_payload("page", hide=True, default=7))
    params = admin.build_state(cfg)["backends"][0]["tools"][0]["params"]
    assert params[0]["default"] == 7 and params[0]["hide"] is True


def test_export_import_round_trips_param_default(defaults_dir):
    _write_defaults(
        defaults_dir,
        "b",
        "t",
        params=[{"original": "repoName", "description": "d", "required": True}],
    )
    cfg = _single_cfg()
    admin.apply_tool_override(cfg, "b", _param_payload(hide=True, default=True))
    bundle = admin.export_settings(cfg)
    assert bundle["backends"]["b"]["tools"]["t"]["params"][0]["default"] is True
    cfg2 = _single_cfg()
    affected, errors = admin.import_settings(cfg2, bundle)
    assert errors == [] and affected == ["b"]
    p = cfg2.backends[0].tools[0].params[0]
    assert p.hide is True and p.default is True


# --- read-only schema surface (issue #2) -----------------------------------
# The wire tools/list carries outputSchema, _meta (FastMCP tags + our
# anthropic/alwaysLoad pin), and ToolAnnotations. capture_defaults needs a live
# backend, so we stub the captured-defaults file (the exact shape capture_defaults
# now writes) and exercise the readers — build_state surfacing them — plus the
# pure annotations-serialization helper used on the capture side.


def test_build_state_surfaces_output_schema_meta_annotations(defaults_dir):
    _write_defaults(
        defaults_dir,
        "b",
        "t",
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
        },
        meta={"_fastmcp": {"tags": ["search"]}, "anthropic/alwaysLoad": True},
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    cfg = _single_cfg()
    tool = admin.build_state(cfg)["backends"][0]["tools"][0]
    assert tool["output_schema"]["properties"]["answer"]["type"] == "string"
    assert tool["meta"]["anthropic/alwaysLoad"] is True
    assert tool["meta"]["_fastmcp"]["tags"] == ["search"]
    assert tool["annotations"] == {"readOnlyHint": True, "destructiveHint": False}


def test_build_state_schema_fields_none_when_absent(defaults_dir):
    # An old defaults file (pre-#2) lacks these keys entirely -> readers must
    # degrade to None, never KeyError, so the UI simply omits the section.
    _write_defaults(defaults_dir, "b", "t")
    cfg = _single_cfg()
    tool = admin.build_state(cfg)["backends"][0]["tools"][0]
    assert tool["output_schema"] is None
    assert tool["meta"] is None
    assert tool["annotations"] is None


def test_annotations_to_dict_from_pydantic_model():
    # capture side serializes mcp.types.ToolAnnotations -> plain JSON dict,
    # dropping unset (None) hints while keeping explicit False.
    from mcp.types import ToolAnnotations

    ann = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    assert admin._annotations_to_dict(ann) == {
        "readOnlyHint": True,
        "destructiveHint": False,
    }


def test_annotations_to_dict_none_and_empty_are_none():
    from mcp.types import ToolAnnotations

    assert admin._annotations_to_dict(None) is None
    # all hints unset -> nothing worth storing -> None (so no key is written)
    assert admin._annotations_to_dict(ToolAnnotations()) is None


def test_annotations_to_dict_accepts_plain_dict():
    assert admin._annotations_to_dict({"readOnlyHint": True, "x": None}) == {
        "readOnlyHint": True
    }


# --- collision validation (no duplicate broadcast names/descriptions) ------


def _write_defaults_multi(d, backend, tools):
    """tools: list of (original, description)."""
    (d / f"{backend}.json").write_text(
        json.dumps(
            {
                "backend": backend,
                "tools": [
                    {"original": o, "title": None, "description": desc, "params": []}
                    for o, desc in tools
                ],
            }
        )
    )


def test_rename_to_existing_broadcast_name_rejected(defaults_dir):
    _write_defaults_multi(defaults_dir, "b", [("t1", "d1"), ("t2", "d2")])
    cfg = _single_cfg()
    with pytest.raises(cl.ConfigError, match="already used"):
        admin.apply_tool_override(
            cfg,
            "b",
            {
                "tool_original": "t1",
                "override": {"name": "t2", "enabled": True, "params": []},
            },
        )


def test_rename_to_unique_name_ok(defaults_dir):
    _write_defaults_multi(defaults_dir, "b", [("t1", "d1"), ("t2", "d2")])
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg,
        "b",
        {
            "tool_original": "t1",
            "override": {"name": "fresh_name", "enabled": True, "params": []},
        },
    )
    assert cfg.backends[0].tools[0].name == "fresh_name"


def test_taking_a_disabled_tools_name_is_a_clean_400(defaults_dir):
    # SEMANTICS CORRECTED with the transform dry-run: "disabled -> not
    # broadcast -> can't collide" held at the selection level, but FastMCP's
    # ToolTransform rejects duplicate TARGET names regardless of enabled — the
    # old behavior persisted a config that 500s every later hot-reload/mount.
    # Reusing a disabled tool's name now needs renaming the disabled entry
    # away first (the error hints at it).
    _write_defaults_multi(defaults_dir, "b", [("t1", "d1"), ("t2", "d2")])
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "stdio",
                    "command": "/bin/x",
                    "tools": [{"original": "t2", "enabled": False}],
                }
            ]
        }
    )
    with pytest.raises(cl.ConfigError, match="break the transforms"):
        admin.apply_tool_override(
            cfg,
            "b",
            {
                "tool_original": "t1",
                "override": {"name": "t2", "enabled": True, "params": []},
            },
        )
    # rename the disabled entry aside -> the name frees up
    admin.apply_tool_override(
        cfg, "b", {"tool_original": "t2", "override": {"name": "t2_retired"}}
    )
    admin.apply_tool_override(
        cfg,
        "b",
        {
            "tool_original": "t1",
            "override": {"name": "t2", "enabled": True, "params": []},
        },
    )
    assert any(t.original == "t1" and t.name == "t2" for t in cfg.backends[0].tools)


def test_duplicate_description_rejected(defaults_dir):
    _write_defaults_multi(defaults_dir, "b", [("t1", "d1"), ("t2", "SHARED DESC")])
    cfg = _single_cfg()
    with pytest.raises(cl.ConfigError, match="identical"):
        admin.apply_tool_override(
            cfg,
            "b",
            {
                "tool_original": "t1",
                "override": {
                    "description": "SHARED DESC",
                    "enabled": True,
                    "params": [],
                },
            },
        )


# --- #22 opt-in collision auto-uniquify -------------------------------------

# Full-width name strategy: the whole legal charset up to the 64-char cap, so
# the property exercises the trim-to-fit branch too.
name64 = st.text(
    alphabet=string.ascii_letters + string.digits + "_-", min_size=1, max_size=64
)


def test_uniquify_appends_2_then_3():
    assert admin.uniquify_name("t", set()) == "t"  # no collision -> untouched
    assert admin.uniquify_name("t", {"t"}) == "t_2"
    assert admin.uniquify_name("t", {"t", "t_2"}) == "t_3"
    assert admin.uniquify_name("t", {"t", "t_2", "t_3"}) == "t_4"


def test_uniquify_trims_base_to_keep_64_char_cap():
    base = "a" * 64
    assert admin.uniquify_name(base, {base}) == "a" * 62 + "_2"
    # the trimmed _2 also taken -> rolls to _3, still exactly at the cap
    assert admin.uniquify_name(base, {base, "a" * 62 + "_2"}) == "a" * 62 + "_3"


@given(base=name64, taken=st.sets(name64, max_size=20))
def test_uniquify_unique_valid_deterministic(base, taken):
    out = admin.uniquify_name(base, taken)
    assert out not in taken
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", out)  # the _NAME_RE rule holds
    assert out == admin.uniquify_name(base, taken)  # deterministic
    if base not in taken:
        assert out == base  # never renames a non-colliding name


def test_apply_uniquify_flag_stores_suffixed_name(defaults_dir):
    _write_defaults_multi(defaults_dir, "b", [("t1", "d1"), ("t2", "d2")])
    cfg = _single_cfg()
    final = admin.apply_tool_override(
        cfg,
        "b",
        {
            "tool_original": "t1",
            "on_collision": "uniquify",
            "override": {"name": "t2", "enabled": True, "params": []},
        },
    )
    assert final == "t2_2"
    assert cfg.backends[0].tools[0].name == "t2_2"


def test_apply_uniquify_flag_no_collision_returns_none(defaults_dir):
    # flag present but nothing collides -> stored verbatim, no uniquified name
    _write_defaults_multi(defaults_dir, "b", [("t1", "d1"), ("t2", "d2")])
    cfg = _single_cfg()
    final = admin.apply_tool_override(
        cfg,
        "b",
        {
            "tool_original": "t1",
            "on_collision": "uniquify",
            "override": {"name": "fresh", "enabled": True, "params": []},
        },
    )
    assert final is None
    assert cfg.backends[0].tools[0].name == "fresh"


def test_apply_without_flag_still_rejects_collision(defaults_dir):
    # the default (no on_collision key) stays exactly today's strict reject
    _write_defaults_multi(defaults_dir, "b", [("t1", "d1"), ("t2", "d2")])
    cfg = _single_cfg()
    with pytest.raises(cl.ConfigError, match="already used"):
        admin.apply_tool_override(
            cfg,
            "b",
            {
                "tool_original": "t1",
                "override": {"name": "t2", "enabled": True, "params": []},
            },
        )


def test_apply_uniquify_flag_keeps_description_collision_rejected(defaults_dir):
    # uniquifying a DESCRIPTION makes no sense — it rejects even with the flag
    _write_defaults_multi(defaults_dir, "b", [("t1", "d1"), ("t2", "SHARED DESC")])
    cfg = _single_cfg()
    with pytest.raises(cl.ConfigError, match="identical"):
        admin.apply_tool_override(
            cfg,
            "b",
            {
                "tool_original": "t1",
                "on_collision": "uniquify",
                "override": {
                    "description": "SHARED DESC",
                    "enabled": True,
                    "params": [],
                },
            },
        )


def test_apply_uniquify_skips_names_of_other_overrides(defaults_dir):
    # taken = EFFECTIVE names: t2 already renamed to "x", so t1 -> "x" lands "x_2"
    _write_defaults_multi(defaults_dir, "b", [("t1", "d1"), ("t2", "d2")])
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg,
        "b",
        {
            "tool_original": "t2",
            "override": {"name": "x", "enabled": True, "params": []},
        },
    )
    final = admin.apply_tool_override(
        cfg,
        "b",
        {
            "tool_original": "t1",
            "on_collision": "uniquify",
            "override": {"name": "x", "enabled": True, "params": []},
        },
    )
    assert final == "x_2"
    t1 = next(t for t in cfg.backends[0].tools if t.original == "t1")
    assert t1.name == "x_2"


def test_cross_backend_same_broadcast_name_ok(defaults_dir):
    # Two backends, each its own endpoint/MCP server now: renaming one backend's
    # tool to match a tool name in ANOTHER backend must NOT collide.
    _write_defaults_multi(defaults_dir, "b1", [("t1", "d1")])
    _write_defaults_multi(defaults_dir, "b2", [("shared", "d2")])
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {"name": "b1", "transport": "stdio", "command": "/bin/x"},
                {"name": "b2", "transport": "stdio", "command": "/bin/x"},
            ]
        }
    )
    admin.apply_tool_override(
        cfg,
        "b1",
        {
            "tool_original": "t1",
            "override": {"name": "shared", "enabled": True, "params": []},
        },
    )
    assert cfg.backends[0].tools[0].name == "shared"


# --- instructions overrides (set_instructions) -----------------------------


def _write_defaults_instr(d, backend, instructions):
    (d / f"{backend}.json").write_text(
        json.dumps(
            {
                "backend": backend,
                "instructions": instructions,
                "server_info": {"name": backend, "version": "1.0"},
                "tools": [],
            }
        )
    )


def test_backend_instructions_equal_to_default_inherits(defaults_dir):
    _write_defaults_instr(defaults_dir, "b", "ORIGINAL")
    cfg = _single_cfg()
    admin.set_instructions(cfg, "b", "ORIGINAL")  # unchanged -> store nothing
    assert cfg.backends[0].instructions is None


def test_backend_instructions_changed_stored(defaults_dir):
    _write_defaults_instr(defaults_dir, "b", "ORIGINAL")
    cfg = _single_cfg()
    admin.set_instructions(cfg, "b", "EDITED")
    assert cfg.backends[0].instructions == "EDITED"


def test_backend_instructions_added_when_default_none(defaults_dir):
    _write_defaults_instr(defaults_dir, "b", None)  # backend sends none
    cfg = _single_cfg()
    admin.set_instructions(cfg, "b", "MY OWN")
    assert cfg.backends[0].instructions == "MY OWN"


def test_backend_instructions_empty_inherits(defaults_dir):
    _write_defaults_instr(defaults_dir, "b", "ORIGINAL")
    cfg = _single_cfg()
    cfg.backends[0].instructions = "stale"
    admin.set_instructions(cfg, "b", "")
    assert cfg.backends[0].instructions is None


def test_set_instructions_unknown_backend_raises(defaults_dir):
    cfg = _single_cfg()
    with pytest.raises(cl.ConfigError):
        admin.set_instructions(cfg, "nope", "x")


def test_build_state_surfaces_instructions(defaults_dir):
    _write_defaults_instr(defaults_dir, "b", "ORIGINAL BLURB")
    cfg = _single_cfg()
    cfg.backends[0].instructions = "EDITED BLURB"
    state = admin.build_state(cfg)
    bs = state["backends"][0]
    assert bs["default_instructions"] == "ORIGINAL BLURB"
    assert bs["instructions"] == "EDITED BLURB"
    assert bs["server_info"] == {"name": "b", "version": "1.0"}


# --- build_state merge -----------------------------------------------------


def test_build_state_merges_defaults_and_override(defaults_dir):
    _write_defaults(
        defaults_dir,
        "b",
        "t",
        desc="orig desc",
        params=[{"original": "p", "description": "pd"}],
    )
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "stdio",
                    "command": "/bin/x",
                    "tools": [{"original": "t", "name": "renamed"}],
                }
            ]
        }
    )
    state = admin.build_state(cfg)
    tool = state["backends"][0]["tools"][0]
    assert tool["original"] == "t"
    assert tool["default_name"] == "t"  # single backend -> bare
    assert tool["name"] == "renamed"  # override surfaced
    assert tool["default_description"] == "orig desc"
    assert tool["params"][0]["default_name"] == "p"


def test_build_state_includes_endpoint(defaults_dir):
    _write_defaults(defaults_dir, "b", "t")
    cfg = _single_cfg()
    assert admin.build_state(cfg)["backends"][0]["endpoint"] == "/b/mcp"


# --- #79 effective_tools scoping (per-backend collision check) --------------


# --- #45 claude_mcp_command (pure argv builder) ------------------------------


@pytest.mark.parametrize("scope", ["local", "user", "project"])
def test_claude_mcp_command_add_argv(scope):
    url = "http://127.0.0.1:9100/b/mcp"
    assert admin.claude_mcp_command("add", "b", url=url, scope=scope) == [
        "claude",
        "mcp",
        "add",
        "--transport",
        "http",
        "--scope",
        scope,
        "gateway-b",
        url,
    ]


@pytest.mark.parametrize("scope", ["local", "user", "project"])
def test_claude_mcp_command_remove_argv(scope):
    # remove takes no url — it only needs the registration name
    assert admin.claude_mcp_command("remove", "b", scope=scope) == [
        "claude",
        "mcp",
        "remove",
        "--scope",
        scope,
        "gateway-b",
    ]


def test_claude_mcp_command_default_scope_is_local():
    argv = admin.claude_mcp_command("remove", "b")
    assert argv[argv.index("--scope") + 1] == "local"


@pytest.mark.parametrize("bad", ["global", "LOCAL", "", "workspace"])
def test_claude_mcp_command_rejects_bad_scope(bad):
    with pytest.raises(cl.ConfigError, match="scope"):
        admin.claude_mcp_command("add", "b", url="http://h/mcp", scope=bad)


def test_claude_mcp_command_rejects_unknown_action():
    with pytest.raises(cl.ConfigError, match="action"):
        admin.claude_mcp_command("list", "b")


def test_claude_mcp_command_add_requires_url():
    with pytest.raises(cl.ConfigError, match="url"):
        admin.claude_mcp_command("add", "b")


def test_effective_tools_scopes_to_one_backend(defaults_dir):
    _write_defaults(defaults_dir, "b1", "t1")
    _write_defaults(defaults_dir, "b2", "t2")
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {"name": "b1", "transport": "stdio", "command": "/bin/x"},
                {"name": "b2", "transport": "stdio", "command": "/bin/x"},
            ]
        }
    )
    assert {t["backend"] for t in admin.effective_tools(cfg, "b1")} == {"b1"}
    assert {t["backend"] for t in admin.effective_tools(cfg)} == {"b1", "b2"}


# --- #43: throttled baseline refresh -----------------------------------------


def _fake_capture(tools=("t1",), instructions=None):
    async def capture(b):
        return {
            "backend": b.name,
            "captured_at": 0,
            "instructions": instructions,
            "server_info": None,
            "capabilities": None,
            "tools": [
                {"original": t, "title": None, "description": "d", "params": []}
                for t in tools
            ],
        }

    return capture


def _b(name="b"):
    return cl.Backend(name=name, transport="stdio", command="/bin/x")


def test_refresh_defaults_captures_and_reports_delta(defaults_dir, monkeypatch):
    _write_defaults(defaults_dir, "b", "t1")
    monkeypatch.setattr(admin, "capture_defaults", _fake_capture(("t1", "t2")))
    log = structlog.get_logger("test")
    res = anyio.run(lambda: admin.refresh_defaults(_b(), log))
    assert res["status"] == "refreshed"
    assert res["added"] == ["t2"] and res["removed"] == []
    assert res["changed"] is True
    # the baseline file was rewritten
    assert {t["original"] for t in admin.load_defaults("b")["tools"]} == {"t1", "t2"}


def test_refresh_defaults_unchanged_is_not_changed(defaults_dir, monkeypatch):
    _write_defaults(defaults_dir, "b", "t1")
    monkeypatch.setattr(admin, "capture_defaults", _fake_capture(("t1",)))
    log = structlog.get_logger("test")
    res = anyio.run(lambda: admin.refresh_defaults(_b(), log))
    assert res["status"] == "refreshed" and res["changed"] is False


def test_refresh_defaults_throttles_second_call(defaults_dir, monkeypatch):
    _write_defaults(defaults_dir, "b", "t1")
    calls = []

    async def capture(b):
        calls.append(b.name)
        return await _fake_capture(("t1",))(b)

    monkeypatch.setattr(admin, "capture_defaults", capture)
    log = structlog.get_logger("test")

    async def go():
        first = await admin.refresh_defaults(_b(), log)
        second = await admin.refresh_defaults(_b(), log)
        forced = await admin.refresh_defaults(_b(), log, force=True)
        return first, second, forced

    first, second, forced = anyio.run(go)
    assert first["status"] == "refreshed"
    assert second["status"] == "throttled"
    assert forced["status"] == "refreshed"  # manual Re-inspect bypasses
    assert calls == ["b", "b"]


def test_refresh_defaults_error_keeps_throttle(defaults_dir, monkeypatch):
    # a down backend is retried at the throttle cadence, not on every trigger
    async def capture(b):
        raise RuntimeError("down")

    monkeypatch.setattr(admin, "capture_defaults", capture)
    log = structlog.get_logger("test")

    async def go():
        return (
            await admin.refresh_defaults(_b(), log),
            await admin.refresh_defaults(_b(), log),
        )

    first, second = anyio.run(go)
    assert first["status"] == "error" and "down" in first["error"]
    assert second["status"] == "throttled"


def test_refresh_and_reload_hot_reloads_only_on_change(
    defaults_dir, tmp_path, monkeypatch
):
    _write_defaults(defaults_dir, "b", "t1")
    cfg = _single_cfg()
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    reloads = []
    monkeypatch.setattr(admin, "hot_reload", lambda *a, **k: reloads.append(a[3]))
    log = structlog.get_logger("test")

    monkeypatch.setattr(admin, "capture_defaults", _fake_capture(("t1",)))
    res = anyio.run(
        lambda: admin.refresh_and_reload(_b(), str(path), {}, {}, log, force=True)
    )
    assert res["changed"] is False and reloads == []

    monkeypatch.setattr(admin, "capture_defaults", _fake_capture(("t1", "t2")))
    res = anyio.run(
        lambda: admin.refresh_and_reload(_b(), str(path), {}, {}, log, force=True)
    )
    assert res["changed"] is True and reloads == ["b"]


def test_refresh_defaults_instructions_change_counts_as_changed(
    defaults_dir, monkeypatch
):
    _write_defaults(defaults_dir, "b", "t1")  # stub has no "instructions" key
    monkeypatch.setattr(
        admin, "capture_defaults", _fake_capture(("t1",), instructions="new blurb")
    )
    log = structlog.get_logger("test")
    res = anyio.run(lambda: admin.refresh_defaults(_b(), log))
    assert res["changed"] is True


# --- dangling overrides still occupy their broadcast names -------------------
# A #43 baseline refresh can orphan an override (backend renamed the tool
# upstream). The dangling entry still lands in the transforms, so FastMCP
# rejects a duplicate TARGET name at build time — the collision check must see
# dangling names as taken or the save 500s (found live: openrouter drift).


def _cfg_with_dangling(defaults_dir):
    _write_defaults(defaults_dir, "b", "new-tool")  # captured baseline
    cfg = _single_cfg()
    cfg.backends[0].tools = [
        cl.ToolOverride(original="old-tool", name="shiny")  # dangling: not captured
    ]
    return cfg


def test_dangling_override_name_counts_as_taken(defaults_dir):
    cfg = _cfg_with_dangling(defaults_dir)
    names = {t["name"] for t in admin.effective_tools(cfg, "b")}
    assert "shiny" in names  # the dangling entry's broadcast name is occupied
    with pytest.raises(cl.ConfigError, match="already used"):
        admin.apply_tool_override(
            cfg, "b", {"tool_original": "new-tool", "override": {"name": "shiny"}}
        )


def test_dangling_override_uniquify_dodges_it(defaults_dir):
    cfg = _cfg_with_dangling(defaults_dir)
    admin.apply_tool_override(
        cfg,
        "b",
        {
            "tool_original": "new-tool",
            "on_collision": "uniquify",
            "override": {"name": "shiny"},
        },
    )
    stored = {t.original: t for t in cfg.backends[0].tools}
    assert stored["new-tool"].name == "shiny_2"  # suffixed past the dangler


def test_dangling_disabled_override_still_blocks_via_dry_build(defaults_dir):
    # A DISABLED dangler doesn't broadcast, but FastMCP's ToolTransform rejects
    # duplicate TARGET names regardless of enabled — the transform dry-run must
    # turn that into a clean 400 (with the reset hint), never a persisted
    # landmine that 500s every later mount.
    cfg = _cfg_with_dangling(defaults_dir)
    cfg.backends[0].tools[0].enabled = False
    with pytest.raises(cl.ConfigError, match="reset that tool"):
        admin.apply_tool_override(
            cfg, "b", {"tool_original": "new-tool", "override": {"name": "shiny"}}
        )


def test_disabled_captured_tool_duplicate_target_rejected(defaults_dir):
    # Pre-existing landmine, no dangler needed: rename tool A -> "x", disable
    # it, then rename tool B -> "x". The broadcast-level check skips disabled
    # entries, but the transform build still raises — must be a 400 at save.
    _write_defaults(defaults_dir, "b", "a")
    d = json.loads((defaults_dir / "b.json").read_text())
    d["tools"].append(
        {"original": "bb", "title": None, "description": "d", "params": []}
    )
    (defaults_dir / "b.json").write_text(json.dumps(d))
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg, "b", {"tool_original": "a", "override": {"name": "x", "enabled": False}}
    )
    with pytest.raises(cl.ConfigError, match="break the transforms"):
        admin.apply_tool_override(
            cfg, "b", {"tool_original": "bb", "override": {"name": "x"}}
        )


def test_uniquify_dodges_disabled_entries_too(defaults_dir):
    # the suffix must not land on a DISABLED entry's target name
    _write_defaults(defaults_dir, "b", "a")
    d = json.loads((defaults_dir / "b.json").read_text())
    d["tools"] += [
        {"original": "bb", "title": None, "description": "d2", "params": []},
        {"original": "cc", "title": None, "description": "d3", "params": []},
    ]
    (defaults_dir / "b.json").write_text(json.dumps(d))
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg, "b", {"tool_original": "a", "override": {"name": "x"}}
    )
    admin.apply_tool_override(
        cfg, "b", {"tool_original": "bb", "override": {"name": "x_2", "enabled": False}}
    )
    final = admin.apply_tool_override(
        cfg,
        "b",
        {"tool_original": "cc", "on_collision": "uniquify", "override": {"name": "x"}},
    )
    assert final == "x_3"  # skipped enabled "x" AND disabled "x_2"


# --- #153: dangling-override detection + one-click migration -----------------


def _cfg_migrate_setup(defaults_dir):
    # captured baseline is the RENAMED tool ("new-tool") with one param "keep";
    # the stored override still points at the OLD name with tuned text, a pin,
    # and two params — one that survives the rename ("keep"), one that doesn't.
    _write_defaults(
        defaults_dir,
        "b",
        "new-tool",
        desc="new desc",
        params=[{"original": "keep", "description": "kd", "required": False}],
    )
    cfg = _single_cfg()
    cfg.backends[0].tools = [
        cl.ToolOverride(
            original="old-tool",
            name="shiny",
            description="tuned desc",
            always_load=True,
            params=[
                cl.ParamOverride(original="keep", description="better"),
                cl.ParamOverride(original="gone", description="lost"),
            ],
        )
    ]
    return cfg


def test_dangling_overrides_detects_orphaned_entry(defaults_dir):
    cfg = _cfg_with_dangling(defaults_dir)  # captured "new-tool"; override "old-tool"
    d = admin.dangling_overrides(cfg, "b")
    assert len(d) == 1
    assert d[0] == {
        "original": "old-tool",
        "name": "shiny",
        "has_description": False,
        "enabled": True,
    }


def test_dangling_overrides_empty_when_all_captured(defaults_dir):
    _write_defaults(defaults_dir, "b", "t")
    cfg = _single_cfg()
    admin.apply_tool_override(
        cfg, "b", {"tool_original": "t", "override": {"name": "renamed"}}
    )
    assert admin.dangling_overrides(cfg, "b") == []


def test_build_state_surfaces_dangling(defaults_dir):
    cfg = _cfg_with_dangling(defaults_dir)
    bs = admin.build_state(cfg)["backends"][0]
    assert [d["original"] for d in bs["dangling"]] == ["old-tool"]
    assert bs["dangling"][0]["name"] == "shiny"


def test_migrate_override_carries_fields_and_surviving_params(defaults_dir):
    cfg = _cfg_migrate_setup(defaults_dir)
    res = admin.migrate_override(cfg, "b", "old-tool", "new-tool")
    assert res["carried_params"] == ["keep"]
    assert res["dropped_params"] == ["gone"]
    tools = {t.original: t for t in cfg.backends[0].tools}
    assert "old-tool" not in tools  # old entry gone
    nt = tools["new-tool"]
    assert nt.name == "shiny"
    assert nt.description == "tuned desc"
    assert nt.always_load is True
    assert [p.original for p in nt.params] == ["keep"]
    assert nt.params[0].description == "better"


def test_migrate_override_to_unknown_target_raises(defaults_dir):
    cfg = _cfg_migrate_setup(defaults_dir)
    with pytest.raises(cl.ConfigError, match="not a captured tool"):
        admin.migrate_override(cfg, "b", "old-tool", "ghost")


def test_migrate_override_to_overridden_target_raises(defaults_dir):
    _write_defaults(defaults_dir, "b", "new-tool", desc="nd")
    cfg = _single_cfg()
    cfg.backends[0].tools = [
        cl.ToolOverride(original="old-tool", name="shiny"),  # dangling
        cl.ToolOverride(original="new-tool", description="already"),  # target taken
    ]
    with pytest.raises(cl.ConfigError, match="already has a stored override"):
        admin.migrate_override(cfg, "b", "old-tool", "new-tool")


def test_migrate_override_from_not_stored_raises(defaults_dir):
    _write_defaults(defaults_dir, "b", "new-tool")
    cfg = _single_cfg()
    with pytest.raises(cl.ConfigError, match="no stored override"):
        admin.migrate_override(cfg, "b", "missing", "new-tool")


def test_migrate_override_from_live_tool_raises(defaults_dir):
    # "old-tool" is still captured -> not dangling -> can't be migrated
    _write_defaults_multi(defaults_dir, "b", [("old-tool", "d1"), ("new-tool", "d2")])
    cfg = _single_cfg()
    cfg.backends[0].tools = [cl.ToolOverride(original="old-tool", name="shiny")]
    with pytest.raises(cl.ConfigError, match="still a live tool"):
        admin.migrate_override(cfg, "b", "old-tool", "new-tool")


# --- #156: sweep orphaned captured-defaults files ----------------------------


def test_sweep_orphan_defaults_removes_unconfigured(defaults_dir):
    _write_defaults(defaults_dir, "keep", "t")
    _write_defaults(defaults_dir, "off", "t")
    _write_defaults(defaults_dir, "orphan", "t")
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {"name": "keep", "transport": "stdio", "command": "/bin/x"},
                {
                    "name": "off",
                    "transport": "stdio",
                    "command": "/bin/x",
                    "enabled": False,
                },
            ]
        }
    )
    removed = admin.sweep_orphan_defaults(cfg, structlog.get_logger("test"))
    assert removed == ["orphan"]
    assert (defaults_dir / "keep.json").exists()
    assert (defaults_dir / "off.json").exists()  # disabled backend is still configured
    assert not (defaults_dir / "orphan.json").exists()


def test_sweep_orphan_defaults_missing_dir_tolerated(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "DEFAULTS_DIR", tmp_path / "no-such-dir")
    cfg = _single_cfg()
    assert admin.sweep_orphan_defaults(cfg, structlog.get_logger("test")) == []


# --- #46: parse `claude mcp list` -> per-backend registration ----------------


def test_parse_cc_registrations_from_cli_output():
    out = (
        "gateway-alpha: http://h/alpha/mcp (HTTP) - ✓ Connected\n"
        "gateway-beta: http://h/beta/mcp (HTTP) - ✘ Failed to connect\n"
    )
    reg = admin.parse_cc_registrations(out, ["alpha", "beta", "gamma"])
    # registration, NOT liveness: beta's failed connection still counts registered
    assert reg == {"alpha": True, "beta": True, "gamma": False}


def test_parse_cc_registrations_colon_anchors_prefix_match():
    # gateway-cc must NOT be read off gateway-cc-docs (the colon anchors it)
    out = "gateway-cc-docs: http://h/cc-docs/mcp (HTTP) - ✓ Connected\n"
    assert admin.parse_cc_registrations(out, ["cc", "cc-docs"]) == {
        "cc": False,
        "cc-docs": True,
    }


# --- ensure_defaults captures concurrently ------------------------------------


def test_ensure_defaults_captures_concurrently(defaults_dir, monkeypatch):
    # First run / fresh install: N missing backends must cost the slowest
    # capture, not the sum — two 0.2s captures well under 0.4s wall clock.
    import time as _time

    async def slow_capture(b):
        await anyio.sleep(0.2)
        return {
            "backend": b.name,
            "captured_at": 0,
            "instructions": None,
            "server_info": None,
            "capabilities": None,
            "tools": [],
        }

    monkeypatch.setattr(admin, "capture_defaults", slow_capture)
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {"name": "b1", "transport": "stdio", "command": "/bin/x"},
                {"name": "b2", "transport": "stdio", "command": "/bin/y"},
            ]
        }
    )
    log = structlog.get_logger("test")
    started = _time.perf_counter()
    anyio.run(lambda: admin.ensure_defaults(cfg, log))
    elapsed = _time.perf_counter() - started
    assert elapsed < 0.38, f"captures serialized: {elapsed:.2f}s"
    assert admin.load_defaults("b1") and admin.load_defaults("b2")


# --- #15 resource + prompt overrides ----------------------------------------


def _write_rp_defaults(d, backend="b"):
    (d / f"{backend}.json").write_text(
        json.dumps(
            {
                "backend": backend,
                "instructions": None,
                "tools": [],
                "resources": [
                    {
                        "uri": "file://a.txt",
                        "name": "a-name",
                        "title": None,
                        "description": "a-desc",
                        "mime_type": "text/plain",
                    }
                ],
                "resource_templates": [
                    {
                        "uri": "res://{id}",
                        "name": "tmpl",
                        "title": None,
                        "description": "t-desc",
                        "mime_type": None,
                    }
                ],
                "prompts": [
                    {
                        "original": "p1",
                        "title": None,
                        "description": "p-desc",
                        "args": [
                            {"original": "q", "description": "q-desc", "required": True}
                        ],
                    },
                    {"original": "p2", "title": None, "description": None, "args": []},
                ],
            }
        )
    )


def test_apply_resource_override_stores_diff_only(defaults_dir):
    _write_rp_defaults(defaults_dir)
    cfg = _single_cfg()
    admin.apply_resource_override(
        cfg,
        "b",
        {
            "uri": "file://a.txt",
            "override": {"name": "better", "description": "a-desc"},  # desc == default
        },
    )
    (r,) = cfg.backends[0].resources
    assert (r.uri, r.name, r.description) == ("file://a.txt", "better", None)


def test_apply_resource_override_equal_to_default_stores_nothing(defaults_dir):
    _write_rp_defaults(defaults_dir)
    cfg = _single_cfg()
    admin.apply_resource_override(
        cfg,
        "b",
        {"uri": "file://a.txt", "override": {"name": "a-name", "enabled": True}},
    )
    assert cfg.backends[0].resources == []


def test_apply_resource_override_disable_is_an_override(defaults_dir):
    _write_rp_defaults(defaults_dir)
    cfg = _single_cfg()
    admin.apply_resource_override(
        cfg, "b", {"uri": "res://{id}", "override": {"enabled": False}}
    )
    (r,) = cfg.backends[0].resources
    assert r.uri == "res://{id}" and r.enabled is False


def test_apply_resource_override_partial_put_preserves_absent_fields(defaults_dir):
    _write_rp_defaults(defaults_dir)
    cfg = _single_cfg()
    admin.apply_resource_override(
        cfg, "b", {"uri": "file://a.txt", "override": {"name": "better"}}
    )
    admin.apply_resource_override(
        cfg, "b", {"uri": "file://a.txt", "override": {"description": "newd"}}
    )
    (r,) = cfg.backends[0].resources
    assert r.name == "better" and r.description == "newd"


def test_apply_resource_override_unknown_backend_rejected(defaults_dir):
    with pytest.raises(cl.ConfigError, match="unknown backend"):
        admin.apply_resource_override(
            _single_cfg(), "nope", {"uri": "file://a.txt", "override": {}}
        )


def test_apply_prompt_override_stores_rename_and_arg_diffs(defaults_dir):
    _write_rp_defaults(defaults_dir)
    cfg = _single_cfg()
    admin.apply_prompt_override(
        cfg,
        "b",
        {
            "prompt_original": "p1",
            "override": {
                "name": "better_p1",
                "description": "p-desc",  # == default -> inherits
                "args": [{"original": "q", "description": "new-q"}],
            },
        },
    )
    (p,) = cfg.backends[0].prompts
    assert (p.original, p.name, p.description) == ("p1", "better_p1", None)
    assert [(a.original, a.description) for a in p.args] == [("q", "new-q")]


def test_apply_prompt_override_no_diff_removes_entry(defaults_dir):
    _write_rp_defaults(defaults_dir)
    cfg = _single_cfg()
    admin.apply_prompt_override(
        cfg,
        "b",
        {"prompt_original": "p1", "override": {"name": "x"}},
    )
    admin.apply_prompt_override(
        cfg,
        "b",
        {
            "prompt_original": "p1",
            "override": {"name": "p1", "description": "p-desc", "enabled": True},
        },
    )
    assert cfg.backends[0].prompts == []


def test_apply_prompt_override_rejects_invalid_name(defaults_dir):
    _write_rp_defaults(defaults_dir)
    with pytest.raises(cl.ConfigError, match="invalid prompt name"):
        admin.apply_prompt_override(
            _single_cfg(),
            "b",
            {"prompt_original": "p1", "override": {"name": "has space"}},
        )


def test_apply_prompt_override_rejects_broadcast_name_collision(defaults_dir):
    _write_rp_defaults(defaults_dir)
    cfg = _single_cfg()
    with pytest.raises(cl.ConfigError, match="already used by prompt"):
        admin.apply_prompt_override(
            cfg, "b", {"prompt_original": "p1", "override": {"name": "p2"}}
        )


def test_apply_prompt_override_duplicate_target_with_disabled_is_400(defaults_dir):
    # broadcast-level check skips disabled entries; the transform dry-build
    # still rejects a duplicate TARGET name (mirrors the tool landmine, #152).
    _write_rp_defaults(defaults_dir)
    cfg = _single_cfg()
    admin.apply_prompt_override(
        cfg,
        "b",
        {"prompt_original": "p1", "override": {"name": "x", "enabled": False}},
    )
    with pytest.raises(cl.ConfigError, match="break the transforms"):
        admin.apply_prompt_override(
            cfg, "b", {"prompt_original": "p2", "override": {"name": "x"}}
        )


def test_apply_prompt_override_partial_put_preserves_args(defaults_dir):
    _write_rp_defaults(defaults_dir)
    cfg = _single_cfg()
    admin.apply_prompt_override(
        cfg,
        "b",
        {
            "prompt_original": "p1",
            "override": {"args": [{"original": "q", "description": "tuned"}]},
        },
    )
    admin.apply_prompt_override(
        cfg, "b", {"prompt_original": "p1", "override": {"name": "renamed"}}
    )
    (p,) = cfg.backends[0].prompts
    assert p.name == "renamed"
    assert [(a.original, a.description) for a in p.args] == [("q", "tuned")]


def test_build_state_surfaces_resources_and_prompts(defaults_dir):
    _write_rp_defaults(defaults_dir)
    cfg = _single_cfg()
    admin.apply_resource_override(
        cfg, "b", {"uri": "file://a.txt", "override": {"name": "better"}}
    )
    admin.apply_prompt_override(
        cfg,
        "b",
        {
            "prompt_original": "p1",
            "override": {"args": [{"original": "q", "description": "tuned"}]},
        },
    )
    state = admin.build_state(cfg)["backends"][0]
    res = {r["uri"]: r for r in state["resources"]}
    assert res["file://a.txt"]["name"] == "better"
    assert res["file://a.txt"]["default_name"] == "a-name"
    assert res["file://a.txt"]["template"] is False
    assert res["res://{id}"]["template"] is True
    prompts = {p["original"]: p for p in state["prompts"]}
    assert prompts["p1"]["args"][0]["description"] == "tuned"
    assert prompts["p1"]["args"][0]["default_description"] == "q-desc"
    assert prompts["p1"]["args"][0]["required"] is True
    assert prompts["p2"]["enabled"] is True


def test_build_state_degrades_without_rp_capture(defaults_dir):
    # pre-#15 defaults file (no resources/prompts keys) -> empty lists, no crash
    _write_defaults(defaults_dir, "b", "t")
    state = admin.build_state(_single_cfg())["backends"][0]
    assert state["resources"] == [] and state["prompts"] == []


def test_export_import_round_trips_resources_and_prompts(defaults_dir):
    _write_rp_defaults(defaults_dir)
    cfg = _single_cfg()
    admin.apply_resource_override(
        cfg,
        "b",
        {"uri": "file://a.txt", "override": {"name": "better", "enabled": False}},
    )
    admin.apply_prompt_override(
        cfg,
        "b",
        {
            "prompt_original": "p1",
            "override": {
                "name": "better_p1",
                "args": [{"original": "q", "description": "tuned"}],
            },
        },
    )
    bundle = admin.export_settings(cfg)
    fresh = _single_cfg()
    affected, errors = admin.import_settings(fresh, bundle, mode="replace")
    assert errors == [] and affected == ["b"]
    assert admin.export_settings(fresh) == bundle


def test_import_rejects_unknown_resource_and_prompt(defaults_dir):
    _write_rp_defaults(defaults_dir)
    bundle = {
        "kind": admin.EXPORT_KIND,
        "version": 1,
        "backends": {
            "b": {
                "resources": {"file://ghost.txt": {"name": "x"}},
                "prompts": {"ghost": {"name": "y"}},
            }
        },
    }
    _, errors = admin.import_settings(_single_cfg(), bundle)
    assert any("resource unknown" in e for e in errors)
    assert any("prompt unknown" in e for e in errors)


def test_hot_reload_swaps_resource_prompt_transform_too(defaults_dir):
    # holders must carry BOTH gateway-owned transforms and swap them all —
    # otherwise stale rp transforms pile up on the live proxy.
    import structlog
    from fastmcp.server import create_proxy

    _write_rp_defaults(defaults_dir)
    cfg = _single_cfg()
    admin.apply_resource_override(
        cfg, "b", {"uri": "file://a.txt", "override": {"name": "better"}}
    )
    b = cfg.backends[0]
    proxy = create_proxy(cl.to_proxy_config_one(b), name="mcp-gateway-b")
    baseline = len(proxy._transforms)
    registry, holders = {"b": proxy}, {}
    log = structlog.get_logger("test")
    admin.hot_reload(registry, holders, cfg, "b", log)
    assert len(holders["b"]) == 2  # tool transform + rp transform
    assert len(proxy._transforms) == baseline + 2
    # second reload replaces, never accumulates
    admin.hot_reload(registry, holders, cfg, "b", log)
    assert len(proxy._transforms) == baseline + 2
    # dropping the overrides drops the rp transform from the proxy
    cfg.backends[0].resources = []
    admin.hot_reload(registry, holders, cfg, "b", log)
    assert len(holders["b"]) == 1
    assert len(proxy._transforms) == baseline + 1


def test_refresh_defaults_flags_rp_only_changes(defaults_dir, monkeypatch):
    # a backend that only changes its prompts (same tools) must still count as
    # changed so the auto-refresh hot-reloads the transforms
    import asyncio as _asyncio

    _write_rp_defaults(defaults_dir)
    new_data = json.loads((defaults_dir / "b.json").read_text())
    new_data["prompts"][0]["description"] = "moved"

    async def fake_capture(b):
        return new_data

    monkeypatch.setattr(admin, "capture_defaults", fake_capture)
    b = cl.Backend(name="b", transport="stdio", command="/bin/x")
    res = _asyncio.run(
        admin.refresh_defaults(b, structlog.get_logger("test"), force=True)
    )
    assert res["status"] == "refreshed" and res["changed"] is True


# --- #157: age-gated post-mount baseline refresh ------------------------------


def _stamp_defaults(d, backend, captured_at):
    """Set (or add) the persisted ``captured_at`` on an existing defaults file."""
    p = d / f"{backend}.json"
    data = json.loads(p.read_text())
    data["captured_at"] = captured_at
    p.write_text(json.dumps(data))


def _counting_capture(tools=("t1",)):
    calls = []

    async def capture(b):
        calls.append(b.name)
        return await _fake_capture(tools)(b)

    return calls, capture


def test_refresh_defaults_age_gate_skips_fresh_baseline(defaults_dir, monkeypatch):
    _write_defaults(defaults_dir, "b", "t1")
    _stamp_defaults(defaults_dir, "b", time.time() - 10)
    calls, capture = _counting_capture(("t1", "t2"))
    monkeypatch.setattr(admin, "capture_defaults", capture)
    log = structlog.get_logger("test")
    res = anyio.run(lambda: admin.refresh_defaults(_b(), log, max_age=3600))
    assert res["status"] == "fresh"
    assert calls == []  # backend was never probed
    # the stored baseline is untouched
    assert {t["original"] for t in admin.load_defaults("b")["tools"]} == {"t1"}


def test_refresh_defaults_age_gate_refreshes_stale_baseline(defaults_dir, monkeypatch):
    _write_defaults(defaults_dir, "b", "t1")
    _stamp_defaults(defaults_dir, "b", time.time() - 7200)
    calls, capture = _counting_capture(("t1", "t2"))
    monkeypatch.setattr(admin, "capture_defaults", capture)
    log = structlog.get_logger("test")
    res = anyio.run(lambda: admin.refresh_defaults(_b(), log, max_age=3600))
    assert res["status"] == "refreshed" and calls == ["b"]


def test_refresh_defaults_age_gate_zero_is_ungated(defaults_dir, monkeypatch):
    # max_age=0 preserves the pre-#157 behavior: refresh even a seconds-old file
    _write_defaults(defaults_dir, "b", "t1")
    _stamp_defaults(defaults_dir, "b", time.time())
    calls, capture = _counting_capture()
    monkeypatch.setattr(admin, "capture_defaults", capture)
    log = structlog.get_logger("test")
    res = anyio.run(lambda: admin.refresh_defaults(_b(), log, max_age=0))
    assert res["status"] == "refreshed" and calls == ["b"]


@pytest.mark.parametrize("stamp", [None, "not-a-number", True])
def test_refresh_defaults_age_gate_treats_bad_stamp_as_stale(
    defaults_dir, monkeypatch, stamp
):
    # missing / non-numeric / bool captured_at (pre-#43 or hand-edited files)
    # must refresh, never skip
    _write_defaults(defaults_dir, "b", "t1")
    if stamp is not None:
        _stamp_defaults(defaults_dir, "b", stamp)
    calls, capture = _counting_capture()
    monkeypatch.setattr(admin, "capture_defaults", capture)
    log = structlog.get_logger("test")
    res = anyio.run(lambda: admin.refresh_defaults(_b(), log, max_age=10**9))
    assert res["status"] == "refreshed" and calls == ["b"]


def test_refresh_defaults_age_gate_future_stamp_is_stale(defaults_dir, monkeypatch):
    # a captured_at in the future (clock went backwards) must not skip forever
    _write_defaults(defaults_dir, "b", "t1")
    _stamp_defaults(defaults_dir, "b", time.time() + 9999)
    calls, capture = _counting_capture()
    monkeypatch.setattr(admin, "capture_defaults", capture)
    log = structlog.get_logger("test")
    res = anyio.run(lambda: admin.refresh_defaults(_b(), log, max_age=3600))
    assert res["status"] == "refreshed" and calls == ["b"]


def test_refresh_defaults_age_gate_missing_baseline_refreshes(
    defaults_dir, monkeypatch
):
    calls, capture = _counting_capture()
    monkeypatch.setattr(admin, "capture_defaults", capture)
    log = structlog.get_logger("test")
    res = anyio.run(lambda: admin.refresh_defaults(_b(), log, max_age=10**9))
    assert res["status"] == "refreshed" and calls == ["b"]


def test_refresh_defaults_force_bypasses_age_gate(defaults_dir, monkeypatch):
    _write_defaults(defaults_dir, "b", "t1")
    _stamp_defaults(defaults_dir, "b", time.time())
    calls, capture = _counting_capture()
    monkeypatch.setattr(admin, "capture_defaults", capture)
    log = structlog.get_logger("test")
    res = anyio.run(
        lambda: admin.refresh_defaults(_b(), log, force=True, max_age=10**9)
    )
    assert res["status"] == "refreshed" and calls == ["b"]


def test_refresh_defaults_fresh_skip_leaves_event_triggers_live(
    defaults_dir, monkeypatch
):
    # an age-gate skip must NOT stamp the in-process throttle: a subsequent
    # ungated (event-driven) trigger still refreshes immediately
    _write_defaults(defaults_dir, "b", "t1")
    _stamp_defaults(defaults_dir, "b", time.time())
    calls, capture = _counting_capture()
    monkeypatch.setattr(admin, "capture_defaults", capture)
    log = structlog.get_logger("test")

    async def go():
        gated = await admin.refresh_defaults(_b(), log, max_age=3600)
        event = await admin.refresh_defaults(_b(), log)  # e.g. admin page load
        return gated, event

    gated, event = anyio.run(go)
    assert gated["status"] == "fresh"
    assert event["status"] == "refreshed" and calls == ["b"]


# --- #157: orphan-sweep disjoint-config guard ---------------------------------


def test_sweep_orphan_refuses_majority_wipe(defaults_dir):
    # a scratch/test config sharing the real state dir must NOT wipe the real
    # baselines: >half orphaned -> loud refusal, nothing deleted
    for n in ("real1", "real2", "real3"):
        _write_defaults(defaults_dir, n, "t")
    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "scratch", "transport": "stdio", "command": "/bin/x"}]}
    )
    removed = admin.sweep_orphan_defaults(cfg, structlog.get_logger("test"))
    assert removed == []
    for n in ("real1", "real2", "real3"):
        assert (defaults_dir / f"{n}.json").exists()


def test_sweep_orphan_exactly_half_still_sweeps(defaults_dir):
    for n in ("keep1", "keep2", "gone1", "gone2"):
        _write_defaults(defaults_dir, n, "t")
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {"name": "keep1", "transport": "stdio", "command": "/bin/x"},
                {"name": "keep2", "transport": "stdio", "command": "/bin/x"},
            ]
        }
    )
    removed = admin.sweep_orphan_defaults(cfg, structlog.get_logger("test"))
    assert removed == ["gone1", "gone2"]
    assert not (defaults_dir / "gone1.json").exists()
    assert (defaults_dir / "keep1.json").exists()
