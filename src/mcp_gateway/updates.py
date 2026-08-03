"""Bounded PyPI version checks for the public mcp-gateway distribution.

The daemon uses this module only when ``update_check`` is enabled. Checks are
read-only, run off the event loop, tolerate offline/error responses, and never
apply an update. The explicit CLI update path uses the same fixed endpoint to
resolve or validate an exact release before delegating installation to ``uv``.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import re
import threading
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from mcp_gateway.metadata import gateway_version

DISTRIBUTION_NAME = "mcp-local-gateway"
PYPI_JSON_URL = f"https://pypi.org/pypi/{DISTRIBUTION_NAME}/json"
CHECK_INTERVAL = 86400.0
FETCH_TIMEOUT = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024
CURRENT_VERSION = gateway_version()
USER_AGENT = f"mcp-gateway-update-check/{CURRENT_VERSION}"

_STABLE_VERSION_RE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
_status_lock = threading.Lock()
_status: dict[str, Any] = {
    "current_version": CURRENT_VERSION if CURRENT_VERSION != "unknown" else None,
    "latest_version": None,
    "available": False,
    "checked_at": None,
    "error": None,
}


class UpdateCheckError(RuntimeError):
    """A version lookup could not be completed safely."""


def is_stable_version(version: str) -> bool:
    """Return whether *version* is one of this project's stable SemVer tags."""

    return _STABLE_VERSION_RE.fullmatch(version) is not None


def _version_key(version: str) -> tuple[int, int, int]:
    match = _STABLE_VERSION_RE.fullmatch(version)
    if match is None:
        raise ValueError(version)
    return int(match[1]), int(match[2]), int(match[3])


def installed_version() -> str | None:
    """Return the installed public distribution version when available."""

    try:
        return importlib.metadata.version(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        fallback = gateway_version()
        return fallback if fallback != "unknown" else None


def _fetch_releases(
    *, url: str = PYPI_JSON_URL, timeout: float = FETCH_TIMEOUT
) -> Mapping[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise UpdateCheckError("PyPI response exceeded 1 MiB")
        payload = json.loads(raw)
    except UpdateCheckError:
        raise
    except (OSError, ValueError) as exc:
        raise UpdateCheckError(
            f"could not check {DISTRIBUTION_NAME} on PyPI: {exc}"
        ) from None
    releases = payload.get("releases") if isinstance(payload, dict) else None
    if not isinstance(releases, dict):
        raise UpdateCheckError(f"unexpected PyPI response for {DISTRIBUTION_NAME}")
    return releases


def _published_versions(
    releases: Mapping[str, Any], *, include_yanked: bool = False
) -> list[str]:
    versions: list[str] = []
    for version, files in releases.items():
        if not isinstance(version, str) or not is_stable_version(version):
            continue
        if not isinstance(files, list) or not any(
            isinstance(file, dict) and (include_yanked or not file.get("yanked", False))
            for file in files
        ):
            continue
        versions.append(version)
    return versions


def current_status() -> dict[str, Any]:
    """Return a JSON-safe snapshot without performing network I/O."""

    with _status_lock:
        return dict(_status)


def check_now(
    *, url: str = PYPI_JSON_URL, timeout: float = FETCH_TIMEOUT
) -> dict[str, Any]:
    """Perform one bounded check; record rather than raise expected failures."""

    current = installed_version()
    try:
        versions = _published_versions(_fetch_releases(url=url, timeout=timeout))
        if not versions:
            raise UpdateCheckError(
                f"no stable published releases found for {DISTRIBUTION_NAME}"
            )
        latest = max(versions, key=_version_key)
        available = (
            current is not None
            and is_stable_version(current)
            and _version_key(latest) > _version_key(current)
        )
        error = None
    except UpdateCheckError as exc:
        latest = None
        available = False
        error = str(exc)

    status = {
        "current_version": current,
        "latest_version": latest,
        "available": available,
        "checked_at": datetime.now(UTC).isoformat(),
        "error": error,
    }
    with _status_lock:
        _status.clear()
        _status.update(status)
        return dict(_status)


def latest_version(*, url: str = PYPI_JSON_URL, timeout: float = FETCH_TIMEOUT) -> str:
    """Return the latest stable published version or raise on lookup failure."""

    releases = _fetch_releases(url=url, timeout=timeout)
    versions = _published_versions(releases)
    if not versions:
        raise UpdateCheckError(
            f"no stable published releases found for {DISTRIBUTION_NAME}"
        )
    return max(versions, key=_version_key)


def version_exists(  # noqa: FBT001, FBT002 - keyword-only policy flag
    version: str,
    *,
    allow_yanked: bool = False,
    url: str = PYPI_JSON_URL,
    timeout: float = FETCH_TIMEOUT,
) -> bool:
    """Return whether an exact stable release has an installable file."""

    if not is_stable_version(version):
        return False
    return version in _published_versions(
        _fetch_releases(url=url, timeout=timeout), include_yanked=allow_yanked
    )


async def monitor(log: Any | None = None) -> None:
    """Check immediately and daily without blocking or destabilizing the daemon."""

    while True:
        try:
            status = await asyncio.to_thread(check_now)
            if status["error"] is not None:
                if log is not None:
                    log.warning("update_check_failed", error=status["error"])
            elif status["available"] and log is not None:
                log.info(
                    "update_available",
                    latest=status["latest_version"],
                    current=status["current_version"],
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive task isolation
            if log is not None:
                log.warning("update_check_error", error=str(exc))
        await asyncio.sleep(CHECK_INTERVAL)
