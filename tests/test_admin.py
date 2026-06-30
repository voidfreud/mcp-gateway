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


# --- apply_tool_override (needs a defaults file) ---------------------------


@pytest.fixture
def defaults_dir(tmp_path, monkeypatch):
    d = tmp_path / "defaults"
    d.mkdir()
    monkeypatch.setattr(admin, "DEFAULTS_DIR", d)
    return d


def _write_defaults(d, backend, tool, desc="orig desc", params=None):
    (d / f"{backend}.json").write_text(
        json.dumps(
            {
                "backend": backend,
                "tools": [
                    {
                        "original": tool,
                        "title": None,
                        "description": desc,
                        "params": params or [],
                    }
                ],
            }
        )
    )


def _single_cfg(backend="b", tool="t"):
    return cl.GatewayConfig.model_validate(
        {"backends": [{"name": backend, "transport": "stdio", "command": "/bin/x"}]}
    )


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
