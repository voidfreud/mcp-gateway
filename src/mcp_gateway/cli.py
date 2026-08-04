"""Scriptable control CLI for mcp-gateway.

Root command tree: ``run``, ``version``, ``update``, ``service``,
``status``, ``check``, ``restart`` and ``logs``, plus the four domain
command groups registered by the ``cli_*`` modules (backend, surface
[tool/resource/prompt/instructions], virtual, settings).

The pre-#284 daemon/service flags (``--foreground``, ``--install-service``,
``--uninstall-service``, ``--service-status``, ``--version``) survive as
hidden compatibility aliases translated onto the new tree, and a
no-argument invocation runs the gateway in the foreground directly — the
first-run service-install prompt is gone.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from mcp_gateway import (
    cli_backend,
    cli_settings,
    cli_surface,
    cli_virtual,
    config_loader,
    logging_setup,
    service,
)
from mcp_gateway.cli_common import (
    AdminClient,
    CLIContext,
    CLIError,
    expect_object,
    require_yes,
    safe_human_text,
)
from mcp_gateway.metadata import gateway_version

DEFAULT_TIMEOUT = 30.0
_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_LOG_ENTRY_KEYS = frozenset({"timestamp", "level", "logger", "event"})

# Local-only commands (run/version/update/service*) never touch the Admin API:
# they get this placeholder client so ``CLIContext.client`` stays non-optional
# for the API-backed domain modules, while NO config read or token resolution
# happens for them — a broken config or missing token must not block
# version/run/update/service recovery. Constructing the placeholder has no
# side effects (three attribute assignments).
_LOCAL_ONLY_CLIENT = AdminClient("http://127.0.0.1:9100", None, DEFAULT_TIMEOUT)


# ---------------------------------------------------------------------------
# Config / URL / token resolution — query commands never seed or create config
# ---------------------------------------------------------------------------


def _default_config_path() -> str:
    """Mirror ``server.default_config_path()`` without importing the server."""
    env = os.environ.get("MCP_GATEWAY_CONFIG")
    if env:
        return env
    if Path("config.toml").is_file():
        return "config.toml"
    return str(Path("~/.config/mcp-gateway/config.toml").expanduser())


def _read_config(path: str) -> dict | None:
    """Parse config.toml WITHOUT seeding: query commands must not create it."""
    p = Path(path).expanduser()
    if not p.is_file():
        return None
    try:
        with p.open("rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CLIError(f"could not read config {p}: {exc}") from None
    if not isinstance(raw, dict):
        raise CLIError(f"config {p} is not a TOML table")
    return raw


def _token_from_env(token_env: str | None) -> str | None:
    """The bearer token named by ``--token-env``, if given.

    A named variable that is unset or empty is an error. Returns ``None``
    when no ``--token-env`` was passed.
    """
    if token_env is None:
        return None
    token = os.environ.get(token_env)
    if not token:
        raise CLIError(f"environment variable {token_env!r} (--token-env) is not set")
    return token


def _resolve_token(raw: dict | None, token_env: str | None) -> str | None:
    """Resolve the Admin API bearer token for a CONFIG-DERIVED URL.

    Order: the ``--token-env`` named variable, then
    ``MCP_GATEWAY_ADMIN_TOKEN``, then the configured ``bearer_token`` or
    ``oauth.admin_bearer_token`` (each stored as a ``${ENV_VAR}`` reference,
    resolved here). A missing named env is an error. Returns ``None`` when
    the gateway runs unauthenticated (loopback, no token configured).
    Secrets never leave this process. An EXPLICIT ``--url`` must NOT use
    this ambient chain — only ``--token-env`` may authorize it
    (CLI-AUTH-ORIGIN-001).
    """
    if token_env is not None:
        return _token_from_env(token_env)
    env_token = os.environ.get("MCP_GATEWAY_ADMIN_TOKEN")
    if env_token:
        return env_token
    if raw is not None:
        ref = raw.get("bearer_token")
        if ref is None:
            oauth = raw.get("oauth")
            if isinstance(oauth, dict):
                ref = oauth.get("admin_bearer_token")
        if ref is not None:
            if not _ENV_REF.match(str(ref).strip()):
                raise CLIError("config bearer token must be one ${ENV_VAR} reference")
            try:
                return config_loader.expand_env_required(
                    str(ref), "config bearer token"
                )
            except config_loader.ConfigError as exc:
                raise CLIError(str(exc)) from None
    return None


_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "0:0:0:0:0:0:0:0"})


def _loopback_client_host(host: str) -> str:
    """Map a config bind host to a host a loopback client can reach.

    Wildcard binds (``0.0.0.0`` / ``::``) and bare IPv6 literals are not
    directly usable as a client URL: wildcards become loopback, and IPv6
    literals get URL brackets.
    """
    host = host.strip().strip("[]")
    if host in _WILDCARD_HOSTS:
        return "127.0.0.1"
    if ":" in host:
        return f"[{host}]"
    return host


def _default_url(raw: dict | None) -> str:
    """Derive the Admin API base URL from existing config, else loopback:9100."""
    if raw is not None:
        host = str(raw.get("host", "127.0.0.1"))
        port = raw.get("port", 9100)
        return f"http://{_loopback_client_host(host)}:{port}"
    return "http://127.0.0.1:9100"


# ---------------------------------------------------------------------------
# Global options: --url / --config / --token-env / --timeout / --json anywhere
# ---------------------------------------------------------------------------

_VALUE_GLOBALS = {
    "--url": "url",
    "--config": "config",
    "--token-env": "token_env",
    "--timeout": "timeout",
}
_FLAG_GLOBALS = {"--json": "json"}


def _extract_globals(argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Pull the global options out of argv wherever they appear."""
    opts: dict[str, Any] = {}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            rest.extend(argv[i:])
            break
        if tok in _FLAG_GLOBALS:
            opts[_FLAG_GLOBALS[tok]] = True
            i += 1
            continue
        if tok in _VALUE_GLOBALS:
            if i + 1 >= len(argv):
                raise CLIError(f"{tok} requires a value")
            opts[_VALUE_GLOBALS[tok]] = argv[i + 1]
            i += 2
            continue
        name, sep, value = tok.partition("=")
        if sep and name in _VALUE_GLOBALS:
            opts[_VALUE_GLOBALS[name]] = value
            i += 1
            continue
        rest.append(tok)
        i += 1
    if "timeout" in opts:
        try:
            timeout = float(opts["timeout"])
        except ValueError:
            raise CLIError(
                f"--timeout must be a number (got {opts['timeout']!r})"
            ) from None
        if timeout <= 0:
            raise CLIError("--timeout must be positive")
        opts["timeout"] = timeout
    return opts, rest


