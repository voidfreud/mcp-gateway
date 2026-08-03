"""Installed CLI: foreground daemon plus macOS service lifecycle."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import TextIO

from mcp_gateway import service
from mcp_gateway.metadata import gateway_version

USAGE = """usage: mcp-gateway [--version | --foreground |
                   --install-service [--restart] |
                   --uninstall-service [--keep-data | --purge-data] |
                   --service-status]"""


def _answer(prompt: str, *, stdin: TextIO, stdout: TextIO) -> str:
    stdout.write(prompt)
    stdout.flush()
    return stdin.readline().strip().lower()


def _print_install(result, *, stdout: TextIO) -> None:
    action = "installed and started" if result.reloaded else "already installed"
    stdout.write(f"resident service {action}: com.void.mcp-gateway\n")
    if result.migrated_config:
        stdout.write("migrated checkout config to ~/.config/mcp-gateway/config.toml\n")
    if result.removed_legacy_link:
        stdout.write("removed legacy ~/.local/opt/mcp-gateway symlink\n")


def _print_status(status, *, stdout: TextIO) -> None:
    stdout.write(f"service: {'loaded' if status.loaded else 'not loaded'}\n")
    if status.pid is None:
        return
    stdout.write(f"pid: {status.pid}\n")
    if status.gateway_rss_bytes is not None:
        stdout.write(f"gateway RSS: {status.gateway_rss_bytes / 1048576:.1f} MiB\n")
    if status.children_rss_bytes is not None:
        stdout.write(
            f"backend child processes: {status.child_processes} "
            f"({status.children_rss_bytes / 1048576:.1f} MiB RSS)\n"
        )
    if status.total_rss_bytes is not None:
        stdout.write(
            f"resident process tree RSS: {status.total_rss_bytes / 1048576:.1f} MiB\n"
        )
    if status.cpu_percent is not None:
        stdout.write(f"gateway CPU snapshot: {status.cpu_percent:.1f}%\n")


def _run_foreground() -> None:
    from mcp_gateway.server import run_foreground  # noqa: PLC0415

    run_foreground()


def _uninstall(args: list[str], *, stdin: TextIO, stdout: TextIO) -> None:
    options = set(args[1:])
    if len(options) != len(args[1:]) or not options <= {
        "--keep-data",
        "--purge-data",
    }:
        raise SystemExit(2)
    if options == {"--keep-data", "--purge-data"}:
        raise SystemExit(2)
    if "--keep-data" in options:
        purge = False
    elif "--purge-data" in options:
        purge = True
    elif stdin.isatty() and stdout.isatty():
        stdout.write(
            "Service files will be removed. Config, logs, and backups are user data.\n"
        )
        purge = _answer(
            "Keep config and logs/state? [y/N] ",
            stdin=stdin,
            stdout=stdout,
        ) not in {"y", "yes"}
    else:
        raise service.ServiceError(
            "non-interactive uninstall requires --keep-data or --purge-data"
        )
    result = service.uninstall_service(purge_data=purge)
    stdout.write(
        f"resident service removed; "
        f"{'config and state deleted' if purge else 'config and state kept'}\n"
    )
    if not result.removed and not result.unloaded:
        stdout.write("service was already absent\n")


def _dispatch_explicit(args: list[str], *, stdin: TextIO, stdout: TextIO) -> bool:
    if args and args[0] == "--install-service":
        if args not in (["--install-service"], ["--install-service", "--restart"]):
            raise SystemExit(2)
        _print_install(
            service.install_service(force_restart="--restart" in args),
            stdout=stdout,
        )
        return True
    if args and args[0] == "--uninstall-service":
        _uninstall(args, stdin=stdin, stdout=stdout)
        return True
    if args == ["--service-status"]:
        if sys.platform != "darwin":
            raise service.ServiceError(
                "service status is available only for the macOS resident service"
            )
        _print_status(service.resource_status(), stdout=stdout)
        return True
    if args == ["--foreground"]:
        service.refresh_installed_service()
        _run_foreground()
        return True
    return False


def _default_start(*, stdin: TextIO, stdout: TextIO) -> None:
    paths = service.service_paths()
    if service.should_offer_service_install(paths=paths, stdin=stdin, stdout=stdout):
        choice = _answer(
            "Install mcp-gateway as a resident macOS login service? [y/N] ",
            stdin=stdin,
            stdout=stdout,
        )
        if choice in {"y", "yes"}:
            _print_install(service.install_service(paths=paths), stdout=stdout)
            return
        service.mark_prompt_declined(paths)
        stdout.write(
            "service install declined; starting in the foreground "
            "(use --install-service later)\n"
        )
    service.refresh_installed_service(paths=paths)
    _run_foreground()


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> None:
    """Dispatch foreground and application-owned service lifecycle modes."""

    args = list(sys.argv[1:] if argv is None else argv)
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    error = stderr or sys.stderr

    if args == ["--version"]:
        sink.write(f"mcp-gateway {gateway_version()}\n")
        return

    try:
        if _dispatch_explicit(args, stdin=source, stdout=sink):
            return
        if args:
            raise SystemExit(2)
        _default_start(stdin=source, stdout=sink)
    except service.ServiceError as exc:
        error.write(f"error: {exc}\n")
        raise SystemExit(1) from None
    except SystemExit as exc:
        if exc.code == 2:
            error.write(f"{USAGE}\n")
        raise


if __name__ == "__main__":
    main()
