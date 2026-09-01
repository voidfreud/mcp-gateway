"""Backend runners heal on their own: a backend that is down at boot or whose
warm session dies comes back without a daemon restart."""

from __future__ import annotations

import time

import pytest
import structlog
from starlette.testclient import TestClient

from mcp_gateway import admin, server
from mcp_gateway import config_loader as cl


class _Mounts:
    """A stand-in for _mount_backend: fails the first *fail_first* attempts,
    then registers a placeholder proxy and keeps the death callback."""

    def __init__(self, fail_first: int = 0) -> None:
        self.fail_first = fail_first
        self.attempts = 0
        self.deaths: list = []

    async def __call__(  # noqa: PLR0913, PLR0917 — mirrors _mount_backend
        self,
        app,
        stack,
        b,
        cfg,
        all_tools,
        meta,
        captured,
        rt,
        log,
        handler=None,
        on_death=None,
        oauth=None,
    ) -> bool:
        self.attempts += 1
        if self.attempts <= self.fail_first:
            rt.set_status(b.name, "down", error="ConnectError: refused")
            return False
        rt.mount(b.name, object(), [])
        self.deaths.append(on_death)
        return True


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(server, "RECONNECT_MIN", 0.02)
    monkeypatch.setattr(server, "RECONNECT_MAX", 0.1)


def _app(tmp_path, monkeypatch, mounts: _Mounts):
    async def fake_refresh(*_a, **_k):
        return {"status": "throttled"}

    monkeypatch.setattr(server, "_mount_backend", mounts)
    monkeypatch.setattr(admin, "refresh_and_reload", fake_refresh)
    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}]}
    )
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    app = server._build_app(
        cfg, structlog.get_logger("test"), {}, {}, {}, config_path=str(path)
    )
    return app, path


def _wait_for(pred, tries=300, delay=0.01) -> bool:
    for _ in range(tries):
        if pred():
            return True
        time.sleep(delay)
    return False


def _state(client) -> dict:
    return client.get("/ready").json()["backends"].get("b", {})


def test_backend_down_at_boot_reports_reconnecting_then_recovers(tmp_path, monkeypatch):
    mounts = _Mounts(fail_first=2)
    app, _ = _app(tmp_path, monkeypatch, mounts)
    with TestClient(app) as client:
        assert _wait_for(lambda: _state(client).get("state") == "reconnecting")
        r = client.get("/ready")
        assert r.status_code == 503
        entry = r.json()["backends"]["b"]
        assert entry["error"] == "ConnectError: refused"
        assert entry["retry_in"] > 0
        assert "b" in r.json()["missing"]
        assert _wait_for(lambda: client.get("/ready").status_code == 200)
        assert _state(client) == {"state": "up"}
        assert mounts.attempts == 3


def test_dead_warm_session_is_replaced(tmp_path, monkeypatch):
    mounts = _Mounts()
    app, _ = _app(tmp_path, monkeypatch, mounts)
    with TestClient(app) as client:
        assert _wait_for(lambda: mounts.attempts == 1)
        assert mounts.deaths == [mounts.deaths[0]] and mounts.deaths[0] is not None
        # The status probe cannot talk to the placeholder proxy, reports the
        # error, and wakes the runner exactly as a dying tool call would.
        r = client.get("/admin/api/status")
        assert r.json()["backends"]["b"]["state"] == "error"
        assert _wait_for(lambda: mounts.attempts == 2)
        assert _wait_for(lambda: _state(client) == {"state": "up"})


def test_stateless_backend_has_no_death_callback(tmp_path, monkeypatch):
    mounts = _Mounts()
    app, path = _app(tmp_path, monkeypatch, mounts)
    cfg = cl.load(str(path))
    cfg.backends[0].stateless = True
    cl.save(cfg, str(path))
    with TestClient(app) as client:
        client.post("/admin/api/backend/b/stateless", json={"value": True})
        assert _wait_for(lambda: mounts.attempts == 2)
        assert mounts.deaths[-1] is None


def test_disabling_a_backend_stops_its_reconnects(tmp_path, monkeypatch):
    mounts = _Mounts(fail_first=10_000)
    app, path = _app(tmp_path, monkeypatch, mounts)
    with TestClient(app) as client:
        assert _wait_for(lambda: mounts.attempts >= 2)
        cfg = cl.load(str(path))
        cfg.backends[0].enabled = False
        cl.save(cfg, str(path))
        assert _wait_for(lambda: client.get("/ready").json()["enabled"] == [])
        time.sleep(0.3)
        seen = mounts.attempts
        time.sleep(0.3)
        assert mounts.attempts == seen
        assert client.get("/ready").json()["backends"] == {}


def test_shutdown_while_reconnecting_is_prompt(tmp_path, monkeypatch):
    mounts = _Mounts(fail_first=10_000)
    app, _ = _app(tmp_path, monkeypatch, mounts)
    started = time.perf_counter()
    with TestClient(app):
        assert _wait_for(lambda: mounts.attempts >= 2)
    assert time.perf_counter() - started < server.SHUTDOWN_GRACE


@pytest.mark.parametrize(
    ("previous", "lived", "expected"),
    [
        (0.0, None, "min"),  # first failed mount waits the minimum
        (0.02, None, 0.04),  # each further failure doubles
        (0.1, None, "max"),  # and never exceeds the maximum
        (0.0, 0.01, "min"),  # a short-lived session counts as a failure
        (0.04, 0.01, 0.08),
        (0.08, 0.1, 0.0),  # a session that lasted the maximum retries at once
        (0.08, 5.0, 0.0),
    ],
)
def test_reconnect_delay_schedule(previous, lived, expected):
    expected = {"min": server.RECONNECT_MIN, "max": server.RECONNECT_MAX}.get(
        expected, expected
    )
    assert server._reconnect_delay(previous, lived) == pytest.approx(expected)


def test_network_failures_count_as_session_death():
    import httpx

    assert server.is_session_death(httpx.ConnectError("All connection attempts failed"))
    assert server.is_session_death(httpx.ReadError(""))
    assert server.is_session_death(RuntimeError("connection refused"))
    assert not server.is_session_death(httpx.ReadTimeout("slow tool"))