_LEGACY_PREFIXES = {
    "--version": ["version"],
    "--foreground": ["run"],
    "--service-status": ["service", "status"],
    "--install-service": ["service", "install"],
    "--uninstall-service": ["service", "uninstall", "--yes"],
}


def _translate_legacy(argv: list[str]) -> list[str]:
    """Map the pre-#284 daemon/service flags onto the new command tree.

    ``--uninstall-service`` carries ``--yes`` because the legacy flag was
    itself explicit, non-interactive consent (the new CLI never prompts).
    """
    if not argv:
        return []
    head, rest = argv[0], argv[1:]
    prefix = _LEGACY_PREFIXES.get(head)
    if prefix is None:
        return list(argv)
    if head == "--install-service" and rest not in ([], ["--restart"]):
        raise CLIError(f"unexpected arguments after {head}: {' '.join(rest)}")
    if head == "--uninstall-service" and rest not in (
        [],
        ["--keep-data"],
        ["--purge-data"],
    ):
        raise CLIError(f"unexpected arguments after {head}: {' '.join(rest)}")
    return prefix + list(rest)


# ---------------------------------------------------------------------------
# Parser assembly
# ---------------------------------------------------------------------------


def _add_global_option_group(parser: argparse.ArgumentParser) -> None:
    """Help-only group for the global options.

    The pre-extractor (:func:`_extract_globals`) consumes these from anywhere
    in argv before argparse parses, so declaring them here never double-parses
    — it only documents their syntax and defaults in ``--help``.
    """
    group = parser.add_argument_group(
        "global options",
        "accepted anywhere in argv, before or after the command",
    )
    group.add_argument(
        "--url",
        metavar="URL",
        help=(
            "Admin API base URL (default: http://<host>:<port> from an existing "
            "config.toml, else http://127.0.0.1:9100; an explicit URL only "
            "accepts a token via --token-env, and only over https or a "
            "verified loopback host)"
        ),
    )
    group.add_argument(
        "--config",
        metavar="PATH",
        help=(
            "config.toml path (default: $MCP_GATEWAY_CONFIG, then ./config.toml, "
            "then ~/.config/mcp-gateway/config.toml)"
        ),
    )
    group.add_argument(
        "--token-env",
        metavar="NAME",
        help=(
            "NAME of an environment variable holding the Admin bearer token; "
            "with an explicit --url this is the ONLY token source (default for "
            "config-derived URLs: $MCP_GATEWAY_ADMIN_TOKEN, then the configured "
            "bearer_token / oauth.admin_bearer_token)"
        ),
    )
    group.add_argument(
        "--timeout",
        metavar="SECONDS",
        help="per-request HTTP timeout in seconds (default: 30)",
    )
    group.add_argument(
        "--json",
        action="store_true",
        help=(
            "print exactly one JSON value instead of human text for finite "
            "control/query/mutation commands (not `run` or help output; "
            "`logs follow` streams one JSON value per line until interrupted)"
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-gateway",
        description=(
            "Control the mcp-gateway daemon: run it in the foreground, manage "
            "the resident macOS service, and drive the authenticated Admin API."
        ),
    )
    _add_global_option_group(parser)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=False)

    p_run = sub.add_parser("run", help="run the gateway in the foreground")
    p_run.set_defaults(handler=_cmd_run, local=True)

    p_version = sub.add_parser("version", help="print the installed gateway version")
    p_version.set_defaults(handler=_cmd_version, local=True)

    p_update = sub.add_parser(
        "update", help="update the installed gateway and resident service"
    )
    p_update.add_argument(
        "--version",
        metavar="VERSION",
        help="install a specific version (default: latest)",
    )
    p_update.set_defaults(handler=_cmd_update, local=True)

    _add_service_commands(sub)

    p_status = sub.add_parser("status", help="show per-backend daemon status")
    p_status.set_defaults(handler=_cmd_status)

    p_check = sub.add_parser(
        "check", help="check daemon readiness (/ready, no auth needed)"
    )
    p_check.set_defaults(handler=_cmd_check, no_token=True)

    p_restart = sub.add_parser("restart", help="restart the daemon (launchd-managed)")
    p_restart.set_defaults(handler=_cmd_restart)

    _register_domain_commands(sub)
    _add_logs_commands(sub)
    return parser


