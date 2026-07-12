"""Tests for admin pure logic — override-vs-default diffing, name validation,
apply_tool_override (store iff differs), and build_state merge."""

from __future__ import annotations

import json
import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

import admin
import config_loader as cl

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


def test_collision_ignored_when_other_tool_disabled(defaults_dir):
    _write_defaults_multi(defaults_dir, "b", [("t1", "d1"), ("t2", "d2")])
    # t2 is disabled -> not broadcast -> t1 may take its name
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
