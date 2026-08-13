"""``settings`` control CLI: show/set/export/import (issue #284).

All commands talk to the authenticated Admin API:
  GET  /admin/api/settings   — gateway-wide boot-time settings
  PUT  /admin/api/settings   — update settings (validated, atomic)
  GET  /admin/api/export     — one-call settings bundle (export_settings)
  POST /admin/api/import     — atomic bundle import (import_settings)

The API never resolves secrets: ``bearer_token`` is stored and returned as a
``${ENV}`` reference, and the export bundle excludes backend topology/auth, so
nothing here expands or reveals secret values.  Unknown/invalid values are
rejected locally before any request is sent where the server would reject them
anyway; everything else is validated server-side before commit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp_gateway.cli_common import (
    LIMIT_MAX_BYTES,
    UNSET,
    CLIContext,
    CLIError,
    expect_object,
    limit_flag_type,
    read_json_source,
    require_yes,
)

# Keys writable through PUT /admin/api/settings (mirrors put_settings exactly).
SETTINGS_KEYS = (
    "bearer_token",
    "introspect_interval",
    "log_level",
    "log_max_bytes",
    "log_backup_count",
    "update_check",
    # #286: configurable UTF-8 metadata limits.
    "server_instructions_max_bytes",
    "tool_description_max_bytes",
)

# Matches logging_setup.LOG_LEVELS / the GatewayConfig Literal.  Kept local so
# the CLI stays a thin HTTP client with no config-loader dependency.
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

LOG_MAX_BYTES_MIN = 64 * 1024
LOG_MAX_BYTES_MAX = 1024 * 1024 * 1024
LOG_BACKUP_MIN = 1
LOG_BACKUP_MAX = 100

# Same shape as the server's guard (#155): a stored token is a single
# ${ENV_VAR} reference, never a pasted secret.
_ENV_REF_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")

# Config keys that exist but are boot-time-only: the Admin API cannot mutate
# them, so they get a targeted hint instead of a generic "unknown key" error.
_READONLY_HINTS = {
    "host": "bind address is fixed at boot; edit config.toml",
    "port": "bind port is fixed at boot; edit config.toml",
    "log_file": "log file path is fixed at boot; edit config.toml",
    "baseline_max_age": "read at boot; edit config.toml",
    "auth_mode": "derived from the configured auth profile",
    "oauth": "OAuth profile is read-only via the Admin API; edit config.toml",
}


def register_settings_commands(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``settings`` command group on the root subparsers."""

    parser = subparsers.add_parser(
        "settings",
        help="show/update gateway settings, export/import the settings bundle",
        description=(
            "Gateway-wide boot-time settings and the export/import bundle. "
            "Settings changes persist and restart the daemon when launchd-managed."
        ),
    )
    commands = parser.add_subparsers(
        dest="settings_command", required=True, metavar="COMMAND"
    )

    show = commands.add_parser("show", help="show gateway-wide settings")
    show.set_defaults(handler=_settings_show)

    set_parser = commands.add_parser(
        "set",
        help="update gateway-wide settings",
        description=(
            "Updates only the keys given (absent keys keep stored values). "
            "Unknown or invalid values fail before anything is sent."
        ),
    )
    set_parser.add_argument(
        "--set",
        dest="sets",
        action="append",
        default=[],
        metavar="KEY=JSON",
        help=(
            "set one key to a JSON value; repeatable. Keys: "
            + ", ".join(SETTINGS_KEYS)
            + '. Quote strings, e.g. --set log_level="INFO"'
        ),
    )
    set_parser.add_argument(
        "--bearer-token",
        metavar="REF",
        help=(
            "set bearer_token to a ${ENV_VAR} reference (never the secret "
            "itself); empty string clears it"
        ),
    )
    set_parser.add_argument(
        "--introspect-interval",
        type=int,
        metavar="SECONDS",
        help="scheduled re-introspection sweep interval; 0 disables it",
    )
    set_parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        help="log verbosity: " + ", ".join(LOG_LEVELS),
    )
    set_parser.add_argument(
        "--log-max-bytes",
        type=int,
        metavar="BYTES",
        help=f"active log file cap ({LOG_MAX_BYTES_MIN}..{LOG_MAX_BYTES_MAX})",
    )
    set_parser.add_argument(
        "--log-backup-count",
        type=int,
        metavar="COUNT",
        help=f"rotated log files kept ({LOG_BACKUP_MIN}..{LOG_BACKUP_MAX})",
    )
    set_parser.add_argument(
        "--server-instructions-max-bytes",
        type=limit_flag_type(None, "server_instructions_max_bytes"),
        metavar="N",
        help=f"server instructions cap in UTF-8 bytes (1..{LIMIT_MAX_BYTES})",
    )
    set_parser.add_argument(
        "--tool-description-max-bytes",
        type=limit_flag_type("unlimited", "tool_description_max_bytes"),
        default=UNSET,
        metavar="N|unlimited",
        help=(
            f"tool description cap in UTF-8 bytes (1..{LIMIT_MAX_BYTES}); "
            "'unlimited' removes the cap"
        ),
    )
    toggle = set_parser.add_mutually_exclusive_group()
    toggle.add_argument(
        "--update-check",
        dest="update_check",
        action="store_true",
        default=None,
        help="enable the daily PyPI update check",
    )
    toggle.add_argument(
        "--no-update-check",
        dest="update_check",
        action="store_false",
        default=None,
        help="disable the daily PyPI update check",
    )
    set_parser.set_defaults(handler=_settings_set)

    export = commands.add_parser(
        "export",
        help="export the settings bundle as JSON",
        description=(
            "Emits the exact /admin/api/export bundle (stored overrides and "
            "instructions; no backend topology or secrets)."
        ),
    )
    export.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help="write the bundle to FILE instead of stdout",
    )
    export.add_argument(
        "--force",
        action="store_true",
        help="replace an existing regular file at the destination",
    )
    export.add_argument(
        "--full",
        action="store_true",
        help="include captured defaults for context (import ignores them)",
    )
    export.set_defaults(handler=_settings_export)

    import_parser = commands.add_parser(
        "import",
        help="import a settings bundle (atomic; validated all-or-nothing)",
        description=(
            "Imports a bundle from a JSON file or '-' (stdin) exactly like the "
            "dashboard: stored overrides + instructions are overwritten per "
            "backend, so this requires --yes. Backend topology is never imported."
        ),
    )
    import_parser.add_argument(
        "source",
        metavar="SOURCE",
        help="JSON bundle file, or '-' to read from stdin",
    )
    import_parser.add_argument(
        "--mode",
        choices=("merge", "replace"),
        default="replace",
        help=(
            "replace resets each named backend to the bundle exactly (dashboard "
            "behavior); merge applies on top, preserving values for keys absent "
            "from each entry (default: replace)"
        ),
    )
    import_parser.add_argument(
        "--yes",
        action="store_true",
        help="acknowledge that stored overrides + instructions are overwritten",
    )
    import_parser.set_defaults(handler=_settings_import)


