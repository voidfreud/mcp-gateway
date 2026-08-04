"""Backend lifecycle and topology CLI commands (issue #284).

Every command talks only to the existing Admin API: the per-backend routes in
``admin_routes_backend`` (pin / enabled / stateless / display-name / rename /
add / remove), the operational ``/admin/api/introspect`` and
``/admin/api/refresh`` sweep in ``admin_routes_ops``, plus ``/admin/api/state``
and ``/admin/api/status`` for read views. No config-file writes happen here,
and no secrets are resolved or printed: ``--auth-value``, ``--env`` and
``--header`` values must be safe templates — exactly ``${VAR}`` or
``Bearer|Basic|Token ${VAR}`` (never a literal or a raw/ref mix, matching
``credential_policy.is_safe_credential_value``); ``--env-literal``/
``--header-literal`` carry clearly non-secret literals but reject
credential-like names; the merged ``--file`` payload is validated against
the exact add fields (unknown keys rejected) with the same template rule,
headers classified by ``credential_policy.is_credential_like_key`` and env by
``credential_policy.is_credential_like_env_key`` (credential-STORE location keys
like ``PASSWORD_FILE``/``TOKEN_CACHE_DIR`` exempt); and ``show`` never
echoes ``auth_value`` (``--json`` included) — only whether one is
configured.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import quote

from mcp_gateway.cli_common import (
    CLIContext,
    CLIError,
    expect_object,
    read_json_source,
    reject_unknown_fields,
    require_yes,
)
from mcp_gateway.credential_policy import (
    is_credential_like_env_key,
    is_credential_like_key,
    is_safe_credential_value,
)

# Same contract the server enforces for backend names (admin.py ``_NAME_RE``
# and config_loader.RESERVED_BACKEND_NAMES): fail fast client-side so a bad
# name never half-builds a payload.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RESERVED_NAMES = frozenset({"virtual", "admin", "health", "ready"})
_TRANSPORTS = ("http", "streamable-http", "sse", "stdio")
_HTTP_TRANSPORTS = ("http", "streamable-http", "sse")

# Exact fields the add route accepts (admin_routes_backend.add_backend plus
# the #284 env passthrough): any other key in a merged ``--file`` payload is
# rejected before the request goes out.
_BACKEND_ADD_FIELDS = frozenset(
    {
        "name",
        "transport",
        "url",
        "command",
        "args",
        "env",
        "auth_header",
        "auth_value",
        "headers",
        "auth",
        "headers_helper",
        "stateless",
        "init_timeout",
        "request_timeout",
    }
)


def _validate_name(value: Any) -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise CLIError(
            f"invalid backend name {value!r}: use only letters, digits, '_' "
            "or '-' (max 64 chars)"
        )
    if value in _RESERVED_NAMES:
        raise CLIError(f"backend name {value!r} is reserved")
    return value


def _secret_env_ref(value: str, what: str) -> str | None:
    """Validate a secret-bearing flag value: empty clears it, otherwise it
    must be a safe template — exactly ``${VAR}`` or ``Bearer|Basic|Token
    ${VAR}`` — never a literal or a raw/ref mix. The value is never echoed."""
    stripped = value.strip()
    if not stripped:
        return None
    if not is_safe_credential_value(stripped):
        raise CLIError(
            f"{what} must be exactly a ${{VAR}} or "
            "'Bearer|Basic|Token ${VAR}' template (never mixed with raw text)"
        )
    return stripped


def _state(ctx: CLIContext) -> dict[str, Any]:
    return expect_object(
        ctx.client.request("GET", "/admin/api/state"), "state response"
    )


def _backends(state: Mapping[str, Any]) -> list[Any]:
    backends = state.get("backends")
    if not isinstance(backends, list):
        raise CLIError("unexpected state response: 'backends' must be a list")
    return backends


def _find_backend(state: Mapping[str, Any], name: str) -> dict[str, Any]:
    for b in _backends(state):
        if isinstance(b, dict) and b.get("name") == name:
            return b
    raise CLIError(f"unknown backend {name!r}")


def _status_map(ctx: CLIContext) -> dict[str, Any]:
    """Live per-backend probe results (#23); empty on failure, like the
    dashboard, so a read view never dies because one probe hiccupped."""
    try:
        resp = expect_object(
            ctx.client.request("GET", "/admin/api/status"), "status response"
        )
    except CLIError:
        return {}
    backends = resp.get("backends")
    if not isinstance(backends, dict):
        return {}
    return backends


def _status_human(entry: Mapping[str, Any] | None) -> str:
    if not entry:
        return "n/a"
    state = entry.get("state")
    if state == "ok":
        parts = ["ok"]
        if entry.get("tools") is not None:
            parts.append(f"{entry['tools']} tools")
        if entry.get("ms") is not None:
            parts.append(f"{entry['ms']}ms")
        return ", ".join(parts)
    if state == "error":
        return f"error: {entry.get('error', 'unknown')}"
    return str(state)


def _list_field(b: Mapping[str, Any], key: str) -> list[Any]:
    value = b.get(key)
    return value if isinstance(value, list) else []


def _enabled_tools(b: Mapping[str, Any]) -> int:
    tools = _list_field(b, "tools")
    return sum(1 for t in tools if isinstance(t, dict) and t.get("enabled", True))


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append(
            "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        )
    return lines


# ---------------------------------------------------------------------------
# Read views
# ---------------------------------------------------------------------------


def _cmd_list(args: argparse.Namespace, ctx: CLIContext) -> None:
    del args  # unused
    state = _state(ctx)
    status = _status_map(ctx)
    payload = []
    rows = []
    for b in _backends(state):
        if not isinstance(b, dict):
            continue
        entry = status.get(b.get("name"))
        if not isinstance(entry, dict):
            entry = {}
        tools = len(_list_field(b, "tools"))
        payload.append(
            {
                "name": b.get("name"),
                "display_name": b.get("display_name"),
                "endpoint": b.get("endpoint"),
                "transport": b.get("transport"),
                "enabled": b.get("enabled"),
                "stateless": b.get("stateless"),
                "always_load": b.get("always_load"),
                "introspected": b.get("introspected"),
                "tools": tools,
                "status": entry or None,
            }
        )
        rows.append(
            (
                b.get("name") or "?",
                b.get("transport") or "?",
                entry.get("state", "n/a"),
                f"{_enabled_tools(b)}/{tools}" if b.get("introspected") else "-",
                "stateless" if b.get("stateless") else "warm",
            )
        )
    human = _table(["NAME", "TRANSPORT", "STATE", "TOOLS", "SESSION"], rows)
    if not rows:
        human = ["(no backends)"]
    ctx.emit(payload, human)


def _cmd_show(args: argparse.Namespace, ctx: CLIContext) -> None:
    state = _state(ctx)
    b = _find_backend(state, args.name)
    entry = _status_map(ctx).get(args.name)
    if not isinstance(entry, dict):
        entry = {}
    payload = dict(b)
    # Never echo the stored auth value (it may be an inline secret); report
    # only whether one is configured.
    payload.pop("auth_value", None)
    payload["auth_value_set"] = bool(b.get("auth_value"))
    payload["status"] = entry or None

    lines = [
        f"name: {b.get('name')}",
        f"display_name: {b.get('display_name') or '(none)'}",
        f"endpoint: {b.get('endpoint')}",
        f"transport: {b.get('transport')}",
    ]
    if b.get("transport") == "stdio":
        command = " ".join([b.get("command") or "", *_list_field(b, "args")]).rstrip()
        lines.append(f"command: {command or '(none)'}")
    else:
        lines.append(f"url: {b.get('url') or '(none)'}")
    if b.get("auth_header"):
        lines.append(f"auth_header: {b['auth_header']}")
        lines.append(f"auth_value: {'(set)' if b.get('auth_value') else '(unset)'}")
    lines += [
        f"enabled: {'yes' if b.get('enabled') else 'no'}",
        f"session: {'stateless' if b.get('stateless') else 'warm'}",
        f"pinned: {'yes' if b.get('always_load') else 'no'}",
        f"introspected: {'yes' if b.get('introspected') else 'no'}",
        f"status: {_status_human(entry)}",
    ]
    if b.get("server_info"):
        lines.append(f"server_info: {b['server_info']}")
    tools = _list_field(b, "tools")
    lines.append(f"tools: {_enabled_tools(b)}/{len(tools)}")
    lines.append(f"resources: {len(_list_field(b, 'resources'))}")
    lines.append(f"prompts: {len(_list_field(b, 'prompts'))}")
    dangling = _list_field(b, "dangling")
    if dangling:
        lines.append(f"dangling overrides: {', '.join(map(str, dangling))}")
    ctx.emit(payload, lines)


# ---------------------------------------------------------------------------
# add / remove / rename
# ---------------------------------------------------------------------------


def _parse_headers_helper(value: str) -> str | list[str]:
    stripped = value.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise CLIError(f"invalid --headers-helper JSON list: {exc}") from exc
        if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
            raise CLIError("--headers-helper JSON must be a list of strings (argv)")
        return parsed
    return stripped


# Scalar ``--flag`` -> payload field pairs for ``backend add``; a None flag
# leaves the field untouched (so ``--file`` values survive unless overridden).
# ``auth_value`` is handled separately: it must be a ${ENV_VAR} reference.
_ADD_SCALAR_FIELDS = (
    ("name", "name"),
    ("transport", "transport"),
    # ``--backend-url`` (never ``--url``: that is a global Admin API option).
    ("backend_url", "url"),
    ("command", "command"),
    ("auth_header", "auth_header"),
    ("auth", "auth"),
    ("init_timeout", "init_timeout"),
    ("request_timeout", "request_timeout"),
)


def _add_file_payload(args: argparse.Namespace, ctx: CLIContext) -> dict[str, Any]:
    """Base payload from ``--file`` (an empty dict when omitted)."""
    if args.file is None:
        return {}
    raw = read_json_source(args.file, stdin=ctx.stdin)
    if not isinstance(raw, dict):
        raise CLIError("--file payload must be a JSON object")
    # JSON object keys are always strings.
    return {str(key): value for key, value in raw.items()}


class _EnvHeaderAction(argparse.Action):
    """Collect ``(literal, raw)`` entries for one name/value family in argv
    order, so later flags override earlier ones for the same key (duplicate-
    last merge across ``--env``/``--env-literal`` and
    ``--header``/``--header-literal``)."""

    def __init__(
        self,
        option_strings,
        dest,
        literal: bool = False,
        **kwargs,
    ):
        super().__init__(option_strings, dest, **kwargs)
        self.literal = literal

    def __call__(self, parser, namespace, values, option_string=None):
        entries = getattr(namespace, self.dest, None)
        if entries is None:
            entries = []
            setattr(namespace, self.dest, entries)
        entries.append((self.literal, values))


def _merged_headers(
    payload: Mapping[str, Any], entries: list[tuple[bool, str]]
) -> dict[str, str]:
    """Merge header entries over the payload's ``headers``, last write wins.
    ``--header`` values must be safe templates (exactly ``${VAR}`` or
    ``Bearer|Basic|Token ${VAR}``); ``--header-literal`` values may be plain
    literals but reject credential-like names. Values are never echoed."""
    headers = dict(payload.get("headers") or {})
    for literal, raw in entries:
        name, sep, value = raw.partition(":")
        if not sep or not name.strip():
            shown = name.strip() or "?"
            raise CLIError(f"invalid --header entry {shown!r}: expected NAME:VALUE")
        name = name.strip()
        value = value.strip()
        if literal:
            if is_credential_like_key(name):
                raise CLIError(
                    f"--header-literal {name} cannot carry a credential-like "
                    "name (use --header with a ${VAR} template)"
                )
        elif not is_safe_credential_value(value):
            raise CLIError(
                f"--header {name} must be exactly a ${{VAR}} or "
                "'Bearer|Basic|Token ${VAR}' template "
                "(never mixed with raw text)"
            )
        headers[name] = value
    return headers


def _merged_env(
    payload: Mapping[str, Any], entries: list[tuple[bool, str]]
) -> dict[str, str]:
    """Merge env entries over the payload's ``env``, last write wins.
    ``--env`` values must be safe templates (exactly ``${VAR}`` or
    ``Bearer|Basic|Token ${VAR}``); ``--env-literal`` values may be plain
    literals but reject credential-like names. Values are never echoed."""
    env = dict(payload.get("env") or {})
    for literal, raw in entries:
        key, sep, value = raw.partition("=")
        if not sep or not key.strip():
            shown = key.strip() or "?"
            raise CLIError(f"invalid --env entry {shown!r}: expected NAME=VALUE")
        key = key.strip()
        value = value.strip()
        if literal:
            if is_credential_like_env_key(key):
                raise CLIError(
                    f"--env-literal {key} cannot carry a credential-like "
                    "name (use --env with a ${VAR} template)"
                )
        elif not is_safe_credential_value(value):
            raise CLIError(
                f"--env {key} must be exactly a ${{VAR}} or "
                "'Bearer|Basic|Token ${VAR}' template "
                "(never mixed with raw text)"
            )
        env[key] = value
    return env


def _add_payload(args: argparse.Namespace, ctx: CLIContext) -> dict[str, Any]:
    """Build the add payload: ``--file`` base overlaid with explicit flags."""
    payload = _add_file_payload(args, ctx)
    for attr, field in _ADD_SCALAR_FIELDS:
        value = getattr(args, attr)
        if value is not None:
            payload[field] = value
    if args.auth_value is not None:
        payload["auth_value"] = _secret_env_ref(args.auth_value, "--auth-value")
    if args.arg:
        payload["args"] = list(args.arg)
    if args.header_entries:
        payload["headers"] = _merged_headers(payload, args.header_entries)
    if args.headers_helper is not None:
        payload["headers_helper"] = _parse_headers_helper(args.headers_helper)
    if args.env_entries:
        payload["env"] = _merged_env(payload, args.env_entries)
    if args.stateless is not None:
        payload["stateless"] = args.stateless
    return payload


def _check_string_mapping(
    payload: Mapping[str, Any],
    field: str,
    what: str,
    is_credential_key: Callable[[str], bool],
) -> None:
    """Reject non-string mappings and raw credentials under credential-like
    keys in a headers/env field of the merged add payload (covers ``--file``
    and literal flags). Headers pass ``is_credential_like_key``; env passes
    ``is_credential_like_env_key`` (credential-store path keys exempt).
    Errors name the key, never the value."""
    raw = payload.get(field)
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise CLIError(f"backend {what} must be a JSON object of string values")
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise CLIError(f"backend {what} must map names to string values")
        if is_credential_key(key) and not is_safe_credential_value(value):
            raise CLIError(
                f"backend {what} {key!r} must be exactly a ${{VAR}} or "
                "'Bearer|Basic|Token ${VAR}' template "
                "(never mixed with raw text)"
            )


def _check_secret_fields(payload: Mapping[str, Any]) -> None:
    """Validate the merged payload's secret-bearing fields: ``auth_value``
    must reference an ``${ENV_VAR}``, and headers/env must be string mappings
    with no raw credentials under credential-like keys."""
    auth_value = payload.get("auth_value")
    if auth_value is not None:
        if not isinstance(auth_value, str):
            raise CLIError("backend auth_value must be a string")
        if auth_value.strip() and not is_safe_credential_value(auth_value.strip()):
            raise CLIError(
                "backend auth_value must be exactly a ${VAR} or "
                "'Bearer|Basic|Token ${VAR}' template "
                "(never mixed with raw text)"
            )
    _check_string_mapping(payload, "headers", "headers", is_credential_like_key)
    _check_string_mapping(payload, "env", "env", is_credential_like_env_key)


def _validate_add_payload(payload: Mapping[str, Any]) -> str:
    """Validate the merged payload; returns the backend name."""
    reject_unknown_fields(payload, _BACKEND_ADD_FIELDS, "backend add payload")
    if payload.get("name") is None:
        raise CLIError("backend name is required (positional argument or --file)")
    name = _validate_name(payload["name"])
    transport = payload.get("transport")
    if transport not in _TRANSPORTS:
        raise CLIError(f"--transport is required: one of {', '.join(_TRANSPORTS)}")
    if transport in _HTTP_TRANSPORTS and not payload.get("url"):
        raise CLIError(f"--backend-url is required for {transport} backends")
    if transport == "stdio" and not payload.get("command"):
        raise CLIError("--command is required for stdio backends")
    if "args" in payload and not isinstance(payload["args"], list):
        raise CLIError("args must be a JSON array")
    _check_secret_fields(payload)
    return name


def _add_human(resp: Mapping[str, Any], name: str) -> str:
    reloaded = resp.get("reloaded", "")
    if reloaded == "hot-add":
        return f"added backend '{name}' (mounted live)"
    if reloaded == "mount-failed":
        return f"added backend '{name}' (configured, but mount failed)"
    if reloaded == "restarting":
        return f"added backend '{name}' (gateway restart scheduled)"
    if reloaded == "dev-no-restart":
        return f"added backend '{name}' (restart needed — dev/foreground daemon)"
    return f"added backend '{name}'"


def _cmd_add(args: argparse.Namespace, ctx: CLIContext) -> None:
    payload = _add_payload(args, ctx)
    name = _validate_add_payload(payload)
    resp = expect_object(
        ctx.client.request("POST", "/admin/api/backend", payload=payload),
        "add response",
    )
    ctx.emit(resp, _add_human(resp, resp.get("backend") or name))


def _cmd_remove(args: argparse.Namespace, ctx: CLIContext) -> None:
    require_yes(args, f"remove backend {args.name!r} (restarts the gateway)")
    resp = expect_object(
        ctx.client.request("DELETE", f"/admin/api/backend/{quote(args.name)}"),
        "remove response",
    )
    reloaded = resp.get("reloaded", "")
    if reloaded == "restarting":
        human = f"removed backend '{args.name}' (gateway restart scheduled)"
    elif reloaded == "dev-no-restart":
        human = (
            f"removed backend '{args.name}' "
            "(config updated; restart needed — dev/foreground daemon)"
        )
    else:
        human = f"removed backend '{args.name}'"
    ctx.emit(resp, human)


def _cmd_rename(args: argparse.Namespace, ctx: CLIContext) -> None:
    new_name = _validate_name(args.new_name)
    resp = expect_object(
        ctx.client.request(
            "POST",
            f"/admin/api/backend/{quote(args.name)}/rename",
            payload={"value": new_name},
        ),
        "rename response",
    )
    lines = [
        f"renamed '{args.name}' -> '{new_name}'",
        f"endpoint: {resp.get('old_endpoint', '?')} -> {resp.get('new_endpoint', '?')}",
        f"registration: {resp.get('old_registration', '?')} -> "
        f"{resp.get('new_registration', '?')}",
        f"reloaded: {resp.get('reloaded', '?')}",
    ]
    ctx.emit(resp, lines)


def _cmd_display_name(args: argparse.Namespace, ctx: CLIContext) -> None:
    """Set or clear a backend's display-only label (#42), mirroring the
    dashboard's on-blur save (empty value clears)."""
    if args.clear and args.value is not None:
        raise CLIError("--clear cannot be combined with a display name VALUE")
    value = "" if args.clear else args.value
    if value is None:
        raise CLIError("specify a display name VALUE, or --clear to remove it")
    resp = expect_object(
        ctx.client.request(
            "POST",
            f"/admin/api/backend/{quote(args.name)}/display-name",
            payload={"value": value},
        ),
        "display-name response",
    )
    if value:
        human = f"display name of '{args.name}' set to {value!r}"
    else:
        human = f"display name of '{args.name}' cleared"
    ctx.emit(resp, human)


# ---------------------------------------------------------------------------
# enable / disable (one or all), pin / unpin, session strategy
# ---------------------------------------------------------------------------


def _cmd_enable(args: argparse.Namespace, ctx: CLIContext) -> None:
    resp = expect_object(
        ctx.client.request(
            "POST",
            f"/admin/api/backend/{quote(args.name)}/enabled",
            payload={"value": True},
        ),
        "enable response",
    )
    ctx.emit(resp, f"enabled '{args.name}'")


def _cmd_disable(args: argparse.Namespace, ctx: CLIContext) -> None:
    resp = expect_object(
        ctx.client.request(
            "POST",
            f"/admin/api/backend/{quote(args.name)}/enabled",
            payload={"value": False},
        ),
        "disable response",
    )
    ctx.emit(resp, f"disabled '{args.name}'")


def _cmd_enable_all(args: argparse.Namespace, ctx: CLIContext) -> None:
    del args  # unused
    resp = expect_object(
        ctx.client.request("POST", "/admin/api/enabled", payload={"value": True}),
        "enable-all response",
    )
    ctx.emit(resp, "enabled all backends")


def _cmd_disable_all(args: argparse.Namespace, ctx: CLIContext) -> None:
    del args  # unused
    resp = expect_object(
        ctx.client.request("POST", "/admin/api/enabled", payload={"value": False}),
        "disable-all response",
    )
    ctx.emit(resp, "disabled all backends")


def _cmd_pin(args: argparse.Namespace, ctx: CLIContext) -> None:
    resp = expect_object(
        ctx.client.request(
            "POST",
            f"/admin/api/backend/{quote(args.name)}/pin",
            payload={"value": True},
        ),
        "pin response",
    )
    ctx.emit(resp, f"pinned '{args.name}' — all tools load upfront")


def _cmd_unpin(args: argparse.Namespace, ctx: CLIContext) -> None:
    resp = expect_object(
        ctx.client.request(
            "POST",
            f"/admin/api/backend/{quote(args.name)}/pin",
            payload={"value": False},
        ),
        "unpin response",
    )
    ctx.emit(resp, f"unpinned '{args.name}' — tools load on demand")


def _cmd_session(args: argparse.Namespace, ctx: CLIContext) -> None:
    if args.stateless is None:
        raise CLIError("specify --stateless or --warm")
    resp = expect_object(
        ctx.client.request(
            "POST",
            f"/admin/api/backend/{quote(args.name)}/stateless",
            payload={"value": args.stateless},
        ),
        "session response",
    )
    if args.stateless:
        human = f"'{args.name}' now uses stateless sessions (fresh session per call)"
    else:
        human = (
            f"'{args.name}' now uses warm sessions (reused, auto-recycled on failure)"
        )
    ctx.emit(resp, human)


# ---------------------------------------------------------------------------
# inspect / refresh (introspection)
# ---------------------------------------------------------------------------


def _refresh_human(entry: Mapping[str, Any]) -> str:
    status = entry.get("status")
    if status == "skipped":
        return "skipped (disabled or unmounted)"
    if status == "throttled":
        return "throttled (recently refreshed)"
    if status == "fresh":
        return f"fresh (captured {entry.get('age', '?')}s ago)"
    if status == "error":
        return f"error: {entry.get('error', 'unknown')}"
    if status == "refreshed":
        changed = "changed" if entry.get("changed") else "unchanged"
        return f"refreshed, {entry.get('tools', '?')} tools, {changed}"
    return str(status)


def _cmd_inspect(args: argparse.Namespace, ctx: CLIContext) -> None:
    resp = expect_object(
        ctx.client.request("POST", f"/admin/api/introspect/{quote(args.name)}"),
        "introspect response",
    )
    status = resp.get("status")
    if status == "refreshed":
        changed = "changed" if resp.get("changed") else "unchanged"
        human = (
            f"re-inspected '{args.name}': refreshed, "
            f"{resp.get('tools', '?')} tools, {changed}"
        )
    elif status == "throttled":
        human = f"re-inspected '{args.name}': throttled (recently refreshed)"
    elif status == "fresh":
        human = (
            f"re-inspected '{args.name}': fresh (captured {resp.get('age', '?')}s ago)"
        )
    else:
        human = f"re-inspected '{args.name}': {status or '?'}"
    ctx.emit(resp, human)


def _cmd_refresh(args: argparse.Namespace, ctx: CLIContext) -> None:
    del args  # unused
    resp = expect_object(
        ctx.client.request("POST", "/admin/api/refresh"), "refresh response"
    )
    raw_backends = resp.get("backends")
    backends = raw_backends if isinstance(raw_backends, dict) else {}
    lines = [f"{name}: {_refresh_human(entry)}" for name, entry in backends.items()]
    if not lines:
        lines = ["(no backends)"]
    ctx.emit(resp, lines)


# ---------------------------------------------------------------------------
# Registrar
# ---------------------------------------------------------------------------


def register_backend_commands(  # noqa: PLR0915 - one declarative parser block per command
    subparsers: argparse._SubParsersAction,
) -> None:
    """Register the ``backend`` command tree on *subparsers*."""

    parser = subparsers.add_parser(
        "backend", help="manage MCP backends (lifecycle, topology, live state)"
    )
    sub = parser.add_subparsers(
        dest="backend_command", required=True, metavar="COMMAND"
    )

    p = sub.add_parser("list", help="list configured backends with live state")
    p.set_defaults(handler=_cmd_list)

    p = sub.add_parser("show", help="show one backend's configuration and live state")
    p.add_argument("name", help="backend name")
    p.set_defaults(handler=_cmd_show)

    p = sub.add_parser(
        "add", help="add a backend (imports, introspects, then mounts it live)"
    )
    p.add_argument(
        "name",
        nargs="?",
        help="backend name (required unless --file supplies it)",
    )
    p.add_argument(
        "--transport",
        choices=_TRANSPORTS,
        help="transport (required unless --file supplies it)",
    )
    p.add_argument(
        "--backend-url",
        metavar="URL",
        help="remote URL for http/streamable-http/sse backends (never --url, "
        "which sets the Admin API base URL)",
    )
    p.add_argument("--command", metavar="CMD", help="stdio command")
    p.add_argument(
        "--arg",
        action="append",
        default=None,
        metavar="ARG",
        help="stdio argument (repeatable)",
    )
    p.add_argument(
        "--env",
        action=_EnvHeaderAction,
        dest="env_entries",
        literal=False,
        default=None,
        metavar="NAME='${VAR}'",
        help="child-process env for stdio backends (repeatable); each value "
        "must be exactly a ${VAR} or 'Bearer|Basic|Token ${VAR}' template "
        "(never mixed with raw text); later entries win",
    )
    p.add_argument(
        "--env-literal",
        action=_EnvHeaderAction,
        dest="env_entries",
        literal=True,
        default=None,
        metavar="NAME=VALUE",
        help="env entry with a clearly non-secret literal value (repeatable; "
        "credential-like names such as TOKEN or KEY are rejected)",
    )
    p.add_argument("--auth-header", metavar="NAME", help="authorization header name")
    p.add_argument(
        "--auth-value",
        metavar="VALUE",
        help="authorization header value — must be exactly a ${VAR} or "
        "'Bearer|Basic|Token ${VAR}' template (never mixed with raw text); "
        "empty clears it",
    )
    p.add_argument("--auth", choices=("oauth",), help="OAuth-protected remote")
    p.add_argument(
        "--header",
        action=_EnvHeaderAction,
        dest="header_entries",
        literal=False,
        default=None,
        metavar="NAME:${VALUE}",
        help="extra static header (repeatable); each value must be exactly a "
        "${VAR} or 'Bearer|Basic|Token ${VAR}' template (never mixed with "
        "raw text); later entries win",
    )
    p.add_argument(
        "--header-literal",
        action=_EnvHeaderAction,
        dest="header_entries",
        literal=True,
        default=None,
        metavar="NAME:VALUE",
        help="extra static header with a clearly non-secret literal value "
        "(repeatable; credential-like names are rejected)",
    )
    p.add_argument(
        "--headers-helper",
        metavar="CMD_OR_JSON",
        help="helper printing a JSON headers object to stdout: a JSON list of "
        "strings runs without a shell, a plain string runs via the shell",
    )
    p.add_argument("--init-timeout", type=float, metavar="SECONDS", help="init timeout")
    p.add_argument(
        "--request-timeout",
        type=float,
        metavar="SECONDS",
        help="request timeout",
    )
    session = p.add_mutually_exclusive_group()
    session.add_argument(
        "--stateless",
        dest="stateless",
        action="store_const",
        const=True,
        help="stateless sessions (a fresh session per call)",
    )
    session.add_argument(
        "--warm",
        dest="stateless",
        action="store_const",
        const=False,
        help="warm sessions (reused, auto-recycled on failure)",
    )
    p.add_argument(
        "--file",
        metavar="FILE",
        help="full backend JSON payload from FILE ('-' = stdin); explicit flags "
        "override the file's fields",
    )
    p.set_defaults(handler=_cmd_add)

    p = sub.add_parser(
        "display-name",
        help="set or clear a backend's display-only label (#42)",
    )
    p.add_argument("name", help="backend name")
    p.add_argument(
        "value",
        nargs="?",
        help="new display name (use --clear instead to remove it)",
    )
    p.add_argument("--clear", action="store_true", help="remove the display name")
    p.set_defaults(handler=_cmd_display_name)

    p = sub.add_parser(
        "remove", help="remove a backend (destructive; restarts the gateway)"
    )
    p.add_argument("name", help="backend name")
    p.add_argument("--yes", action="store_true", help="confirm removal")
    p.set_defaults(handler=_cmd_remove)

    p = sub.add_parser(
        "rename", help="hard-rename a backend (routing identity changes)"
    )
    p.add_argument("name", help="current backend name")
    p.add_argument("new_name", help="new backend name")
    p.set_defaults(handler=_cmd_rename)

    p = sub.add_parser("enable", help="enable one backend (mounts it live)")
    p.add_argument("name", help="backend name")
    p.set_defaults(handler=_cmd_enable)

    p = sub.add_parser("disable", help="disable one backend (unmounts it live)")
    p.add_argument("name", help="backend name")
    p.set_defaults(handler=_cmd_disable)

    p = sub.add_parser("enable-all", help="enable every backend")
    p.set_defaults(handler=_cmd_enable_all)

    p = sub.add_parser("disable-all", help="disable every backend")
    p.set_defaults(handler=_cmd_disable_all)

    p = sub.add_parser(
        "pin", help="pin all of a backend's tools to load upfront (eager)"
    )
    p.add_argument("name", help="backend name")
    p.set_defaults(handler=_cmd_pin)

    p = sub.add_parser("unpin", help="unpin a backend (tools load on demand instead)")
    p.add_argument("name", help="backend name")
    p.set_defaults(handler=_cmd_unpin)

    p = sub.add_parser(
        "session", help="set a backend's warm/stateless session strategy"
    )
    p.add_argument("name", help="backend name")
    strategy = p.add_mutually_exclusive_group(required=True)
    strategy.add_argument(
        "--stateless",
        dest="stateless",
        action="store_const",
        const=True,
        help="stateless sessions (a fresh session per call)",
    )
    strategy.add_argument(
        "--warm",
        dest="stateless",
        action="store_const",
        const=False,
        help="warm sessions (reused, auto-recycled on failure)",
    )
    p.set_defaults(handler=_cmd_session)

    p = sub.add_parser(
        "inspect",
        help="force a re-introspect of one backend (bypasses the refresh throttle)",
    )
    p.add_argument("name", help="backend name")
    p.set_defaults(handler=_cmd_inspect)

    p = sub.add_parser(
        "refresh",
        help="refresh every enabled+mounted backend's capture (throttled sweep)",
    )
    p.set_defaults(handler=_cmd_refresh)