def _add_service_commands(sub: argparse._SubParsersAction) -> None:  # noqa: SLF001
    p = sub.add_parser("service", help="manage the resident macOS login service")
    ss = p.add_subparsers(dest="service_command", metavar="COMMAND", required=True)

    p_install = ss.add_parser("install", help="install or repair the resident service")
    p_install.add_argument(
        "--restart", action="store_true", help="force a restart after install"
    )
    p_install.set_defaults(handler=_cmd_service_install, local=True)

    p_uninstall = ss.add_parser("uninstall", help="remove the resident service")
    data = p_uninstall.add_mutually_exclusive_group()
    data.add_argument(
        "--keep-data", action="store_true", help="keep config/state (default)"
    )
    data.add_argument(
        "--purge-data", action="store_true", help="delete config/state too"
    )
    p_uninstall.add_argument(
        "--yes", action="store_true", help="confirm removal (required)"
    )
    p_uninstall.set_defaults(handler=_cmd_service_uninstall, local=True)

    p_status = ss.add_parser(
        "status", help="show the resident service status (macOS only)"
    )
    p_status.set_defaults(handler=_cmd_service_status, local=True)


def _register_domain_commands(sub: argparse._SubParsersAction) -> None:  # noqa: SLF001
    """The four domain command groups (#284), one ``register_*_commands`` each."""
    cli_backend.register_backend_commands(sub)
    cli_surface.register_surface_commands(sub)
    cli_virtual.register_virtual_commands(sub)
    cli_settings.register_settings_commands(sub)


def _log_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if not 1 <= limit <= 500:
        raise argparse.ArgumentTypeError("must be between 1 and 500")
    return limit


