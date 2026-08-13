"""Virtual Tool CLI commands: draft lifecycle, catalog, validation and tests.

Every Virtual Tool dashboard operation from ``admin_routes_virtual.py`` has a
counterpart here: listing the live tools with their resolution receipts
(``list``/``show``), browsing the backend source-tool catalog used by the
editor (``catalog``), draft creation/replacement/deletion (``create``/
``update``/``delete``), resolution and execution checks (``validate``/``test``),
and the draft-vs-active lifecycle transitions (``activate``/``disable``).

Definitions are sent exactly as the dashboard sends them: a full JSON object
(the ``VirtualTool`` config model) via ``--file`` (or ``-`` for stdin), with
scalar flags overlaying common fields and repeated ``--input``/``--member``
JSON items replacing the members/inputs lists.  ``update`` merges over the
current definition so a single flag (e.g. ``--description``) is enough for a
common scalar edit; the server always stores the result as an inactive draft.
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
from typing import Any

from mcp_gateway.cli_common import (
    LIMIT_MAX_BYTES,
    UNSET,
    CLIContext,
    CLIError,
    expect_object,
    limit_flag_type,
    limit_human,
    read_json_source,
    require_yes,
)

_VIRTUAL_TOOLS = "/admin/api/virtual-tools"
_VIRTUAL_CATALOG = "/admin/api/virtual-catalog"

# Field names the server returns on top of the VirtualTool definition when
# listing (resolution receipts, live status).  They are not part of the
# wire-validated model (extra="forbid") and must be stripped before a
# definition is PUT back.
_DEFINITION_KEYS = (
    "name",
    "description",
    "always_load",
    "dispatch",
    "inputs",
    "members",
    "router",
    "routing_input_max_chars",
    "max_result_bytes",
    # #286: UTF-8 description cap; null = inherit the gateway global.
    "description_max_bytes",
    "failure_policy",
)


def _tool_path(name: str) -> str:
    return f"{_VIRTUAL_TOOLS}/{urllib.parse.quote(name, safe='')}"


def _json_object(value: str, label: str) -> dict:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise CLIError(f"{label} must be valid JSON: {exc}") from None
    if not isinstance(parsed, dict):
        raise CLIError(f"{label} must be a JSON object")
    return parsed


def _json_scalar(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _fetch_tool(ctx: CLIContext, name: str) -> dict:
    data = expect_object(
        ctx.client.request("GET", _VIRTUAL_TOOLS), "virtual-tools response"
    )
    tools = data.get("tools")
    if not isinstance(tools, list):
        raise CLIError("virtual-tools response: 'tools' must be a list")
    for tool in tools:
        if tool.get("name") == name:
            return tool
    raise CLIError(f"unknown virtual tool {name!r}")


def _strip_definition(tool: dict) -> dict:
    """Return the wire-valid VirtualTool fields of a listed tool.

    The list endpoint decorates each definition with ``resolution`` (top level
    and per member) plus live status keys; those are rejected by the config
    model (``extra="forbid"``), so they are dropped before reuse as an update
    base.  ``enabled`` is left out too: the server always stores drafts.
    """

    definition = {key: tool[key] for key in _DEFINITION_KEYS if key in tool}
    for member in definition.get("members") or []:
        member.pop("resolution", None)
    return definition


def _has_definition_flags(args: argparse.Namespace) -> bool:
    # ``description_max_bytes`` defaults to the UNSET sentinel (an explicit
    # ``inherit`` is ``None``), so it is checked separately.
    if getattr(args, "description_max_bytes", UNSET) is not UNSET:
        return True
    return any(
        getattr(args, name) is not None
        for name in (
            "tool_name",
            "description",
            "dispatch",
            "always_load",
            "max_result_bytes",
            "failure_policy",
            "routing_input_max_chars",
            "inputs",
            "members",
            "router_model",
            "router_api_key",
            "router_conditions",
            "router_timeout",
            "router_fallback",
            "router_egress_acknowledged",
        )
    )


_SCALAR_FIELDS: tuple[tuple[str, str], ...] = (
    ("tool_name", "name"),
    ("description", "description"),
    ("dispatch", "dispatch"),
    ("always_load", "always_load"),
    ("max_result_bytes", "max_result_bytes"),
    ("failure_policy", "failure_policy"),
    ("routing_input_max_chars", "routing_input_max_chars"),
)


def _overlay_scalars(args: argparse.Namespace, payload: dict[str, Any]) -> None:
    """Overlay the readable scalar flags onto the definition payload."""
    for attr, key in _SCALAR_FIELDS:
        value = getattr(args, attr)
        if value is not None:
            payload[key] = value


def _apply_router_flags(args: argparse.Namespace, payload: dict[str, Any]) -> None:
    """Overlay the ``--router-*`` flags, creating the router object on demand."""
    flags: dict[str, Any] = {
        "model": args.router_model,
        "api_key": args.router_api_key,
        "conditions": args.router_conditions,
        "timeout": args.router_timeout,
        "fallback": args.router_fallback,
    }
    if args.router_egress_acknowledged is not None:
        flags["egress_acknowledged"] = args.router_egress_acknowledged
    if not any(value is not None for value in flags.values()):
        return
    router = payload.get("router")
    if router is None:
        router = {}
        payload["router"] = router
    elif not isinstance(router, dict):
        raise CLIError("--router-* flags need a JSON object router in the payload")
    router.update({key: value for key, value in flags.items() if value is not None})


def _definition_payload(
    args: argparse.Namespace, ctx: CLIContext, *, base: dict | None = None
) -> dict:
    """Assemble the definition object sent to POST/PUT.

    ``--file`` (or ``-``) supplies the full JSON definition; scalar flags
    overlay individual fields on top; repeated ``--input``/``--member`` items
    replace the corresponding lists wholesale (predictable, since entries
    cannot be addressed individually).
    """

    payload: dict[str, Any] = dict(base) if base is not None else {}
    if args.file is not None:
        loaded = expect_object(
            read_json_source(args.file, stdin=ctx.stdin), "virtual tool definition"
        )
        payload.update(loaded)
    _overlay_scalars(args, payload)
    # Scoped by hand (not via _SCALAR_FIELDS): ``inherit`` is None, which the
    # generic overlay skips but here must still be sent (null) to clear a cap.
    if getattr(args, "description_max_bytes", UNSET) is not UNSET:
        payload["description_max_bytes"] = args.description_max_bytes
    if args.inputs is not None:
        payload["inputs"] = [
            _json_object(item, f"--input {item!r}") for item in args.inputs
        ]
    if args.members is not None:
        payload["members"] = [
            _json_object(item, f"--member {item!r}") for item in args.members
        ]
    _apply_router_flags(args, payload)
    if not payload:
        raise CLIError(
            "no virtual tool definition given: pass --file (or '-') "
            "or at least one flag"
        )
    return payload


def _test_arguments(args: argparse.Namespace, ctx: CLIContext) -> dict:
    arguments: dict[str, Any] = {}
    if args.arguments is not None:
        loaded = expect_object(
            read_json_source(args.arguments, stdin=ctx.stdin), "test arguments"
        )
        arguments.update(loaded)
    for item in args.arg_pairs or []:
        if "=" not in item:
            raise CLIError(f"--arg expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise CLIError(f"--arg expects KEY=VALUE, got {item!r}")
        arguments[key] = _json_scalar(value)
    return arguments


def _resolution_summary(tool: dict) -> str:
    resolution = tool.get("resolution") or {}
    if resolution.get("ok") is False:
        errors = resolution.get("errors") or []
        detail = errors[0] if errors else "unresolved"
        if len(detail) > 64:
            detail = f"{detail[:61]}..."
        return f"error: {detail}"
    if resolution.get("ok") is True:
        return "ok"
    return "unknown"


def _human_list(data: dict) -> list[str]:
    mounted = data.get("mounted")
    endpoint = data.get("endpoint", "/virtual/mcp")
    tools = data.get("tools") or []
    lines = [f"Virtual tools at {endpoint} (mounted: {'yes' if mounted else 'no'})"]
    if not tools:
        lines.append("  (none)")
        return lines
    lines.append(
        f"  {'NAME':<26} {'STATE':<7} {'DISPATCH':<8} {'MEMBERS':<8} RESOLUTION"
    )
    for tool in tools:
        name = tool.get("name", "?")
        state = "active" if tool.get("enabled") else "draft"
        dispatch = tool.get("dispatch", "all")
        members = len(tool.get("members") or [])
        lines.append(
            f"  {name:<26} {state:<7} {dispatch:<8} {members:<8} "
            f"{_resolution_summary(tool)}"
        )
    return lines


def _human_show_inputs(inputs: list[dict]) -> list[str]:
    lines = []
    for item in inputs:
        required = "required" if item.get("required", True) else "optional"
        default = item.get("default")
        suffix = f", default {json.dumps(default)}" if default is not None else ""
        lines.append(
            f"                 - {item.get('name', '?')} "
            f"({item.get('type', 'string')}, {required}{suffix})"
        )
    return lines


def _member_summary(item: dict) -> str:
    """Render one member line: label, effective binding, live resolution."""
    label = item.get("label") or (
        f"{item.get('backend_id')}/{item.get('tool_original')}"
    )
    resolution = item.get("resolution") or {}
    if resolution.get("resolved") is False:
        return f"{label}  [error: {resolution.get('error', 'unresolved')}]"
    backend = (
        resolution.get("backend_effective")
        or resolution.get("backend")
        or item.get("backend_id", "?")
    )
    effective = resolution.get("tool_effective") or item.get("tool_original", "?")
    detail = []
    if item.get("args"):
        detail.append(f"args={json.dumps(item['args'], sort_keys=True)}")
    if item.get("static_args"):
        detail.append(f"static={json.dumps(item['static_args'], sort_keys=True)}")
    if item.get("timeout") not in (None, 30.0):
        detail.append(f"timeout={item['timeout']}s")
    if item.get("route_patterns"):
        detail.append(f"routes={len(item['route_patterns'])}")
    suffix = f"  {', '.join(detail)}" if detail else ""
    return f"{label} -> {backend}/{effective}  [ok]{suffix}"


def _human_show_members(members: list[dict]) -> list[str]:
    return [f"                 - {_member_summary(item)}" for item in members]


def _router_summary(router: dict) -> str:
    acknowledged = "yes" if router.get("egress_acknowledged") else "no"
    return (
        f"model={router.get('model', '')} "
        f"api_key={router.get('api_key') or '(none)'} "
        f"timeout={router.get('timeout')}s "
        f"fallback={router.get('fallback', 'all')} "
        f"egress_acknowledged={acknowledged}"
    )


def _human_show(tool: dict) -> list[str]:
    state = "active" if tool.get("enabled") else "draft"
    lines = [
        f"Name:            {tool.get('name', '?')}",
        f"State:           {state}",
        f"Dispatch:        {tool.get('dispatch', 'all')}",
        f"Always load:     {'yes' if tool.get('always_load') else 'no'}",
        f"Description:     {tool.get('description', '')}",
    ]
    inputs = tool.get("inputs") or []
    if inputs:
        lines.append(f"Inputs:          {len(inputs)}")
        lines.extend(_human_show_inputs(inputs))
    else:
        lines.append("Inputs:          (none)")
    members = tool.get("members") or []
    if members:
        lines.append(f"Members:         {len(members)}")
        lines.extend(_human_show_members(members))
    else:
        lines.append("Members:         (none)")
    # #286: stored/effective description cap and current byte count.
    if (
        tool.get("description_max_bytes") is not None
        or tool.get("effective_description_max_bytes") is not None
        or tool.get("description_bytes") is not None
    ):
        cap = limit_human(
            tool.get("description_max_bytes"),
            tool.get("effective_description_max_bytes"),
        )
        lines.append(f"Description limit: {cap}")
        dbytes = tool.get("description_bytes")
        if dbytes is not None:
            lines.append(f"Description bytes: {dbytes}")
    router = tool.get("router")
    if router:
        lines.append(f"Router:          {_router_summary(router)}")
        if router.get("conditions"):
            lines.append(f"                 conditions: {router['conditions']}")
        if router.get("egress_consent_fingerprint"):
            lines.append(
                f"                 egress_consent_fingerprint: "
                f"{router['egress_consent_fingerprint']}"
            )
    else:
        lines.append("Router:          (none)")
    lines.append(
        f"Budget:          max_result_bytes={tool.get('max_result_bytes', 262144)} "
        f"failure_policy={tool.get('failure_policy', 'partial')} "
        f"routing_input_max_chars={tool.get('routing_input_max_chars', 4096)}"
    )
    resolution = tool.get("resolution") or {}
    if resolution.get("ok") is False:
        lines.append("Resolution:      failed")
        lines.extend(
            f"                 - {error}" for error in (resolution.get("errors") or [])
        )
    else:
        lines.append(f"Resolution:      {'ok' if resolution.get('ok') else 'unknown'}")
    return lines


def _human_catalog(data: dict) -> list[str]:
    backends = data.get("backends") or []
    lines = ["Backend catalog (source tools for virtual members)"]
    if not backends:
        lines.append("  (no backends)")
        return lines
    for backend in backends:
        name = backend.get("name", "?")
        effective = backend.get("effective_name") or name
        state = "enabled" if backend.get("enabled") else "disabled"
        tools = backend.get("tools") or []
        lines.append(f"  {name} ({effective}) [{state}] {len(tools)} tools")
        for tool in tools:
            original = tool.get("original", "?")
            effective_name = tool.get("effective_name") or original
            mark = "on" if tool.get("enabled") else "off"
            params = len(tool.get("params") or [])
            renamed = f" -> {effective_name}" if effective_name != original else ""
            lines.append(f"    {original}{renamed} [{mark}] {params} params")
    return lines


def _human_validate_ok(name: str, resolution: dict) -> list[str]:
    lines = [f"virtual tool {name!r} resolved and valid"]
    for member in resolution.get("members") or []:
        if member.get("resolved"):
            backend = (
                member.get("backend_effective")
                or member.get("backend")
                or member.get("backend_id", "?")
            )
            lines.append(
                f"  {member.get('label', '?')} -> {backend}/"
                f"{member.get('tool_effective', '?')} [ok]"
            )
        else:
            lines.append(
                f"  {member.get('label', '?')} "
                f"[error: {member.get('error', 'unresolved')}]"
            )
    return lines


def _human_validate_failed(name: str, body: dict) -> str:
    errors = body.get("errors")
    if errors:
        detail = "\n".join(f"  - {error}" for error in errors)
        return f"virtual tool {name!r} validation failed:\n{detail}"
    message = body.get("error")
    if message:
        return f"virtual tool {name!r} validation failed: {message}"
    return f"virtual tool {name!r} validation failed"


def _human_test(name: str, result: dict) -> list[str]:
    ok = result.get("ok")
    last = result.get("last_test") or {}
    ms = last.get("ms")
    ms_text = f" · {ms} ms" if ms is not None else ""
    lines = [f"virtual tool {name!r} test {'passed' if ok else 'failed'}{ms_text}"]
    payload = result.get("result") or {}
    structured = payload.get("structured") or payload.get("structured_content") or {}
    selected = structured.get("selected")
    if selected:
        lines.append(f"  members: {', '.join(str(item) for item in selected)}")
    members = structured.get("members")
    if members:
        for member in members:
            if member.get("status") == "ok":
                continue
            lines.append(
                f"  - {member.get('member', '?')}: {member.get('error') or 'failed'}"
            )
    elif not ok:
        lines.append(f"  error: {_result_error(payload)}")
    return lines


def _result_error(payload: dict) -> str:
    content = payload.get("content") or []
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and block.get("text")
        ):
            return block["text"]
    error = payload.get("error") or payload.get("isError") or payload.get("is_error")
    if error is not None:
        return str(error)
    return "tool returned an error"


def _human_test_failed(name: str, body: dict) -> str:
    errors = body.get("errors")
    if errors:
        detail = "\n".join(f"  - {error}" for error in errors)
        return f"virtual tool {name!r} test failed:\n{detail}"
    message = body.get("error")
    if message:
        return f"virtual tool {name!r} test failed: {message}"
    return f"virtual tool {name!r} test failed"


def _resolution_blocked(name: str, action: str, body: dict) -> str:
    errors = body.get("errors")
    if errors:
        detail = "\n".join(f"  - {error}" for error in errors)
        return f"{action} of {name!r} blocked:\n{detail}"
    message = body.get("error")
    if message:
        return f"{action} of {name!r} blocked: {message}"
    return f"{action} of {name!r} blocked"


def _cmd_virtual_list(args: argparse.Namespace, ctx: CLIContext) -> None:
    data = expect_object(
        ctx.client.request("GET", _VIRTUAL_TOOLS), "virtual-tools response"
    )
    ctx.emit(data, _human_list(data))


def _cmd_virtual_show(args: argparse.Namespace, ctx: CLIContext) -> None:
    tool = _fetch_tool(ctx, args.name)
    ctx.emit(tool, _human_show(tool))


def _cmd_virtual_catalog(args: argparse.Namespace, ctx: CLIContext) -> None:
    data = expect_object(
        ctx.client.request("GET", _VIRTUAL_CATALOG), "virtual-catalog response"
    )
    ctx.emit(data, _human_catalog(data))


def _cmd_virtual_create(args: argparse.Namespace, ctx: CLIContext) -> None:
    payload = _definition_payload(args, ctx)
    result = expect_object(
        ctx.client.request("POST", _VIRTUAL_TOOLS, payload=payload),
        "create response",
    )
    tool = result.get("tool")
    if not isinstance(tool, dict):
        tool = {}
    name = tool.get("name") or payload.get("name", "?")
    ctx.emit(result, f"created virtual tool {name!r} as draft")


def _cmd_virtual_update(args: argparse.Namespace, ctx: CLIContext) -> None:
    current = _fetch_tool(ctx, args.name)
    if args.file is None and not _has_definition_flags(args):
        raise CLIError(
            f"nothing to change for virtual tool {args.name!r}: "
            "pass --file or at least one flag"
        )
    payload = _definition_payload(args, ctx, base=_strip_definition(current))
    result = expect_object(
        ctx.client.request("PUT", _tool_path(args.name), payload=payload),
        "update response",
    )
    tool = result.get("tool")
    if not isinstance(tool, dict):
        tool = {}
    name = tool.get("name") or payload.get("name", args.name)
    ctx.emit(
        result,
        f"updated virtual tool {name!r} as draft "
        "(activate to put it back into service)",
    )


def _cmd_virtual_delete(args: argparse.Namespace, ctx: CLIContext) -> None:
    require_yes(args, f"delete virtual tool {args.name!r}")
    result = ctx.client.request("DELETE", _tool_path(args.name))
    ctx.emit(result, f"deleted virtual tool {args.name!r}")


def _cmd_virtual_validate(args: argparse.Namespace, ctx: CLIContext) -> None:
    path = _tool_path(args.name) + "/validate"
    try:
        resolution = expect_object(
            ctx.client.request("POST", path), "validate response"
        )
    except CLIError as exc:
        body = getattr(exc, "response", None)
        if isinstance(body, dict) and body.get("ok") is False:
            raise CLIError(_human_validate_failed(args.name, body)) from None
        raise
    ctx.emit(resolution, _human_validate_ok(args.name, resolution))


def _cmd_virtual_test(args: argparse.Namespace, ctx: CLIContext) -> None:
    arguments = _test_arguments(args, ctx)
    path = _tool_path(args.name) + "/test"
    try:
        result = expect_object(
            ctx.client.request("POST", path, payload={"arguments": arguments}),
            "test response",
        )
    except CLIError as exc:
        body = getattr(exc, "response", None)
        if isinstance(body, dict) and body.get("ok") is False:
            raise CLIError(_human_test_failed(args.name, body)) from None
        raise
    ctx.emit(result, _human_test(args.name, result))
    if result.get("ok") is False:
        # The test endpoint returns 200 when the tool ran but produced an
        # error result; emit already printed the full receipt, so automation
        # still gets a nonzero exit with a concise stderr line.
        raise CLIError(f"virtual tool {args.name!r} test failed")


def _cmd_virtual_activate(args: argparse.Namespace, ctx: CLIContext) -> None:
    path = _tool_path(args.name) + "/activate"
    try:
        result = expect_object(ctx.client.request("POST", path), "activate response")
    except CLIError as exc:
        body = getattr(exc, "response", None)
        if isinstance(body, dict) and body.get("ok") is False:
            raise CLIError(_resolution_blocked(args.name, "activation", body)) from None
        raise
    ctx.emit(result, f"activated virtual tool {args.name!r} (hot reload)")


def _cmd_virtual_disable(args: argparse.Namespace, ctx: CLIContext) -> None:
    path = _tool_path(args.name) + "/disable"
    result = expect_object(ctx.client.request("POST", path), "disable response")
    ctx.emit(result, f"disabled virtual tool {args.name!r} (hot reload)")


def _add_definition_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--file",
        "-f",
        metavar="PATH",
        help="full JSON definition of the tool; '-' reads stdin",
    )
    parser.add_argument(
        "--name",
        dest="tool_name",
        metavar="NAME",
        help="tool name (rename on update)",
    )
    parser.add_argument("--description", metavar="TEXT", help="tool description")
    parser.add_argument(
        "--dispatch",
        choices=("all", "keyword", "llm"),
        help="member selection mode",
    )
    parser.add_argument(
        "--always-load",
        dest="always_load",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="load this tool upfront at mount time (--no-always-load to clear)",
    )
    parser.add_argument(
        "--max-result-bytes", type=int, metavar="BYTES", help="output budget"
    )
    parser.add_argument(
        "--failure-policy",
        choices=("partial", "strict"),
        help="failure policy for member results",
    )
    parser.add_argument(
        "--routing-input-max-chars",
        type=int,
        metavar="CHARS",
        help="routing text character limit",
    )
    parser.add_argument(
        "--description-max-bytes",
        type=limit_flag_type("inherit", "description_max_bytes"),
        default=UNSET,
        metavar="N|inherit",
        help=(
            f"description cap in UTF-8 bytes (1..{LIMIT_MAX_BYTES}); "
            "'inherit' follows the gateway-global tool description cap "
            "(itself 'unlimited' by default)"
        ),
    )
    parser.add_argument(
        "--input",
        dest="inputs",
        action="append",
        metavar="JSON",
        help="one input definition (repeatable; replaces the payload's inputs)",
    )
    parser.add_argument(
        "--member",
        dest="members",
        action="append",
        metavar="JSON",
        help="one member binding (repeatable; replaces the payload's members)",
    )
    parser.add_argument("--router-model", metavar="MODEL", help="LLM router model id")
    parser.add_argument(
        "--router-api-key",
        metavar="ENV_REF",
        help="LLM router api key as a ${ENV_VAR} reference",
    )
    parser.add_argument(
        "--router-conditions",
        metavar="TEXT",
        help="LLM router selection instructions",
    )
    parser.add_argument(
        "--router-timeout",
        type=float,
        metavar="SECONDS",
        help="LLM router timeout",
    )
    parser.add_argument(
        "--router-fallback",
        metavar="LABEL",
        help="LLM router fallback member label",
    )
    parser.add_argument(
        "--router-egress-acknowledged",
        dest="router_egress_acknowledged",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="acknowledge external-routing egress consent "
        "(--no-router-egress-acknowledged to revoke)",
    )


def register_virtual_commands(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``virtual`` command group on the root subparsers."""

    parser = subparsers.add_parser("virtual", help="manage gateway-owned Virtual Tools")
    commands = parser.add_subparsers(
        dest="virtual_command", metavar="COMMAND", required=True
    )

    cmd = commands.add_parser("list", help="list Virtual Tools with resolution status")
    cmd.set_defaults(handler=_cmd_virtual_list)

    cmd = commands.add_parser(
        "show", help="show one Virtual Tool definition and resolution"
    )
    cmd.add_argument("name", help="virtual tool name")
    cmd.set_defaults(handler=_cmd_virtual_show)

    cmd = commands.add_parser(
        "catalog", help="list backend source tools usable in virtual members"
    )
    cmd.set_defaults(handler=_cmd_virtual_catalog)

    cmd = commands.add_parser(
        "create", help="create a new Virtual Tool draft (POST a full definition)"
    )
    _add_definition_flags(cmd)
    cmd.set_defaults(handler=_cmd_virtual_create)

    cmd = commands.add_parser(
        "update",
        help="replace a Virtual Tool definition; always stored as an inactive draft",
    )
    cmd.add_argument("name", help="current virtual tool name")
    _add_definition_flags(cmd)
    cmd.set_defaults(handler=_cmd_virtual_update)

    cmd = commands.add_parser("delete", help="delete a Virtual Tool (requires --yes)")
    cmd.add_argument("name", help="virtual tool name")
    cmd.add_argument("--yes", action="store_true", help="confirm deletion")
    cmd.set_defaults(handler=_cmd_virtual_delete)

    cmd = commands.add_parser(
        "validate", help="resolve a Virtual Tool against the live backends"
    )
    cmd.add_argument("name", help="virtual tool name")
    cmd.set_defaults(handler=_cmd_virtual_validate)

    cmd = commands.add_parser(
        "test", help="run a Virtual Tool once with test arguments"
    )
    cmd.add_argument("name", help="virtual tool name")
    cmd.add_argument(
        "--arguments",
        "-a",
        metavar="FILE",
        help="JSON object of arguments; '-' reads stdin",
    )
    cmd.add_argument(
        "--arg",
        dest="arg_pairs",
        action="append",
        metavar="KEY=VALUE",
        help="one argument (repeatable; values are parsed as JSON when possible)",
    )
    cmd.set_defaults(handler=_cmd_virtual_test)

    cmd = commands.add_parser(
        "activate", help="activate a Virtual Tool draft (hot reload)"
    )
    cmd.add_argument("name", help="virtual tool name")
    cmd.set_defaults(handler=_cmd_virtual_activate)

    cmd = commands.add_parser(
        "disable", help="disable an active Virtual Tool (hot reload)"
    )
    cmd.add_argument("name", help="virtual tool name")
    cmd.set_defaults(handler=_cmd_virtual_disable)
