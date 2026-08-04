"""Non-Virtual surface control over the Admin API (#284).

Implements the dashboard's tool / resource / prompt / instructions surface as
scriptable commands:

- ``tool list|show|set|reset|run|migrate|discard``
- ``resource list|show|set|reset``
- ``prompt list|show|set|reset``
- ``instructions show|set|clear``

Every read comes from ``GET /admin/api/state``; every mutation posts the exact
payload the dashboard sends to its route, so renames, titles, enabled/pin
state, hidden parameters with injected defaults, parameter/argument overrides,
and the reset / migrate / discard semantics all match the UI (and the server's
#139 merge semantics: a key ABSENT from an override payload preserves the
stored value, empty strings inherit the default).

Complex payloads accept ``--file PATH`` or ``--file -`` (stdin) as JSON;
common scalar edits also get readable flags. Destructive reset/discard require
``--yes`` (no prompt, so non-TTY use is safe).
"""

from __future__ import annotations

import argparse
import json
from typing import Any, TextIO, cast
from urllib.parse import quote

from mcp_gateway.cli_common import (
    CLIContext,
    CLIError,
    expect_object,
    read_json_source,
    reject_unknown_fields,
    require_yes,
)

_STATE_PATH = "/admin/api/state"

# Sentinel for flags whose "cleared" value is None (e.g. --max-result-chars none).
_MISSING = object()

# Allowed keys in --file override JSON for tool/resource/prompt set — fail
# closed on typos instead of silently dropping them (the dashboard always
# sends exactly these; an unknown key would otherwise be ignored server-side).
_TOOL_OVERRIDE_FIELDS = frozenset(
    {
        "name",
        "title",
        "description",
        "enabled",
        "always_load",
        "max_result_chars",
        "params",
    }
)
_TOOL_PARAM_FIELDS = frozenset({"original", "name", "description", "hide", "default"})
_RESOURCE_OVERRIDE_FIELDS = frozenset({"name", "title", "description", "enabled"})
_PROMPT_OVERRIDE_FIELDS = frozenset({"name", "title", "description", "enabled", "args"})
_PROMPT_ARG_FIELDS = frozenset({"original", "description"})


# ---------------------------------------------------------------------------
# Shared state helpers
# ---------------------------------------------------------------------------


def _state(ctx: CLIContext) -> dict[str, Any]:
    return expect_object(
        ctx.client.request("GET", _STATE_PATH), "state endpoint response"
    )


def _find_backend(state: dict[str, Any], name: str) -> dict[str, Any]:
    for b in state.get("backends") or []:
        if b.get("name") == name:
            return b
    raise CLIError(f"unknown backend {name!r}")


def _check_backend_filter(state: dict[str, Any], backend: str | None) -> None:
    if backend is not None and not any(
        b.get("name") == backend for b in state.get("backends") or []
    ):
        raise CLIError(f"unknown backend {backend!r}")


def _find_tool(b: dict[str, Any], original: str) -> dict[str, Any] | None:
    return next(
        (t for t in b.get("tools") or [] if t.get("original") == original), None
    )


def _find_resource(b: dict[str, Any], uri: str) -> dict[str, Any] | None:
    return next((r for r in b.get("resources") or [] if r.get("uri") == uri), None)


def _find_prompt(b: dict[str, Any], original: str) -> dict[str, Any] | None:
    return next(
        (p for p in b.get("prompts") or [] if p.get("original") == original), None
    )


def _tool_ref_exists(b: dict[str, Any], original: str) -> bool:
    if _find_tool(b, original) is not None:
        return True
    return any(d.get("original") == original for d in b.get("dangling") or [])


def _missing_tool_msg(backend: str, original: str, b: dict[str, Any]) -> str:
    if any(d.get("original") == original for d in b.get("dangling") or []):
        return (
            f"tool {original!r} in backend {backend!r} is a dangling override "
            "(the backend renamed it upstream) — use 'tool migrate' or "
            "'tool discard'"
        )
    return (
        f"tool {original!r} not found in backend {backend!r} (not introspected "
        "or renamed away — re-inspect the backend first)"
    )


def _tool_is_overridden(t: dict[str, Any]) -> bool:
    return bool(
        t.get("name")
        or t.get("title")
        or t.get("description")
        or not t.get("enabled", True)
        or t.get("always_load")
        or t.get("max_result_chars") is not None
        or any(
            p.get("name")
            or p.get("description")
            or p.get("hide")
            or p.get("default") is not None
            for p in t.get("params") or []
        )
    )


def _resource_is_overridden(r: dict[str, Any]) -> bool:
    return bool(
        r.get("name")
        or r.get("title")
        or r.get("description")
        or not r.get("enabled", True)
    )


def _prompt_is_overridden(p: dict[str, Any]) -> bool:
    return bool(
        p.get("name")
        or p.get("title")
        or p.get("description")
        or not p.get("enabled", True)
        or any(a.get("description") for a in p.get("args") or [])
    )


def _parse_scalar(value: str) -> Any:
    """Typed value parsing like the dashboard's inputs: JSON-parseable values
    are sent typed (numbers, booleans, null), anything else as a raw string."""
    if isinstance(value, str):
        s = value.strip()
        if s == "":
            return ""
        try:
            return json.loads(s)
        except ValueError:
            return value
    return value


def _read_text(source: str, stdin: TextIO) -> str:
    """Read raw (non-JSON) text from a file or stdin for instructions values."""
    if source == "-":
        return stdin.read()
    try:
        with open(source, encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        raise CLIError(f"cannot read {source!r}: {exc}") from exc


def _max_chars(value: str) -> int | None:
    v = value.strip().lower()
    if v in ("", "none", "null"):
        return None
    try:
        return int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid --max-result-chars {value!r} (a positive integer or 'none')"
        ) from None