def _add_logs_commands(sub: argparse._SubParsersAction) -> None:  # noqa: SLF001
    p = sub.add_parser("logs", help="read the daemon's structured event log")
    ls = p.add_subparsers(dest="logs_command", metavar="COMMAND", required=True)

    p_show = ls.add_parser("show", help="print the tail of the event log")
    p_show.add_argument(
        "--limit",
        type=_log_limit,
        default=100,
        metavar="N",
        help="entries to show (1-500, default 100)",
    )
    p_show.add_argument(
        "--level",
        type=str.upper,
        choices=logging_setup.LOG_LEVELS,
        help="only this level",
    )
    p_show.add_argument("--event", help="only this event name")
    p_show.set_defaults(handler=_cmd_logs_show)

    p_follow = ls.add_parser(
        "follow",
        help="stream new log events until interrupted (--json: one per line)",
    )
    p_follow.add_argument(
        "--level",
        type=str.upper,
        choices=logging_setup.LOG_LEVELS,
        help="only this level",
    )
    # NOTE: no --event here — the /admin/api/logs/stream endpoint filters by
    # level only (event filtering comes with the SSE lifecycle PR); logs show
    # keeps --event because GET /admin/api/logs supports it.
    p_follow.set_defaults(handler=_cmd_logs_follow)


# ---------------------------------------------------------------------------
# Root command handlers
# ---------------------------------------------------------------------------


def _run_foreground() -> None:
    from mcp_gateway.server import run_foreground  # noqa: PLC0415

    run_foreground()


def _start_foreground() -> None:
    try:
        service.refresh_installed_service()
    except service.ServiceError as exc:
        raise CLIError(str(exc)) from None
    _run_foreground()


def _cmd_run(args: argparse.Namespace, ctx: CLIContext) -> None:
    _start_foreground()


def _cmd_version(args: argparse.Namespace, ctx: CLIContext) -> None:
    version = gateway_version()
    ctx.emit({"version": version}, f"mcp-gateway {version}")


def _cmd_update(args: argparse.Namespace, ctx: CLIContext) -> None:
    try:
        result = service.update_application(args.version)
    except service.ServiceError as exc:
        raise CLIError(str(exc)) from None
    payload = {
        "changed": result.changed,
        "previous_version": result.previous_version,
        "installed_version": result.installed_version,
        "service_restarted": result.service_restarted,
    }
    if not result.changed:
        ctx.emit(
            payload, f"mcp-gateway {result.installed_version} is already installed"
        )
        return
    lines = [
        f"updated mcp-gateway {result.previous_version} -> {result.installed_version}"
    ]
    if result.service_restarted:
        lines.append("resident service restarted and health/readiness verified")
    ctx.emit(payload, lines)


def _cmd_service_install(args: argparse.Namespace, ctx: CLIContext) -> None:
    try:
        result = service.install_service(force_restart=args.restart)
    except service.ServiceError as exc:
        raise CLIError(str(exc)) from None
    payload = {
        "changed": result.changed,
        "reloaded": result.reloaded,
        "migrated_config": result.migrated_config,
        "removed_legacy_link": result.removed_legacy_link,
    }
    action = "installed and started" if result.reloaded else "already installed"
    msg = f"resident service {action}: {service.LABEL}"
    notes = []
    if result.changed and result.reloaded:
        notes.append("future updates: run `mcp-gateway update`")
    if result.migrated_config:
        notes.append("migrated checkout config to ~/.config/mcp-gateway/config.toml")
    if result.removed_legacy_link:
        notes.append("removed legacy ~/.local/opt/mcp-gateway symlink")
    ctx.emit(payload, [msg, *notes])


def _cmd_service_uninstall(args: argparse.Namespace, ctx: CLIContext) -> None:
    require_yes(args, "service uninstall")
    purge = bool(args.purge_data)
    try:
        result = service.uninstall_service(purge_data=purge)
    except service.ServiceError as exc:
        raise CLIError(str(exc)) from None
    was_absent = not result.removed and not result.unloaded
    payload = {
        "unloaded": result.unloaded,
        "removed": [str(p) for p in result.removed],
        "purged_data": result.purged_data,
        "was_absent": was_absent,
    }
    lines = [
        "resident service removed; "
        f"{'config and state deleted' if purge else 'config and state kept'}"
    ]
    if was_absent:
        lines.append("service was already absent")
    ctx.emit(payload, lines)


