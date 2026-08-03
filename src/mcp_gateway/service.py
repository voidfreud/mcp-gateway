"""macOS LaunchAgent lifecycle for the installed mcp-gateway tool.

The service is deliberately managed by the application rather than a package
installer hook: uv does not run post-install hooks, and the application can
keep install, upgrade, and removal symmetric.  This module uses only the
standard library so lifecycle commands remain available even when daemon
imports fail.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

LABEL = "com.void.mcp-gateway"
TEMPLATE_VERSION = "1"
DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
LAUNCHCTL = "/bin/launchctl"
PS = "/bin/ps"

Runner = Callable[..., subprocess.CompletedProcess[str]]
Sleeper = Callable[[float], None]


@dataclass(frozen=True)
class ServiceRuntime:
    """Injectable operating-system collaborators for deterministic tests."""

    runner: Runner = subprocess.run
    sleep: Sleeper = time.sleep
    platform: str = sys.platform
    uid: int = os.getuid()
    probe: Callable[[ServicePaths], None] | None = None


class ServiceError(RuntimeError):
    """A service lifecycle operation could not complete safely."""


@dataclass(frozen=True)
class ServicePaths:
    """Every user-owned artifact created by service installation."""

    home: Path

    @property
    def agents_dir(self) -> Path:
        return self.home / "Library" / "LaunchAgents"

    @property
    def plist(self) -> Path:
        return self.agents_dir / f"{LABEL}.plist"

    @property
    def state_dir(self) -> Path:
        return self.home / ".local" / "state" / "mcp-gateway"

    @property
    def config_dir(self) -> Path:
        return self.home / ".config" / "mcp-gateway"

    @property
    def config(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def wrapper_dir(self) -> Path:
        return self.home / ".local" / "libexec" / "mcp-gateway"

    @property
    def wrapper(self) -> Path:
        return self.wrapper_dir / "run"

    @property
    def binary(self) -> Path:
        return self.home / ".local" / "bin" / "mcp-gateway"

    @property
    def prompted_marker(self) -> Path:
        return self.state_dir / "service-prompted"

    @property
    def legacy_link(self) -> Path:
        return self.home / ".local" / "opt" / "mcp-gateway"


@dataclass(frozen=True)
class InstallResult:
    """Observable result of one idempotent install operation."""

    changed: bool
    reloaded: bool
    migrated_config: bool
    removed_legacy_link: bool


@dataclass(frozen=True)
class UninstallResult:
    """Observable result of one idempotent uninstall operation."""

    unloaded: bool
    removed: tuple[Path, ...]
    purged_data: bool


@dataclass(frozen=True)
class ResourceStatus:
    """On-demand process footprint; collecting it creates no daemon worker."""

    loaded: bool
    pid: int | None
    gateway_rss_bytes: int | None
    child_processes: int
    children_rss_bytes: int | None
    total_rss_bytes: int | None
    cpu_percent: float | None


def service_paths(home: Path | None = None) -> ServicePaths:
    """Return service paths for *home* (or the current user's real home)."""

    return ServicePaths((home or Path.home()).expanduser().resolve())


def _require_macos(platform: str | None = None) -> None:
    if (platform or sys.platform) != "darwin":
        raise ServiceError(
            "mcp-gateway service management is available only on macOS; "
            "run `mcp-gateway --foreground` on this platform"
        )


def _atomic_write(path: Path, data: bytes, *, mode: int) -> None:
    """Atomically replace *path*, syncing both the file and parent directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw_temp)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _write_if_changed(path: Path, data: bytes, *, mode: int) -> bool:
    try:
        if path.read_bytes() == data and stat.S_IMODE(path.stat().st_mode) == mode:
            return False
    except FileNotFoundError:
        pass
    _atomic_write(path, data, mode=mode)
    return True


def _captured_path(paths: ServicePaths, value: str | None = None) -> str:
    """Capture the installing shell PATH and ensure the stable uv shim is visible."""

    entries = [entry for entry in (value or DEFAULT_PATH).split(os.pathsep) if entry]
    shim_dir = str(paths.binary.parent)
    if shim_dir not in entries:
        entries.insert(0, shim_dir)
    return os.pathsep.join(entries)


def _probe_base_url(paths: ServicePaths) -> str:
    host = "127.0.0.1"
    port = 9100
    try:
        payload = tomllib.loads(paths.config.read_text(encoding="utf-8"))
        host = str(payload.get("host", host))
        port = int(payload.get("port", port))
    except FileNotFoundError:
        pass
    except (OSError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise ServiceError(
            f"cannot read service endpoint from {paths.config}: {exc}"
        ) from None

    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _wait_for_gateway(paths: ServicePaths, *, attempts: int = 60) -> None:
    """Wait until launchd's process is alive and its configured backends are ready."""

    base_url = _probe_base_url(paths)
    last_detail = "no response"
    for attempt in range(attempts):
        try:
            statuses = []
            for endpoint in ("health", "ready"):
                with urllib.request.urlopen(  # noqa: S310 - operator-configured local endpoint
                    f"{base_url}/{endpoint}", timeout=2
                ) as response:
                    statuses.append(response.status)
            if statuses == [200, 200]:
                return
            last_detail = f"HTTP statuses {statuses}"
        except urllib.error.HTTPError as exc:
            last_detail = f"HTTP {exc.code} from {exc.url}"
        except (OSError, urllib.error.URLError) as exc:
            last_detail = str(exc)
        if attempt + 1 < attempts:
            time.sleep(1)
    raise ServiceError(
        f"resident service did not pass /health and /ready at {base_url}: {last_detail}"
    )


@dataclass(frozen=True)
class _FileSnapshot:
    data: bytes
    mode: int


def _snapshot(path: Path) -> _FileSnapshot | None:
    try:
        return _FileSnapshot(path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    except FileNotFoundError:
        return None


def _restore(path: Path, snapshot: _FileSnapshot | None) -> None:
    if snapshot is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_write(path, snapshot.data, mode=snapshot.mode)


def _wrapper_bytes(paths: ServicePaths, binary: Path) -> bytes:
    return (
        "#!/bin/sh\n"
        f'binary="{binary}"\n'
        'if [ ! -x "$binary" ]; then\n'
        f'    printf "%s\\n" "mcp-gateway executable missing at {binary}; '
        f'leaving {LABEL} inert until explicit repair or removal" >&2\n'
        "    exit 0\n"
        "fi\n"
        'exec "$binary" --foreground\n'
    ).encode()


def _plist_bytes(paths: ServicePaths, captured_path: str) -> bytes:
    payload: dict[str, Any] = {
        "Label": LABEL,
        "ProgramArguments": [str(paths.wrapper)],
        "WorkingDirectory": str(paths.config_dir),
        "RunAtLoad": True,
        # Restart crashes/non-zero failures, but treat the missing-binary wrapper's
        # successful exit as deliberately inert rather than a crash loop.
        "KeepAlive": {"SuccessfulExit": False},
        "EnvironmentVariables": {
            "PATH": captured_path,
            "MCP_GATEWAY_CONFIG": str(paths.config),
            "MCP_GATEWAY_SERVICE_TEMPLATE_VERSION": TEMPLATE_VERSION,
        },
        "StandardOutPath": str(paths.state_dir / "out.log"),
        "StandardErrorPath": str(paths.state_dir / "err.log"),
        "ExitTimeOut": 15,
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


def _stored_environment(paths: ServicePaths) -> dict[str, str]:
    try:
        payload = plistlib.loads(paths.plist.read_bytes())
        environment = payload.get("EnvironmentVariables", {})
        if isinstance(environment, dict):
            return {
                str(key): str(value)
                for key, value in environment.items()
                if isinstance(key, str) and isinstance(value, str)
            }
    except (FileNotFoundError, OSError, plistlib.InvalidFileException, ValueError):
        pass
    return {}


def installed_template_version(paths: ServicePaths | None = None) -> str | None:
    """Return the installed template version, including legacy/malformed as None."""

    actual = paths or service_paths()
    return _stored_environment(actual).get("MCP_GATEWAY_SERVICE_TEMPLATE_VERSION")


def _run(runner: Runner, argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return runner(argv, check=False, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ServiceError(f"could not run {' '.join(argv)}: {exc}") from None


def _target(uid: int | None = None) -> str:
    return f"gui/{os.getuid() if uid is None else uid}/{LABEL}"


def _loaded_result(
    runner: Runner, *, uid: int | None = None
) -> subprocess.CompletedProcess[str]:
    return _run(runner, [LAUNCHCTL, "print", _target(uid)])


def service_loaded(runner: Runner = subprocess.run, *, uid: int | None = None) -> bool:
    """Return whether launchd currently owns the user service."""

    return _loaded_result(runner, uid=uid).returncode == 0


def _wait_until_unloaded(
    runner: Runner,
    sleep: Sleeper,
    *,
    uid: int | None,
    attempts: int = 20,
) -> None:
    for _ in range(attempts):
        if not service_loaded(runner, uid=uid):
            return
        sleep(0.5)
    raise ServiceError(f"{_target(uid)} remained loaded after 10 seconds")


def _bootstrap(
    paths: ServicePaths,
    runner: Runner,
    sleep: Sleeper,
    *,
    uid: int | None,
    attempts: int = 6,
) -> None:
    domain = f"gui/{os.getuid() if uid is None else uid}"
    result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(attempts):
        result = _run(runner, [LAUNCHCTL, "bootstrap", domain, str(paths.plist)])
        if result.returncode == 0:
            break
        if attempt + 1 < attempts:
            sleep(1)
    assert result is not None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ServiceError(f"launchctl bootstrap failed: {detail or result.returncode}")

    kickstart = _run(runner, [LAUNCHCTL, "kickstart", "-k", _target(uid)])
    if kickstart.returncode != 0:
        detail = (kickstart.stderr or kickstart.stdout).strip()
        raise ServiceError(
            f"launchctl kickstart failed: {detail or kickstart.returncode}"
        )


def _bootout(
    runner: Runner,
    sleep: Sleeper,
    *,
    uid: int | None,
) -> bool:
    if not service_loaded(runner, uid=uid):
        return False
    result = _run(runner, [LAUNCHCTL, "bootout", _target(uid)])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ServiceError(f"launchctl bootout failed: {detail or result.returncode}")
    _wait_until_unloaded(runner, sleep, uid=uid)
    return True


def _migrate_legacy_config(paths: ServicePaths) -> bool:
    """Preserve a checkout-era config before removing its stable symlink."""

    if paths.config.exists() or not paths.legacy_link.is_symlink():
        return False
    try:
        legacy_config = paths.legacy_link.resolve(strict=True) / "config.toml"
    except (OSError, RuntimeError):
        return False
    if not legacy_config.is_file():
        return False
    _atomic_write(paths.config, legacy_config.read_bytes(), mode=0o600)
    return True


def _activate_service(
    paths: ServicePaths,
    system: ServiceRuntime,
    *,
    changed: bool,
    loaded_before: bool,
    prior: tuple[_FileSnapshot | None, _FileSnapshot | None],
) -> bool:
    activation_started = False
    reloaded = False
    try:
        if loaded_before and changed:
            activation_started = True
            _bootout(system.runner, system.sleep, uid=system.uid)
            _bootstrap(paths, system.runner, system.sleep, uid=system.uid)
            reloaded = True
        elif not loaded_before:
            activation_started = True
            _bootstrap(paths, system.runner, system.sleep, uid=system.uid)
            reloaded = True
        (system.probe or _wait_for_gateway)(paths)
        return reloaded
    except Exception as exc:
        rollback_error: Exception | None = None
        if activation_started:
            try:
                _bootout(system.runner, system.sleep, uid=system.uid)
                _restore(paths.wrapper, prior[0])
                _restore(paths.plist, prior[1])
                if loaded_before and prior[1] is not None:
                    _bootstrap(paths, system.runner, system.sleep, uid=system.uid)
            except (
                Exception
            ) as rollback_exc:  # pragma: no cover - catastrophic OS failure
                rollback_error = rollback_exc
        detail = f"service activation failed: {exc}"
        if rollback_error is not None:
            detail += f"; rollback also failed: {rollback_error}"
        elif activation_started:
            detail += "; previous service state restored"
        raise ServiceError(detail) from None


def install_service(
    *,
    paths: ServicePaths | None = None,
    binary: Path | None = None,
    path_value: str | None = None,
    force_restart: bool = False,
    runtime: ServiceRuntime | None = None,
) -> InstallResult:
    """Install or repair the resident LaunchAgent without double-bootstrap."""

    system = runtime or ServiceRuntime()
    _require_macos(system.platform)
    actual = paths or service_paths()
    executable = (binary or actual.binary).expanduser().resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ServiceError(
            f"stable executable not found at {executable}; install the tool first with "
            "`uv tool install <package-or-checkout>`"
        )

    # launchd opens these log paths before exec, so create state first.
    actual.state_dir.mkdir(parents=True, exist_ok=True)
    actual.config_dir.mkdir(parents=True, exist_ok=True)
    actual.agents_dir.mkdir(parents=True, exist_ok=True)
    actual.wrapper_dir.mkdir(parents=True, exist_ok=True)

    migrated_config = _migrate_legacy_config(actual)
    captured_path = _captured_path(
        actual, path_value if path_value is not None else os.environ.get("PATH")
    )
    prior_wrapper = _snapshot(actual.wrapper)
    prior_plist = _snapshot(actual.plist)
    try:
        wrapper_changed = _write_if_changed(
            actual.wrapper, _wrapper_bytes(actual, executable), mode=0o700
        )
        plist_changed = _write_if_changed(
            actual.plist, _plist_bytes(actual, captured_path), mode=0o600
        )
    except Exception as exc:
        _restore(actual.wrapper, prior_wrapper)
        _restore(actual.plist, prior_plist)
        raise ServiceError(
            f"could not install service files atomically: {exc}"
        ) from None

    changed = wrapper_changed or plist_changed
    reloaded = _activate_service(
        actual,
        system,
        changed=changed or force_restart,
        loaded_before=service_loaded(system.runner, uid=system.uid),
        prior=(prior_wrapper, prior_plist),
    )

    _write_if_changed(actual.prompted_marker, b"installed\n", mode=0o600)
    removed_legacy_link = False
    if actual.legacy_link.is_symlink():
        actual.legacy_link.unlink()
        removed_legacy_link = True

    return InstallResult(
        changed=changed,
        reloaded=reloaded,
        migrated_config=migrated_config,
        removed_legacy_link=removed_legacy_link,
    )


def refresh_installed_service(
    *,
    paths: ServicePaths | None = None,
    binary: Path | None = None,
    platform: str | None = None,
) -> bool:
    """Atomically refresh stale installed files without restarting this process."""

    if (platform or sys.platform) != "darwin":
        return False
    actual = paths or service_paths()
    if not actual.plist.exists():
        return False
    environment = _stored_environment(actual)
    expected_wrapper = _wrapper_bytes(
        actual, (binary or actual.binary).expanduser().resolve()
    )
    stale = (
        environment.get("MCP_GATEWAY_SERVICE_TEMPLATE_VERSION") != TEMPLATE_VERSION
        or not actual.wrapper.exists()
        or actual.wrapper.read_bytes() != expected_wrapper
    )
    if not stale:
        return False
    captured_path = _captured_path(
        actual, environment.get("PATH") or os.environ.get("PATH")
    )
    actual.state_dir.mkdir(parents=True, exist_ok=True)
    actual.config_dir.mkdir(parents=True, exist_ok=True)
    _write_if_changed(actual.wrapper, expected_wrapper, mode=0o700)
    _write_if_changed(actual.plist, _plist_bytes(actual, captured_path), mode=0o600)
    return True


def mark_prompt_declined(paths: ServicePaths | None = None) -> None:
    """Persist an explicit first-run decline so the prompt is shown only once."""

    actual = paths or service_paths()
    _atomic_write(actual.prompted_marker, b"declined\n", mode=0o600)


def should_offer_service_install(
    *,
    paths: ServicePaths | None = None,
    platform: str | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> bool:
    """Return whether this invocation may ask the one-time interactive prompt."""

    actual = paths or service_paths()
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    return (
        (platform or sys.platform) == "darwin"
        and not actual.prompted_marker.exists()
        and not actual.plist.exists()
        and source.isatty()
        and sink.isatty()
    )


def uninstall_service(
    *,
    paths: ServicePaths | None = None,
    purge_data: bool = False,
    runtime: ServiceRuntime | None = None,
) -> UninstallResult:
    """Boot out and remove current plus checkout-era service artifacts."""

    system = runtime or ServiceRuntime()
    _require_macos(system.platform)
    actual = paths or service_paths()
    unloaded = _bootout(system.runner, system.sleep, uid=system.uid)
    removed: list[Path] = []
    for path in (actual.plist, actual.wrapper, actual.prompted_marker):
        if path.is_file() or path.is_symlink():
            path.unlink()
            removed.append(path)
    if actual.legacy_link.is_symlink():
        actual.legacy_link.unlink()
        removed.append(actual.legacy_link)

    for directory in (actual.wrapper_dir, actual.legacy_link.parent):
        try:
            directory.rmdir()
        except OSError:
            pass

    if purge_data:
        for directory in (actual.config_dir, actual.state_dir):
            if directory.exists():
                shutil.rmtree(directory)
                removed.append(directory)

    return UninstallResult(
        unloaded=unloaded,
        removed=tuple(removed),
        purged_data=purge_data,
    )


def _parse_pid(output: str) -> int | None:
    match = re.search(r"(?:^|\n)\s*pid\s*=\s*(\d+)\s*(?:\n|$)", output)
    return int(match.group(1)) if match else None


def resource_status(
    runner: Runner = subprocess.run,
    *,
    uid: int | None = None,
) -> ResourceStatus:
    """Measure the resident process tree once, with no polling or background task."""

    job = _loaded_result(runner, uid=uid)
    if job.returncode != 0:
        return ResourceStatus(False, None, None, 0, None, None, None)
    pid = _parse_pid(job.stdout)
    if pid is None:
        return ResourceStatus(True, None, None, 0, None, None, None)

    result = _run(runner, [PS, "-axo", "pid=,ppid=,rss=,pcpu=,command="])
    if result.returncode != 0:
        return ResourceStatus(True, pid, None, 0, None, None, None)

    rows: dict[int, tuple[int, int, float]] = {}
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=4)
        if len(parts) < 4:
            continue
        try:
            process_id, parent_id, rss_kib = map(int, parts[:3])
            cpu = float(parts[3])
        except ValueError:
            continue
        rows[process_id] = (parent_id, rss_kib * 1024, cpu)

    root = rows.get(pid)
    if root is None:
        return ResourceStatus(True, pid, None, 0, None, None, None)
    descendants: set[int] = set()
    frontier = {pid}
    while frontier:
        children = {
            process_id
            for process_id, (parent_id, _rss, _cpu) in rows.items()
            if parent_id in frontier and process_id not in descendants
        }
        descendants.update(children)
        frontier = children

    child_rss = sum(rows[process_id][1] for process_id in descendants)
    gateway_rss = root[1]
    return ResourceStatus(
        loaded=True,
        pid=pid,
        gateway_rss_bytes=gateway_rss,
        child_processes=len(descendants),
        children_rss_bytes=child_rss,
        total_rss_bytes=gateway_rss + child_rss,
        cpu_percent=root[2],
    )