# ---------------------------------------------------------------------------
# argparse plumbing for grouped param/argument edits
# ---------------------------------------------------------------------------


def _append_edit_action(
    attr: str, current_attr: str, flag: str
) -> type[argparse.Action]:
    """Action for ``--param NAME`` / ``--arg NAME``: start a new edit entry.

    Later ``--param-*`` / ``--arg-*`` flags (also custom actions) mutate the
    most recent entry via ``current_attr`` on the namespace.
    """

    class _Append(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            if not isinstance(values, str) or not values:
                parser.error(f"{flag} requires a name")
            edits = getattr(namespace, attr, None)
            if not isinstance(edits, list):
                edits = []
                setattr(namespace, attr, edits)
            entry: dict[str, Any] = {"original": values}
            edits.append(entry)
            setattr(namespace, current_attr, entry)

    return _Append


def _current_edit_action(
    current_attr: str, field: str, const: Any = None
) -> type[argparse.Action]:
    """Action for a flag that edits the most recent ``--param``/``--arg`` entry."""

    class _Field(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            cur = getattr(namespace, current_attr, None)
            if cur is None:
                parser.error(f"{option_string} requires a preceding edit flag")
            # argparse Namespace attributes are dynamically typed; the value is
            # always the ``dict`` entry created by the ``--param``/``--arg``
            # action, so narrow it explicitly for the mutation below.
            cur = cast(dict[str, Any], cur)
            cur[field] = values if const is None else const

    return _Field


# ---------------------------------------------------------------------------
# Tool records / human formatting
# ---------------------------------------------------------------------------


def _tool_record(
    b: dict[str, Any], t: dict[str, Any], full: bool = False
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "backend": b.get("name"),
        "original": t.get("original"),
        "name": t.get("name"),
        "default_name": t.get("default_name"),
        "title": t.get("title"),
        "default_title": t.get("default_title"),
        "description": t.get("description"),
        "default_description": t.get("default_description"),
        "enabled": t.get("enabled", True),
        "always_load": t.get("always_load", False),
        "max_result_chars": t.get("max_result_chars"),
        "params": t.get("params") or [],
    }
    if full:
        for k in (
            "validate",
            "post_process",
            "hook_error",
            "output_schema",
            "meta",
            "annotations",
        ):
            rec[k] = t.get(k)
    return rec


def _tool_line(b: dict[str, Any], t: dict[str, Any]) -> str:
    name = t.get("name") or t.get("default_name") or t.get("original")
    flags = ["enabled" if t.get("enabled", True) else "disabled"]
    if t.get("always_load"):
        flags.append("pinned")
    if _tool_is_overridden(t):
        flags.append("overridden")
    return (
        f"{b.get('name'):<22} {t.get('original'):<26} → {name:<26} {', '.join(flags)}"
    )


def _tool_show_lines(b: dict[str, Any], t: dict[str, Any]) -> list[str]:
    name = t.get("name") or t.get("default_name") or t.get("original")
    lines = [f"backend:  {b.get('name')}", f"tool:     {t.get('original')} → {name}"]
    state = "enabled" if t.get("enabled", True) else "disabled"
    if t.get("always_load"):
        state += ", pinned"
    lines.append(f"state:    {state}")
    if t.get("title") or t.get("default_title"):
        marker = " (override)" if t.get("title") else " (default)"
        lines.append(f"title:    {t.get('title') or t.get('default_title')}{marker}")
    desc = t.get("description") or t.get("default_description")
    if desc:
        lines.append(f"description: {desc}")
    if t.get("max_result_chars") is not None:
        lines.append(f"max_result_chars: {t['max_result_chars']}")
    if t.get("validate") or t.get("post_process"):
        hooks = ", ".join(
            filter(
                None,
                [
                    f"validate={t['validate']!r}" if t.get("validate") else "",
                    f"post_process={t['post_process']!r}"
                    if t.get("post_process")
                    else "",
                ],
            )
        )
        lines.append(
            f"hooks:    {hooks}"
            + (f" (error: {t['hook_error']})" if t.get("hook_error") else "")
        )
    lines.append("params:")
    params = t.get("params") or []
    if not params:
        lines.append("  (none)")
    for p in params:
        bits = [p["original"]]
        if p.get("required"):
            bits.append("required")
        if p.get("name"):
            bits.append(f"→ {p['name']}")
        if p.get("hide"):
            bits.append("hidden")
        if p.get("default") is not None:
            bits.append(f"default={json.dumps(p['default'], sort_keys=True)}")
        pd = p.get("description") or p.get("default_description")
        lines.append("  " + " · ".join(bits) + (f" — {pd}" if pd else ""))
    return lines


# ---------------------------------------------------------------------------
# Tool param override merging (server #139 semantics)
# ---------------------------------------------------------------------------


def _stored_tool_params(t: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The stored param overrides of a tool, keyed by original, for merging."""
    out: dict[str, dict[str, Any]] = {}
    for p in t.get("params") or []:
        entry: dict[str, Any] = {"original": p["original"]}
        if p.get("name"):
            entry["name"] = p["name"]
        if p.get("description"):
            entry["description"] = p["description"]
        if p.get("hide"):
            entry["hide"] = True
        if p.get("default") is not None:
            entry["default"] = p["default"]
        out[p["original"]] = entry
    return out


def _param_is_empty(e: dict[str, Any]) -> bool:
    return not (
        e.get("name")
        or e.get("description")
        or e.get("hide")
        or e.get("default") not in (None, "")
    )


def _merged_tool_params(
    t: dict[str, Any],
    file_params: Any,
    edits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the ``override.params`` list.

    ``--file`` params replace the stored set wholesale (complete override);
    otherwise the stored param overrides are preserved (#139) so a one-param
    edit never wipes the others. ``--param`` edits merge field-wise by name.
    Fully-empty entries are pruned — the server drops them, which is exactly
    how a field override is cleared.
    """
    if file_params is not None:
        if not isinstance(file_params, list):
            raise CLIError("override 'params' must be a JSON array")
        merged: dict[str, dict[str, Any]] = {}
        for p in file_params:
            if (
                not isinstance(p, dict)
                or not isinstance(p.get("original"), str)
                or not p["original"]
            ):
                raise CLIError(
                    "each override param must be an object with an 'original' string"
                )
            reject_unknown_fields(
                p,
                _TOOL_PARAM_FIELDS,
                f"override param {p.get('original')!r}",
            )
            merged[p["original"]] = {
                k: p[k] for k in ("name", "description", "hide", "default") if k in p
            } | {"original": p["original"]}
    else:
        merged = _stored_tool_params(t)
    for e in edits:
        if "default" in e:
            e["default"] = _parse_scalar(e["default"])
        merged[e["original"]] = {**(merged.get(e["original"]) or {}), **e}
    out: list[dict[str, Any]] = []
    for e in merged.values():
        if _param_is_empty(e):
            continue
        out.append(
            {
                k: e[k]
                for k in ("original", "name", "description", "hide", "default")
                if k in e
            }
        )
    return out


# ---------------------------------------------------------------------------
# Tool commands
# ---------------------------------------------------------------------------


def _cmd_tool_list(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    _check_backend_filter(state, args.backend)
    backends = [
        b
        for b in state.get("backends") or []
        if args.backend is None or b.get("name") == args.backend
    ]
    if args.dangling:
        records: list[dict[str, Any]] = []
        lines: list[str] = []
        for b in backends:
            for d in b.get("dangling") or []:
                records.append(
                    {
                        "backend": b.get("name"),
                        "original": d.get("original"),
                        "name": d.get("name"),
                        "has_description": d.get("has_description"),
                        "enabled": d.get("enabled"),
                    }
                )
                line = (
                    f"{b.get('name'):<22} {d.get('original'):<26} "
                    f"→ {d.get('name') or d.get('original')}"
                )
                if d.get("has_description"):
                    line += " · tuned description"
                lines.append(line)
        ctx.emit(records, lines or ["(no dangling overrides)"])
        return
    records, lines = [], []
    for b in backends:
        for t in b.get("tools") or []:
            records.append(_tool_record(b, t))
            lines.append(_tool_line(b, t))
    ctx.emit(records, lines or ["(no tools)"])


def _cmd_tool_show(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    b = _find_backend(state, args.backend)
    t = _find_tool(b, args.original)
    if t is None:
        raise CLIError(_missing_tool_msg(args.backend, args.original, b))
    ctx.emit(_tool_record(b, t, full=True), _tool_show_lines(b, t))


def _apply_tool_scalars(ov: dict[str, Any], args: argparse.Namespace) -> None:
    """Apply the common scalar tool flags onto an override dict (present only).

    Absent flags leave the key out of the payload entirely, so the server's
    #139 merge semantics preserve the stored value for that field.
    """
    if args.name is not None:
        ov["name"] = args.name
    if args.title is not None:
        ov["title"] = args.title
    if args.description is not None:
        ov["description"] = args.description
    if args.enabled is not None:
        ov["enabled"] = args.enabled
    if args.always_load is not None:
        ov["always_load"] = args.always_load
    if args.max_result_chars is not _MISSING:
        ov["max_result_chars"] = args.max_result_chars


def _cmd_tool_set(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    b = _find_backend(state, args.backend)
    t = _find_tool(b, args.original)
    if t is None:
        raise CLIError(_missing_tool_msg(args.backend, args.original, b))
    ov: dict[str, Any] = {}
    file_params = None
    if args.file is not None:
        data = expect_object(
            read_json_source(args.file, stdin=ctx.stdin),
            f"override file {args.file!r}",
        )
        reject_unknown_fields(
            data, _TOOL_OVERRIDE_FIELDS, f"override file {args.file!r}"
        )
        ov.update({k: v for k, v in data.items() if k != "params"})
        file_params = data.get("params")
    _apply_tool_scalars(ov, args)
    edits = getattr(args, "_param_edits", None) or []
    for e in edits:
        if set(e) <= {"original"}:
            raise CLIError(
                f"--param {e['original']!r} has no effect — add at least one of "
                "--param-name/--param-desc/--param-default/--hide/--show"
            )
    if edits or file_params is not None:
        ov["params"] = _merged_tool_params(t, file_params, edits)
    if not ov:
        raise CLIError(
            "nothing to change — pass at least one field flag (--name/--title/"
            "--description/--enabled/--disabled/--pin/--unpin/"
            "--max-result-chars/--param) or --file"
        )
    payload: dict[str, Any] = {
        "backend": args.backend,
        "tool_original": args.original,
        "override": ov,
    }
    if args.auto_uniquify:
        payload["on_collision"] = "uniquify"
    res = expect_object(
        ctx.client.request("PUT", "/admin/api/override", payload=payload),
        "override response",
    )
    human = f"updated {args.original} on {args.backend}"
    if res.get("uniquified"):
        human += f" (name uniquified to {res.get('name')!r})"
    ctx.emit(res, human)


def _cmd_tool_reset(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    b = _find_backend(state, args.backend)
    if not _tool_ref_exists(b, args.original):
        raise CLIError(_missing_tool_msg(args.backend, args.original, b))
    require_yes(args, f"reset tool {args.original!r} on {args.backend!r} to defaults")
    res = ctx.client.request(
        "POST",
        "/admin/api/reset",
        payload={"backend": args.backend, "tool_original": args.original},
    )
    ctx.emit(res, f"reset {args.original} on {args.backend} to defaults")


def _resolve_run_name(b: dict[str, Any], name: str) -> str:
    """Resolve the tool to call: prefer an exact advertised-name match, then
    an original-name match (MCP clients call the broadcast name)."""
    for t in b.get("tools") or []:
        eff = t.get("name") or t.get("default_name") or t.get("original")
        if name == eff:
            return eff
    for t in b.get("tools") or []:
        if name == t.get("original"):
            return t.get("name") or t.get("default_name") or t.get("original")
    return name


def _cmd_tool_run(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    b = _find_backend(state, args.backend)
    tool_name = _resolve_run_name(b, args.tool)
    run_args: dict[str, Any] = {}
    if args.file is not None:
        data = expect_object(
            read_json_source(args.file, stdin=ctx.stdin),
            f"arguments file {args.file!r}",
        )
        run_args.update(data)
    for key, value in args.arg_pairs or []:
        run_args[key] = _parse_scalar(value)
    res = expect_object(
        ctx.client.request(
            "POST",
            "/admin/api/run",
            payload={
                "backend": args.backend,
                "tool": tool_name,
                "args": run_args,
            },
        ),
        "run response",
    )
    human: list[str] = []
    for blk in res.get("content") or []:
        if blk.get("type") == "text" and blk.get("text"):
            human.append(blk["text"])
    if not human and res.get("structured") is not None:
        human.append(json.dumps(res["structured"], indent=2, sort_keys=True))
    human.append(
        f"(tool error · {res.get('ms')} ms)"
        if res.get("is_error")
        else f"(ok · {res.get('ms')} ms)"
    )
    ctx.emit(res, human)
    if res.get("is_error"):
        raise CLIError(
            f"tool {tool_name!r} on backend {args.backend!r} reported a "
            "tool-level error (is_error=true) — see the result above"
        )


def _cmd_tool_migrate(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    b = _find_backend(state, args.backend)
    dangling = {d.get("original") for d in b.get("dangling") or []}
    if args.frm not in dangling:
        raise CLIError(
            f"no dangling override {args.frm!r} in backend {args.backend!r} — "
            "nothing to migrate"
        )
    t = _find_tool(b, args.to)
    if t is None:
        raise CLIError(
            f"cannot migrate to {args.to!r}: it is not a captured tool of backend "
            f"{args.backend!r} — re-inspect the backend, or pick its new tool name"
        )
    if _tool_is_overridden(t):
        raise CLIError(
            f"cannot migrate to {args.to!r}: it already has a stored override — "
            "reset it first"
        )
    res = expect_object(
        ctx.client.request(
            "POST",
            f"/admin/api/backend/{quote(args.backend, safe='')}/migrate-override",
            payload={"from": args.frm, "to": args.to},
        ),
        "migrate response",
    )
    human = f"migrated {args.frm} → {args.to} on {args.backend}"
    if res.get("carried_params"):
        human += " · carried: " + ", ".join(res["carried_params"])
    if res.get("dropped_params"):
        human += " · dropped: " + ", ".join(res["dropped_params"])
    ctx.emit(res, human)


def _cmd_tool_discard(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    b = _find_backend(state, args.backend)
    dangling = {d.get("original") for d in b.get("dangling") or []}
    if args.original not in dangling:
        raise CLIError(
            f"no dangling override {args.original!r} in backend {args.backend!r}"
        )
    require_yes(
        args,
        f"discard the stale override {args.original!r} on {args.backend!r} "
        "(its tuned text is removed)",
    )
    res = ctx.client.request(
        "POST",
        f"/admin/api/backend/{quote(args.backend, safe='')}/discard-override",
        payload={"original": args.original},
    )
    ctx.emit(res, f"discarded stale override {args.original} on {args.backend}")


# ---------------------------------------------------------------------------
# Resource commands
# ---------------------------------------------------------------------------


def _resource_record(b: dict[str, Any], r: dict[str, Any]) -> dict[str, Any]:
    return {
        "backend": b.get("name"),
        "uri": r.get("uri"),
        "template": bool(r.get("template")),
        "name": r.get("name"),
        "default_name": r.get("default_name"),
        "title": r.get("title"),
        "default_title": r.get("default_title"),
        "description": r.get("description"),
        "default_description": r.get("default_description"),
        "mime_type": r.get("mime_type"),
        "enabled": r.get("enabled", True),
    }


def _resource_line(b: dict[str, Any], r: dict[str, Any]) -> str:
    name = r.get("name") or r.get("default_name") or ""
    kind = "template" if r.get("template") else "resource"
    flags = ["enabled" if r.get("enabled", True) else "disabled"]
    if _resource_is_overridden(r):
        flags.append("overridden")
    return (
        f"{b.get('name'):<22} {r.get('uri'):<42} {kind:<9} {name:<24} "
        f"{', '.join(flags)}"
    )


def _resource_show_lines(b: dict[str, Any], r: dict[str, Any]) -> list[str]:
    lines = [
        f"backend:  {b.get('name')}",
        f"uri:      {r.get('uri')}",
        f"kind:     {'template' if r.get('template') else 'resource'}",
        f"state:    {'enabled' if r.get('enabled', True) else 'disabled'}",
    ]
    if r.get("name") or r.get("default_name"):
        marker = " (override)" if r.get("name") else " (default)"
        lines.append(f"name:     {r.get('name') or r.get('default_name')}{marker}")
    if r.get("title") or r.get("default_title"):
        marker = " (override)" if r.get("title") else " (default)"
        lines.append(f"title:    {r.get('title') or r.get('default_title')}{marker}")
    desc = r.get("description") or r.get("default_description")
    if desc:
        lines.append(f"description: {desc}")
    if r.get("mime_type"):
        lines.append(f"mime:     {r['mime_type']}")
    return lines


def _cmd_resource_list(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    _check_backend_filter(state, args.backend)
    records, lines = [], []
    for b in state.get("backends") or []:
        if args.backend is not None and b.get("name") != args.backend:
            continue
        for r in b.get("resources") or []:
            records.append(_resource_record(b, r))
            lines.append(_resource_line(b, r))
    ctx.emit(records, lines or ["(no resources)"])


def _cmd_resource_show(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    b = _find_backend(state, args.backend)
    r = _find_resource(b, args.uri)
    if r is None:
        raise CLIError(
            f"resource {args.uri!r} not found in backend {args.backend!r} "
            "(not introspected or renamed away)"
        )
    ctx.emit(_resource_record(b, r), _resource_show_lines(b, r))


def _cmd_resource_set(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    b = _find_backend(state, args.backend)
    if _find_resource(b, args.uri) is None:
        raise CLIError(
            f"resource {args.uri!r} not found in backend {args.backend!r} "
            "(not introspected or renamed away)"
        )
    ov: dict[str, Any] = {}
    if args.file is not None:
        data = expect_object(
            read_json_source(args.file, stdin=ctx.stdin),
            f"override file {args.file!r}",
        )
        reject_unknown_fields(
            data, _RESOURCE_OVERRIDE_FIELDS, f"override file {args.file!r}"
        )
        ov.update(data)
    if args.name is not None:
        ov["name"] = args.name
    if args.title is not None:
        ov["title"] = args.title
    if args.description is not None:
        ov["description"] = args.description
    if args.enabled is not None:
        ov["enabled"] = args.enabled
    if not ov:
        raise CLIError(
            "nothing to change — pass at least one field flag "
            "(--name/--title/--description/--enabled/--disabled) or --file"
        )
    res = ctx.client.request(
        "PUT",
        "/admin/api/resource-override",
        payload={"backend": args.backend, "uri": args.uri, "override": ov},
    )
    ctx.emit(res, f"updated resource {args.uri} on {args.backend}")


def _cmd_resource_reset(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    b = _find_backend(state, args.backend)
    if _find_resource(b, args.uri) is None:
        raise CLIError(
            f"resource {args.uri!r} not found in backend {args.backend!r} "
            "(not introspected or renamed away)"
        )
    require_yes(args, f"reset resource {args.uri!r} on {args.backend!r} to defaults")
    res = ctx.client.request(
        "POST",
        "/admin/api/resource-reset",
        payload={"backend": args.backend, "uri": args.uri},
    )
    ctx.emit(res, f"reset {args.uri} on {args.backend} to defaults")


# ---------------------------------------------------------------------------
# Prompt commands
# ---------------------------------------------------------------------------


def _prompt_record(b: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    return {
        "backend": b.get("name"),
        "original": p.get("original"),
        "name": p.get("name"),
        "default_name": p.get("default_name"),
        "title": p.get("title"),
        "default_title": p.get("default_title"),
        "description": p.get("description"),
        "default_description": p.get("default_description"),
        "enabled": p.get("enabled", True),
        "args": p.get("args") or [],
    }


def _prompt_line(b: dict[str, Any], p: dict[str, Any]) -> str:
    name = p.get("name") or p.get("default_name") or p.get("original")
    flags = ["enabled" if p.get("enabled", True) else "disabled"]
    if _prompt_is_overridden(p):
        flags.append("overridden")
    return (
        f"{b.get('name'):<22} {p.get('original'):<26} → {name:<26} {', '.join(flags)}"
    )


def _prompt_show_lines(b: dict[str, Any], p: dict[str, Any]) -> list[str]:
    name = p.get("name") or p.get("default_name") or p.get("original")
    lines = [
        f"backend:  {b.get('name')}",
        f"prompt:   {p.get('original')} → {name}",
        f"state:    {'enabled' if p.get('enabled', True) else 'disabled'}",
    ]
    if p.get("title") or p.get("default_title"):
        marker = " (override)" if p.get("title") else " (default)"
        lines.append(f"title:    {p.get('title') or p.get('default_title')}{marker}")
    desc = p.get("description") or p.get("default_description")
    if desc:
        lines.append(f"description: {desc}")
    lines.append("arguments:")
    args = p.get("args") or []
    if not args:
        lines.append("  (none)")
    for a in args:
        bits = [a["original"]]
        if a.get("required"):
            bits.append("required")
        ad = a.get("description") or a.get("default_description")
        lines.append("  " + " · ".join(bits) + (f" — {ad}" if ad else ""))
    return lines


def _stored_prompt_args(p: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for a in p.get("args") or []:
        if a.get("description"):
            out[a["original"]] = {
                "original": a["original"],
                "description": a["description"],
            }
    return out


def _merged_prompt_args(
    p: dict[str, Any],
    file_args: Any,
    edits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the ``override.args`` list (prompt-argument description overrides)."""
    if file_args is not None:
        if not isinstance(file_args, list):
            raise CLIError("override 'args' must be a JSON array")
        merged: dict[str, dict[str, Any]] = {}
        for a in file_args:
            if (
                not isinstance(a, dict)
                or not isinstance(a.get("original"), str)
                or not a["original"]
            ):
                raise CLIError(
                    "each override arg must be an object with an 'original' string"
                )
            reject_unknown_fields(
                a,
                _PROMPT_ARG_FIELDS,
                f"override arg {a.get('original')!r}",
            )
            merged[a["original"]] = {
                "original": a["original"],
                **({k: a[k] for k in ("description",) if k in a}),
            }
    else:
        merged = _stored_prompt_args(p)
    for e in edits:
        merged[e["original"]] = {**(merged.get(e["original"]) or {}), **e}
    return [e for e in merged.values() if e.get("description")]


def _cmd_prompt_list(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    _check_backend_filter(state, args.backend)
    records, lines = [], []
    for b in state.get("backends") or []:
        if args.backend is not None and b.get("name") != args.backend:
            continue
        for p in b.get("prompts") or []:
            records.append(_prompt_record(b, p))
            lines.append(_prompt_line(b, p))
    ctx.emit(records, lines or ["(no prompts)"])


def _cmd_prompt_show(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    b = _find_backend(state, args.backend)
    p = _find_prompt(b, args.original)
    if p is None:
        raise CLIError(
            f"prompt {args.original!r} not found in backend {args.backend!r} "
            "(not introspected or renamed away)"
        )
    ctx.emit(_prompt_record(b, p), _prompt_show_lines(b, p))


def _cmd_prompt_set(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    b = _find_backend(state, args.backend)
    p = _find_prompt(b, args.original)
    if p is None:
        raise CLIError(
            f"prompt {args.original!r} not found in backend {args.backend!r} "
            "(not introspected or renamed away)"
        )
    ov: dict[str, Any] = {}
    file_args = None
    if args.file is not None:
        data = expect_object(
            read_json_source(args.file, stdin=ctx.stdin),
            f"override file {args.file!r}",
        )
        reject_unknown_fields(
            data, _PROMPT_OVERRIDE_FIELDS, f"override file {args.file!r}"
        )
        ov.update({k: v for k, v in data.items() if k != "args"})
        file_args = data.get("args")
    if args.name is not None:
        ov["name"] = args.name
    if args.title is not None:
        ov["title"] = args.title
    if args.description is not None:
        ov["description"] = args.description
    if args.enabled is not None:
        ov["enabled"] = args.enabled
    edits = getattr(args, "_arg_edits", None) or []
    for e in edits:
        if set(e) <= {"original"}:
            raise CLIError(f"--arg {e['original']!r} has no effect — add --arg-desc")
    if edits or file_args is not None:
        ov["args"] = _merged_prompt_args(p, file_args, edits)
    if not ov:
        raise CLIError(
            "nothing to change — pass at least one field flag "
            "(--name/--title/--description/--enabled/--disabled/--arg) or --file"
        )
    res = ctx.client.request(
        "PUT",
        "/admin/api/prompt-override",
        payload={
            "backend": args.backend,
            "prompt_original": args.original,
            "override": ov,
        },
    )
    ctx.emit(res, f"updated prompt {args.original} on {args.backend}")


def _cmd_prompt_reset(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    b = _find_backend(state, args.backend)
    if _find_prompt(b, args.original) is None:
        raise CLIError(
            f"prompt {args.original!r} not found in backend {args.backend!r} "
            "(not introspected or renamed away)"
        )
    require_yes(args, f"reset prompt {args.original!r} on {args.backend!r} to defaults")
    res = ctx.client.request(
        "POST",
        "/admin/api/prompt-reset",
        payload={"backend": args.backend, "prompt_original": args.original},
    )
    ctx.emit(res, f"reset {args.original} on {args.backend} to defaults")


# ---------------------------------------------------------------------------
# Instructions commands
# ---------------------------------------------------------------------------


def _instructions_record(b: dict[str, Any]) -> dict[str, Any]:
    default = b.get("default_instructions")
    override = b.get("instructions")
    return {
        "backend": b.get("name"),
        "default_instructions": default,
        "instructions": override,
        "effective": override if override is not None else (default or ""),
    }


def _cmd_instructions_show(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    if args.backend is not None:
        b = _find_backend(state, args.backend)
        rec = _instructions_record(b)
        ctx.emit(rec, rec["effective"] or "(no instructions)")
        return
    records = [_instructions_record(b) for b in state.get("backends") or []]
    lines = []
    for rec in records:
        one = (rec["effective"] or "(none)").replace("\n", " ")[:72]
        lines.append(f"{rec['backend']:<22} {one}")
    ctx.emit(records, lines or ["(no backends)"])


def _cmd_instructions_set(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    _find_backend(state, args.backend)
    if args.value is not None and args.file is not None:
        raise CLIError(
            "give the instructions either positionally or with --file, not both"
        )
    if args.file is not None:
        value = _read_text(args.file, ctx.stdin)
    elif args.value is not None:
        value = args.value
    else:
        raise CLIError(
            "missing instructions value — pass it positionally or with --file"
        )
    res = ctx.client.request(
        "PUT",
        "/admin/api/instructions",
        payload={"backend": args.backend, "value": value},
    )
    ctx.emit(res, f"updated instructions for {args.backend}")


def _cmd_instructions_clear(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    _find_backend(state, args.backend)
    res = ctx.client.request(
        "PUT",
        "/admin/api/instructions",
        payload={"backend": args.backend, "value": ""},
    )
    ctx.emit(
        res,
        "cleared instructions override for "
        f"{args.backend} (inherits the server original)",
    )


# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------


def _add_common_edits(parser: argparse.ArgumentParser, *, args_help: str) -> None:
    """Flags shared by tool/resource/prompt set commands."""
    parser.add_argument("--file", metavar="PATH|-", help=args_help)
    parser.add_argument(
        "--name", metavar="NAME", help="broadcast name (empty clears the rename)"
    )
    parser.add_argument("--title", metavar="TITLE", help="broadcast title")
    parser.add_argument("--description", metavar="TEXT", help="broadcast description")
    g = parser.add_mutually_exclusive_group()
    g.add_argument(
        "--enabled",
        dest="enabled",
        action="store_true",
        default=None,
        help="broadcast this item",
    )
    g.add_argument(
        "--disabled",
        dest="enabled",
        action="store_false",
        default=None,
        help="stop broadcasting this item",
    )


def _register_tool_commands(subparsers) -> None:
    p = subparsers.add_parser(
        "tool",
        help=(
            "inspect and control MCP tools (rename, title, description, "
            "enable/pin, params, run, migrate, discard)"
        ),
    )
    sp = p.add_subparsers(dest="tool_command", required=True, metavar="COMMAND")

    def leaf(name: str, help: str, handler) -> argparse.ArgumentParser:
        lp = sp.add_parser(name, help=help)
        lp.set_defaults(handler=handler)
        return lp

    lp = leaf(
        "list",
        "list tools (optionally for one backend, or dangling overrides)",
        _cmd_tool_list,
    )
    lp.add_argument("--backend", metavar="NAME", help="only list tools of this backend")
    lp.add_argument(
        "--dangling",
        action="store_true",
        help=(
            "list dangling overrides (tools the backend renamed away) "
            "instead of live tools"
        ),
    )

    lp = leaf("show", "show one tool's effective configuration", _cmd_tool_show)
    lp.add_argument("backend", help="backend name")
    lp.add_argument(
        "original", metavar="TOOL", help="tool original (provider-facing) name"
    )

    lp = leaf("set", "update a tool's broadcast override", _cmd_tool_set)
    lp.add_argument("backend", help="backend name")
    lp.add_argument(
        "original", metavar="TOOL", help="tool original (provider-facing) name"
    )
    _add_common_edits(
        lp,
        args_help=(
            "complete override as a JSON object "
            "(name/title/description/enabled/always_load/max_result_chars/params); "
            "'-' reads stdin; flags override same-named keys"
        ),
    )
    g = lp.add_mutually_exclusive_group()
    g.add_argument(
        "--pin",
        "--always-load",
        dest="always_load",
        action="store_true",
        default=None,
        help="pin: load this tool upfront (alwaysLoad)",
    )
    g.add_argument(
        "--unpin",
        dest="always_load",
        action="store_false",
        default=None,
        help="unpin (tool-search deferral)",
    )
    lp.add_argument(
        "--max-result-chars",
        type=_max_chars,
        default=_MISSING,
        metavar="N|none",
        help="per-tool output cap in chars; 'none' clears the cap",
    )
    lp.add_argument(
        "--auto-uniquify",
        action="store_true",
        help=(
            "on a rename collision, auto-suffix the broadcast name "
            "(_2, _3, ...) instead of failing"
        ),
    )
    lp.add_argument(
        "--param",
        metavar="NAME",
        action=_append_edit_action("_param_edits", "_current_param", "--param"),
        help=(
            "edit one parameter (repeatable); following "
            "--param-name/--param-desc/--param-default/--hide/--show apply to "
            "the most recent one"
        ),
    )
    lp.add_argument(
        "--param-name",
        metavar="NAME",
        action=_current_edit_action("_current_param", "name"),
        help="broadcast rename for the active --param",
    )
    lp.add_argument(
        "--param-desc",
        metavar="TEXT",
        action=_current_edit_action("_current_param", "description"),
        help="description for the active --param",
    )
    lp.add_argument(
        "--param-default",
        metavar="VALUE",
        action=_current_edit_action("_current_param", "default"),
        help=(
            "injected fixed value for the active --param (JSON or plain "
            "text; '' or null clears)"
        ),
    )
    lp.add_argument(
        "--hide",
        action=_current_edit_action("_current_param", "hide", const=True),
        nargs=0,
        help=(
            "hide the active --param from MCP clients (required params need "
            "an injected default)"
        ),
    )
    lp.add_argument(
        "--show",
        action=_current_edit_action("_current_param", "hide", const=False),
        nargs=0,
        help="stop hiding the active --param",
    )

    lp = leaf(
        "reset",
        "clear all overrides for one tool (revert to the backend default)",
        _cmd_tool_reset,
    )
    lp.add_argument("backend", help="backend name")
    lp.add_argument(
        "original", metavar="TOOL", help="tool original (provider-facing) name"
    )
    lp.add_argument("--yes", action="store_true", help="confirm the destructive reset")

    lp = leaf(
        "run",
        "execute a tool through the live proxy (like the dashboard inspector)",
        _cmd_tool_run,
    )
    lp.add_argument("backend", help="backend name")
    lp.add_argument(
        "tool", metavar="TOOL", help="tool to run (original or broadcast name)"
    )
    lp.add_argument(
        "--arg",
        metavar=("KEY", "VALUE"),
        action="append",
        nargs=2,
        dest="arg_pairs",
        help="tool argument (repeatable); values are JSON-typed when parseable",
    )
    lp.add_argument(
        "--file",
        metavar="PATH|-",
        help=(
            "complete arguments object as JSON; '-' reads stdin "
            "(--arg flags merge on top)"
        ),
    )

    lp = leaf(
        "migrate",
        "carry a dangling override's tuned text onto a tool's new original",
        _cmd_tool_migrate,
    )
    lp.add_argument("backend", help="backend name")
    lp.add_argument("frm", metavar="FROM", help="dangling override original to migrate")
    lp.add_argument("to", metavar="TO", help="captured tool that receives the override")

    lp = leaf(
        "discard",
        "drop a dangling override (its tuned text no longer applies)",
        _cmd_tool_discard,
    )
    lp.add_argument("backend", help="backend name")
    lp.add_argument(
        "original", metavar="TOOL", help="dangling override original to discard"
    )
    lp.add_argument(
        "--yes", action="store_true", help="confirm the destructive discard"
    )


def _register_resource_commands(subparsers) -> None:
    p = subparsers.add_parser(
        "resource",
        help="inspect and control MCP resources and templates",
    )
    sp = p.add_subparsers(dest="resource_command", required=True, metavar="COMMAND")

    def leaf(name: str, help: str, handler) -> argparse.ArgumentParser:
        lp = sp.add_parser(name, help=help)
        lp.set_defaults(handler=handler)
        return lp

    lp = leaf(
        "list",
        "list resources and templates (optionally for one backend)",
        _cmd_resource_list,
    )
    lp.add_argument(
        "--backend", metavar="NAME", help="only list resources of this backend"
    )

    lp = leaf("show", "show one resource's effective configuration", _cmd_resource_show)
    lp.add_argument("backend", help="backend name")
    lp.add_argument("uri", metavar="URI", help="resource URI or template uriTemplate")

    lp = leaf("set", "update a resource's broadcast override", _cmd_resource_set)
    lp.add_argument("backend", help="backend name")
    lp.add_argument("uri", metavar="URI", help="resource URI or template uriTemplate")
    _add_common_edits(
        lp,
        args_help=(
            "complete override as a JSON object "
            "(name/title/description/enabled); '-' reads stdin; "
            "flags override same-named keys"
        ),
    )

    lp = leaf(
        "reset",
        "clear all overrides for one resource (revert to default)",
        _cmd_resource_reset,
    )
    lp.add_argument("backend", help="backend name")
    lp.add_argument("uri", metavar="URI", help="resource URI or template uriTemplate")
    lp.add_argument("--yes", action="store_true", help="confirm the destructive reset")


def _register_prompt_commands(subparsers) -> None:
    p = subparsers.add_parser(
        "prompt",
        help="inspect and control MCP prompts",
    )
    sp = p.add_subparsers(dest="prompt_command", required=True, metavar="COMMAND")

    def leaf(name: str, help: str, handler) -> argparse.ArgumentParser:
        lp = sp.add_parser(name, help=help)
        lp.set_defaults(handler=handler)
        return lp

    lp = leaf("list", "list prompts (optionally for one backend)", _cmd_prompt_list)
    lp.add_argument(
        "--backend", metavar="NAME", help="only list prompts of this backend"
    )

    lp = leaf("show", "show one prompt's effective configuration", _cmd_prompt_show)
    lp.add_argument("backend", help="backend name")
    lp.add_argument(
        "original", metavar="PROMPT", help="prompt original (provider-facing) name"
    )

    lp = leaf("set", "update a prompt's broadcast override", _cmd_prompt_set)
    lp.add_argument("backend", help="backend name")
    lp.add_argument(
        "original", metavar="PROMPT", help="prompt original (provider-facing) name"
    )
    _add_common_edits(
        lp,
        args_help=(
            "complete override as a JSON object "
            "(name/title/description/enabled/args); '-' reads stdin; "
            "flags override same-named keys"
        ),
    )
    lp.add_argument(
        "--arg",
        metavar="NAME",
        action=_append_edit_action("_arg_edits", "_current_arg", "--arg"),
        help=(
            "edit one argument (repeatable); following --arg-desc applies "
            "to the most recent one"
        ),
    )
    lp.add_argument(
        "--arg-desc",
        metavar="TEXT",
        action=_current_edit_action("_current_arg", "description"),
        help="description for the active --arg",
    )

    lp = leaf(
        "reset",
        "clear all overrides for one prompt (revert to default)",
        _cmd_prompt_reset,
    )
    lp.add_argument("backend", help="backend name")
    lp.add_argument(
        "original", metavar="PROMPT", help="prompt original (provider-facing) name"
    )
    lp.add_argument("--yes", action="store_true", help="confirm the destructive reset")


def _register_instructions_commands(subparsers) -> None:
    p = subparsers.add_parser(
        "instructions",
        help="per-backend server-instructions override",
    )
    sp = p.add_subparsers(dest="instructions_command", required=True, metavar="COMMAND")

    def leaf(name: str, help: str, handler) -> argparse.ArgumentParser:
        lp = sp.add_parser(name, help=help)
        lp.set_defaults(handler=handler)
        return lp

    lp = leaf(
        "show", "show instructions (all backends, or one)", _cmd_instructions_show
    )
    lp.add_argument(
        "backend", nargs="?", metavar="BACKEND", help="backend name (omit for all)"
    )

    lp = leaf(
        "set", "set a backend's server-instructions override", _cmd_instructions_set
    )
    lp.add_argument("backend", help="backend name")
    lp.add_argument(
        "value",
        nargs="?",
        metavar="TEXT",
        help=(
            "instructions text (empty inherits the server original); "
            "alternatively use --file"
        ),
    )
    lp.add_argument(
        "--file",
        metavar="PATH|-",
        help="read the instructions text from a file or stdin ('-')",
    )

    lp = leaf(
        "clear",
        "clear a backend's instructions override (inherit the server original)",
        _cmd_instructions_clear,
    )
    lp.add_argument("backend", help="backend name")


def register_surface_commands(subparsers) -> None:
    """Register the tool / resource / prompt / instructions command trees."""
    _register_tool_commands(subparsers)
    _register_resource_commands(subparsers)
    _register_prompt_commands(subparsers)
    _register_instructions_commands(subparsers)