def _format_service_status(status) -> list[str]:
    lines = [f"service: {'loaded' if status.loaded else 'not loaded'}"]
    if status.pid is None:
        return lines
    lines.append(f"pid: {status.pid}")
    if status.gateway_rss_bytes is not None:
        lines.append(f"gateway RSS: {status.gateway_rss_bytes / 1048576:.1f} MiB")
    if status.children_rss_bytes is not None:
        lines.append(
            f"backend child processes: {status.child_processes} "
            f"({status.children_rss_bytes / 1048576:.1f} MiB RSS)"
        )
    if status.total_rss_bytes is not None:
        lines.append(
            f"resident process tree RSS: {status.total_rss_bytes / 1048576:.1f} MiB"
        )
    if status.cpu_percent is not None:
        lines.append(f"gateway CPU snapshot: {status.cpu_percent:.1f}%")
    return lines


def _cmd_service_status(args: argparse.Namespace, ctx: CLIContext) -> None:
    if sys.platform != "darwin":
        raise CLIError(
            "service status is available only for the macOS resident service"
        )
    try:
        status = service.resource_status()
    except service.ServiceError as exc:
        raise CLIError(str(exc)) from None
    payload = {
        "loaded": status.loaded,
        "pid": status.pid,
        "gateway_rss_bytes": status.gateway_rss_bytes,
        "child_processes": status.child_processes,
        "children_rss_bytes": status.children_rss_bytes,
        "total_rss_bytes": status.total_rss_bytes,
        "cpu_percent": status.cpu_percent,
    }
    ctx.emit(payload, _format_service_status(status))


def _cmd_status(args: argparse.Namespace, ctx: CLIContext) -> None:
    data = expect_object(
        ctx.client.request("GET", "/admin/api/status"), "status response"
    )
    raw_backends = data.get("backends")
    backends: dict[str, Any] = (
        {str(k): v for k, v in raw_backends.items()}
        if isinstance(raw_backends, dict)
        else {}
    )
    lines: list[str] = []
    for name in sorted(backends):
        entry = backends[name]
        if not isinstance(entry, dict):
            lines.append(f"{name}: unknown")
            continue
        state = entry.get("state", "unknown")
        if state == "ok":
            lines.append(
                f"{name}: ok ({entry.get('tools', 0)} tools, {entry.get('ms', 0)} ms)"
            )
        elif state == "error":
            lines.append(f"{name}: error ({entry.get('error', '')})")
        else:
            lines.append(f"{name}: {state}")
    if not lines:
        lines.append("no backends configured")
    ctx.emit(data, lines)


def _cmd_check(args: argparse.Namespace, ctx: CLIContext) -> None:
    try:
        raw = ctx.client.request("GET", "/ready")
    except CLIError as exc:
        if isinstance(exc.response, dict):
            raw = exc.response  # 503 readiness payload carries the details
        else:
            raise
    data = expect_object(raw, "readiness response")
    ready = bool(data.get("ready"))
    lines = ["ready" if ready else "not ready"]
    missing = data.get("missing")
    mounted = data.get("mounted")
    if isinstance(missing, list) and missing:
        lines.append(f"missing: {', '.join(str(m) for m in missing)}")
    if isinstance(mounted, list) and mounted:
        lines.append(f"mounted: {', '.join(str(m) for m in mounted)}")
    ctx.emit(data, lines)
    if not ready:
        raise CLIError("gateway is not ready (see output above)")


def _cmd_restart(args: argparse.Namespace, ctx: CLIContext) -> None:
    data = expect_object(
        ctx.client.request("POST", "/admin/api/restart"), "restart response"
    )
    if data.get("reloaded") == "restarting":
        msg = "restart scheduled (launchd-managed daemon)"
    else:
        msg = "daemon is dev/foreground-managed; restart not needed"
    ctx.emit(data, msg)


# ---------------------------------------------------------------------------
# Logs: show / follow — centralized here so streaming/error semantics stay
# with the client
# ---------------------------------------------------------------------------


def _format_log_entry(entry: object) -> str:
    if not isinstance(entry, dict):
        return str(entry)
    fields: dict[str, Any] = {str(k): v for k, v in entry.items()}
    raw = fields.get("raw")
    if raw is not None:
        return str(raw)
    head = " ".join(
        part
        for part in (
            str(fields.get("timestamp") or ""),
            str(fields.get("level") or "").upper(),
            str(fields.get("event") or ""),
        )
        if part
    )
    extra = {
        key: value
        for key, value in fields.items()
        if key not in _LOG_ENTRY_KEYS and value is not None
    }
    if extra:
        rendered = " ".join(f"{k}={v}" for k, v in sorted(extra.items()))
        return f"{head} {rendered}" if head else rendered
    return head