def _settings_show(args: argparse.Namespace, ctx: CLIContext) -> None:
    resp = expect_object(
        ctx.client.request("GET", "/admin/api/settings"), "settings response"
    )
    ctx.emit(payload=resp, human=_settings_human(resp))


def _settings_human(settings: dict) -> list[str]:
    """Concise human rendering of the GET settings payload."""

    def shown(value: Any, fallback: str = "(none)") -> str:
        return fallback if value in (None, "") else str(value)

    lines = [
        f"bearer_token: {shown(settings.get('bearer_token'))}",
        f"introspect_interval: {shown(settings.get('introspect_interval'), '0')}",
        f"log_level: {shown(settings.get('log_level'))}",
        f"log_max_bytes: {shown(settings.get('log_max_bytes'))}",
        f"log_backup_count: {shown(settings.get('log_backup_count'))}",
        f"update_check: {shown(settings.get('update_check'), 'false')}",
        # #286: gateway-wide UTF-8 metadata limits (None tool cap = unbounded).
        f"server_instructions_max_bytes: "
        f"{shown(settings.get('server_instructions_max_bytes'))}",
        f"tool_description_max_bytes: "
        f"{shown(settings.get('tool_description_max_bytes'), 'unlimited')}",
    ]
    if settings.get("auth_mode") == "oauth_jwt":
        oauth = settings.get("oauth") or {}
        lines.append("auth_mode: oauth_jwt")
        lines.append(f"oauth.public_base_url: {shown(oauth.get('public_base_url'))}")
        servers = oauth.get("authorization_servers") or []
        lines.append(f"oauth.authorization_servers: {', '.join(map(str, servers))}")
        scopes = oauth.get("required_scopes") or []
        lines.append(f"oauth.required_scopes: {', '.join(map(str, scopes))}")
    else:
        lines.append("auth_mode: static bearer")
    return lines


