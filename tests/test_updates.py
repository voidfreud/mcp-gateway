"""Contracts for bounded, opt-out PyPI update checks."""

from __future__ import annotations

import json
import urllib.error

import pytest

from mcp_gateway import updates


def _files(*, yanked: bool = False) -> list[dict[str, bool]]:
    return [{"yanked": yanked}]


def test_stable_version_contract_rejects_prereleases_and_loose_inputs():
    assert updates.is_stable_version("1.2.3") is True
    for value in ("v1.2.3", "1.2", "1.2.3rc1", "01.2.3", "1.2.3 "):
        assert updates.is_stable_version(value) is False


def test_latest_version_ignores_prereleases_empty_and_fully_yanked(monkeypatch):
    monkeypatch.setattr(
        updates,
        "_fetch_releases",
        lambda **_kwargs: {
            "1.9.0": _files(),
            "2.0.0rc1": _files(),
            "2.0.0": _files(yanked=True),
            "1.10.0": _files(),
            "9.0.0": [],
        },
    )

    assert updates.latest_version() == "1.10.0"
    assert updates.version_exists("1.10.0") is True
    assert updates.version_exists("2.0.0") is False
    assert updates.version_exists("2.0.0", allow_yanked=True) is True


def test_check_now_reports_available_with_stable_json_shape(monkeypatch):
    monkeypatch.setattr(updates, "installed_version", lambda: "1.2.3")
    monkeypatch.setattr(
        updates,
        "_fetch_releases",
        lambda **_kwargs: {"1.2.3": _files(), "1.3.0": _files()},
    )

    status = updates.check_now()

    assert status.keys() == {
        "current_version",
        "latest_version",
        "available",
        "checked_at",
        "error",
    }
    assert status["current_version"] == "1.2.3"
    assert status["latest_version"] == "1.3.0"
    assert status["available"] is True
    assert status["checked_at"].endswith("+00:00")
    assert status["error"] is None
    assert updates.current_status() == status
    assert updates.current_status() is not status


def test_check_now_is_offline_tolerant_but_explicit_lookup_fails(monkeypatch):
    def fail(**_kwargs):
        raise updates.UpdateCheckError("offline")

    monkeypatch.setattr(updates, "_fetch_releases", fail)
    monkeypatch.setattr(updates, "installed_version", lambda: "1.2.3")

    status = updates.check_now()

    assert status["available"] is False
    assert status["latest_version"] is None
    assert status["error"] == "offline"
    with pytest.raises(updates.UpdateCheckError, match="offline"):
        updates.latest_version()


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]


def test_fetch_uses_fixed_user_agent_timeout_and_bounded_read(monkeypatch):
    observed: dict[str, object] = {}
    response = _Response({"releases": {"1.0.0": _files()}})

    def urlopen(request, *, timeout):
        observed["url"] = request.full_url
        observed["user_agent"] = request.get_header("User-agent")
        observed["timeout"] = timeout
        return response

    monkeypatch.setattr(updates.urllib.request, "urlopen", urlopen)

    assert updates.version_exists("1.0.0", timeout=0.25) is True
    assert observed == {
        "url": updates.PYPI_JSON_URL,
        "user_agent": updates.USER_AGENT,
        "timeout": 0.25,
    }


def test_fetch_normalizes_network_failure(monkeypatch):
    def fail(*_args, **_kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(updates.urllib.request, "urlopen", fail)

    with pytest.raises(updates.UpdateCheckError, match="could not check"):
        updates.latest_version()