def _cmd_logs_show(args: argparse.Namespace, ctx: CLIContext) -> None:
    params: dict[str, object] = {"limit": args.limit}
    if args.level:
        params["level"] = args.level
    if args.event:
        params["event"] = args.event
    data = ctx.client.request("GET", "/admin/api/logs", params=params)
    entries = data.get("entries") if isinstance(data, dict) else None
    if isinstance(entries, list):
        lines = [_format_log_entry(entry) for entry in entries]
    else:
        lines = []
    ctx.emit(data, lines if lines else ["(no log entries)"])


def _cmd_logs_follow(args: argparse.Namespace, ctx: CLIContext) -> None:
    """Stream log events until interrupted.

    With ``--json`` each event is printed as exactly one JSON value per line
    (NDJSON) directly to stdout; errors at stream start go to stderr with a
    nonzero exit. Human mode prints one formatted line per event.
    """
    params: dict[str, object] = {}
    if args.level:
        params["level"] = args.level
    for line in ctx.client.stream("/admin/api/logs/stream", params=params):
        if not line.startswith("data: "):
            continue  # keepalive / comment frames
        event_text = line[len("data: ") :]
        if ctx.json_output:
            ctx.stdout.write(event_text + "\n")
            continue
        try:
            entry = json.loads(event_text)
        except ValueError:
            continue
        ctx.stdout.write(safe_human_text(_format_log_entry(entry)) + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> None:
    """Run the control CLI. Raises SystemExit: 0 on success/help, 1 on a
    user-facing error, 2 on a usage error."""
    args = list(sys.argv[1:] if argv is None else argv)
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    error = stderr or sys.stderr

    try:
        globals_, rest = _extract_globals(args)
        rest = _translate_legacy(rest)
        if not rest:
            # No-argument invocation: run the gateway in the foreground
            # directly — the first-run service-install prompt is gone.
            _start_foreground()
            return

        parser = _build_parser()
        redirected = (sys.stdout, sys.stderr) != (sink, error)
        if redirected:
            saved = (sys.stdout, sys.stderr)
            sys.stdout, sys.stderr = sink, error
        try:
            namespace = parser.parse_args(rest)
        finally:
            if redirected:
                sys.stdout, sys.stderr = saved

        handler = getattr(namespace, "handler", None)
        if handler is None:
            parser.print_help(file=error)
            raise SystemExit(2)

        if getattr(namespace, "local", False):
            # Local commands (run/version/update/service*) must work with a
            # broken config or missing token: no config read, no token
            # resolution, no API client construction beyond the placeholder.
            client = _LOCAL_ONLY_CLIENT
        else:
            # Config is parsed only when the URL is config-derived (no
            # explicit --url). An explicit --url must never receive the
            # ambient MCP_GATEWAY_ADMIN_TOKEN or a configured token
            # (CLI-AUTH-ORIGIN-001): only an explicit --token-env may
            # authorize it, so config is never needed in that case.
            no_token = getattr(namespace, "no_token", False)
            url_explicit = globals_.get("url") is not None
            token_env = globals_.get("token_env")
            raw = (
                _read_config(globals_.get("config") or _default_config_path())
                if not url_explicit
                else None
            )
            url = globals_.get("url") or _default_url(raw)
            if no_token:
                # `check` probes the auth-exempt /ready endpoint: derive the
                # URL from config but skip protected token resolution — a
                # secrets-only deployment or an unset --token-env must not
                # block a liveness probe.
                client = AdminClient(
                    url, None, globals_.get("timeout", DEFAULT_TIMEOUT)
                )
            elif url_explicit:
                # CLI-AUTH-ORIGIN-001: only --token-env may authorize an
                # explicit URL; the ambient token chain never applies.
                client = AdminClient(
                    url,
                    _token_from_env(token_env),
                    globals_.get("timeout", DEFAULT_TIMEOUT),
                )
            else:
                token = _resolve_token(raw, token_env)
                client = AdminClient(
                    url, token, globals_.get("timeout", DEFAULT_TIMEOUT)
                )
        ctx = CLIContext(
            client=client,
            stdin=source,
            stdout=sink,
            stderr=error,
            json_output=bool(globals_.get("json")),
        )
        handler(namespace, ctx)
    except CLIError as exc:
        error.write(f"error: {safe_human_text(exc)}\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