def _settings_set(args: argparse.Namespace, ctx: CLIContext) -> None:
    payload: dict[str, Any] = {}
    for pair in args.sets:
        key, value = _parse_set_pair(pair)
        payload[key] = value
    # Explicit flags win over --set pairs, matching a last-write-wins CLI.
    if args.bearer_token is not None:
        payload["bearer_token"] = _normalize_bearer_token(args.bearer_token)
    if args.introspect_interval is not None:
        _require_range("introspect_interval", args.introspect_interval, 0, None)
        payload["introspect_interval"] = args.introspect_interval
    if args.log_level is not None:
        payload["log_level"] = _normalize_log_level(args.log_level)
    if args.log_max_bytes is not None:
        _require_range(
            "log_max_bytes", args.log_max_bytes, LOG_MAX_BYTES_MIN, LOG_MAX_BYTES_MAX
        )
        payload["log_max_bytes"] = args.log_max_bytes
    if args.log_backup_count is not None:
        _require_range(
            "log_backup_count", args.log_backup_count, LOG_BACKUP_MIN, LOG_BACKUP_MAX
        )
        payload["log_backup_count"] = args.log_backup_count
    if args.update_check is not None:
        payload["update_check"] = args.update_check
    if args.server_instructions_max_bytes is not None:
        payload["server_instructions_max_bytes"] = args.server_instructions_max_bytes
    if args.tool_description_max_bytes is not UNSET:
        payload["tool_description_max_bytes"] = args.tool_description_max_bytes

    if not payload:
        raise CLIError(
            "nothing to change: pass --set KEY=JSON or an explicit setting flag"
        )

    resp = expect_object(
        ctx.client.request("PUT", "/admin/api/settings", payload=payload),
        "settings update response",
    )
    ctx.emit(payload=resp, human=_settings_saved_human(resp))


def _parse_set_pair(pair: str) -> tuple[str, Any]:
    key, sep, raw = pair.partition("=")
    if not sep or not key.strip():
        raise CLIError(f"invalid --set value {pair!r}: expected KEY=JSON")
    key = key.strip()
    if key not in SETTINGS_KEYS:
        hint = _READONLY_HINTS.get(key)
        if hint:
            raise CLIError(
                f"settings key {key!r} is not writable via the Admin API ({hint})"
            )
        raise CLIError(
            f"unknown settings key {key!r} (writable keys: {', '.join(SETTINGS_KEYS)})"
        )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CLIError(
            f"invalid JSON for {key!r}: {exc} "
            f'(quote string values, e.g. --set {key}="value")'
        ) from None
    _validate_key_value(key, value)
    return key, value


def _normalize_bearer_token(value: str) -> str | None:
    """Validate an auth flag value: empty clears, otherwise a ${ENV} reference."""
    tok = value.strip()
    if tok and not _ENV_REF_RE.fullmatch(tok):
        raise CLIError(
            "bearer_token must reference an environment variable like "
            "${MCP_GATEWAY_TOKEN} — never paste the secret itself"
        )
    return tok or None


def _normalize_log_level(value: str) -> str:
    level = value.upper()
    if level not in LOG_LEVELS:
        raise CLIError(f"log_level must be one of {', '.join(LOG_LEVELS)}")
    return level


def _require_range(key: str, value: int, lo: int | None, hi: int | None) -> None:
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        if lo is not None and hi is not None:
            raise CLIError(f"{key} must be an integer between {lo} and {hi}")
        if lo is not None:
            raise CLIError(f"{key} must be an integer >= {lo}")
        raise CLIError(f"{key} must be an integer <= {hi}")


def _validate_bearer_token(value: Any) -> None:
    """bearer_token: null clears; otherwise a string that is a ${ENV} ref."""
    if value is None:
        return
    if not isinstance(value, str):
        raise CLIError("bearer_token must be a string or null")
    _normalize_bearer_token(value)


def _validate_log_level(value: Any) -> None:
    """log_level: one of the known levels (case-insensitive)."""
    if not isinstance(value, str):
        raise CLIError(f"log_level must be one of {', '.join(LOG_LEVELS)}")
    _normalize_log_level(value)


def _validate_tool_description_max_bytes(value: Any) -> None:
    """tool_description_max_bytes: an integer in 1..LIMIT_MAX_BYTES, or null
    (no cap). JSON strings/bools/0 must never coerce into an int (#286)."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise CLIError(
            "tool_description_max_bytes must be an integer between 1 and "
            f"{LIMIT_MAX_BYTES}, or null"
        )
    _require_range("tool_description_max_bytes", value, 1, LIMIT_MAX_BYTES)


def _bool_validator(key: str) -> Callable[[Any], None]:
    """Build a strict-boolean validator: JSON strings/integers/null must
    never coerce into a bool (#A8)."""

    def check(value: Any) -> None:
        if not isinstance(value, bool):
            raise CLIError(f"{key} must be a boolean")

    return check


def _int_validator(
    key: str, lo: int | None, hi: int | None, bounds: str
) -> Callable[[Any], None]:
    """Build an integer validator (never a bool, an int subclass) within the
    [lo, hi] bounds, mirroring PUT /admin/api/settings validation."""

    def check(value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CLIError(f"{key} must be an integer{bounds}")
        _require_range(key, value, lo, hi)

    return check


# Local fast-fail mirror of PUT /admin/api/settings validation, keyed by the
# exact key the server accepts. Anything not in this table is rejected earlier
# by _parse_set_pair, so a lookup here can never KeyError.
_KEY_VALIDATORS: dict[str, Callable[[Any], None]] = {
    "bearer_token": _validate_bearer_token,
    "introspect_interval": _int_validator("introspect_interval", 0, None, " >= 0"),
    "log_level": _validate_log_level,
    "log_max_bytes": _int_validator(
        "log_max_bytes",
        LOG_MAX_BYTES_MIN,
        LOG_MAX_BYTES_MAX,
        f" between {LOG_MAX_BYTES_MIN} and {LOG_MAX_BYTES_MAX}",
    ),
    "log_backup_count": _int_validator(
        "log_backup_count",
        LOG_BACKUP_MIN,
        LOG_BACKUP_MAX,
        f" between {LOG_BACKUP_MIN} and {LOG_BACKUP_MAX}",
    ),
    "update_check": _bool_validator("update_check"),
    "server_instructions_max_bytes": _int_validator(
        "server_instructions_max_bytes",
        1,
        LIMIT_MAX_BYTES,
        f" between 1 and {LIMIT_MAX_BYTES}",
    ),
    "tool_description_max_bytes": _validate_tool_description_max_bytes,
}


def _validate_key_value(key: str, value: Any) -> None:
    """Validate one key/value pair exactly as the server would."""
    _KEY_VALIDATORS[key](value)


def _settings_saved_human(resp: dict[str, Any]) -> str:
    if resp.get("reloaded") == "restarting":
        return "settings saved — restarting gateway"
    return "settings saved (dev: no launchd restart — applies on next manual restart)"


def _settings_export(args: argparse.Namespace, ctx: CLIContext) -> None:
    params = {"full": "true"} if args.full else None
    resp = expect_object(
        ctx.client.request("GET", "/admin/api/export", params=params),
        "settings export response",
    )
    text = json.dumps(resp, indent=2) + "\n"
    if args.output:
        _write_export_bundle(Path(args.output).expanduser(), text, force=args.force)
        ctx.emit(payload=resp, human=f"settings bundle written to {args.output}")
    else:
        ctx.stdout.write(text)


def _write_export_bundle(dest: Path, text: str, *, force: bool) -> None:
    """Write *text* to *dest* safely: same-directory temp file (mode 0600),
    flush + fsync, then an atomic install. Without --force any existing path
    (file, symlink, anything) is refused; with --force only a regular file is
    replaced (symlinks and special files are rejected, and the symlink itself
    is never followed). Parent directories are never created. Every failure
    becomes a concise CLIError naming the destination.
    """
    if not dest.parent.is_dir():
        raise CLIError(
            f"could not write {dest}: directory {dest.parent} does not exist"
        )
    if os.path.lexists(dest):
        if not force:
            raise CLIError(f"refusing to overwrite existing {dest} (pass --force)")
        st = os.lstat(dest)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise CLIError(
                f"refusing to replace non-regular path {dest} (symlink or special file)"
            )
    tmp: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if force:
            os.replace(tmp, dest)  # atomic; consumes tmp
        else:
            try:
                os.link(tmp, dest)  # atomic no-overwrite install
            except FileExistsError:
                raise CLIError(
                    f"refusing to overwrite existing {dest} (pass --force)"
                ) from None
            os.unlink(tmp)
        _fsync_dir(dest.parent)
    except OSError as exc:
        raise CLIError(f"could not write {dest}: {exc}") from None
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync of a directory after an install. Not supported on
    every platform, so a failure here is ignored rather than reported."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _settings_import(args: argparse.Namespace, ctx: CLIContext) -> None:
    # The dashboard warns before any import (it always uses replace); both
    # modes overwrite stored values for present keys, so --yes is required.
    require_yes(args, "Import settings overwrites stored overrides + instructions")
    bundle = expect_object(
        read_json_source(args.source, stdin=ctx.stdin), "settings bundle"
    )
    try:
        resp = ctx.client.request(
            "POST",
            "/admin/api/import",
            payload={"settings": bundle, "mode": args.mode},
        )
    except CLIError as exc:
        # AdminClient already raised for the 400; the import route reports
        # per-item failures under ``errors``, which its generic message omits.
        body = exc.response
        if isinstance(body, dict):
            errors = body.get("errors")
            if errors and isinstance(errors, list):
                raise CLIError(
                    "; ".join(str(e) for e in errors), response=body
                ) from None
        raise
    resp = expect_object(resp, "settings import response")
    backends = resp.get("backends")
    if not isinstance(backends, list):
        backends = []
    names = ", ".join(str(b) for b in backends)
    ctx.emit(
        payload=resp,
        human=(
            f"imported settings for {len(backends)} backend(s)"
            f"{': ' + names if names else ''} (mode: {args.mode})"
        ),
    )
