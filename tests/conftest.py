"""Suite-wide guardrails.

Every test runs against a throwaway state dir: the module-level
``admin.STATE_DIR``/``DEFAULTS_DIR``/``BACKUP_DIR`` point at the REAL
``~/.local/state/mcp-gateway`` (which crash-recovery restores from, #96), and
any save-exercising test that forgets to patch them writes fixture configs
into real user state (#135). ``server.py`` reads these via ``admin.<attr>`` at
call time, so patching the module attributes covers every consumer.
"""

import pytest

from mcp_gateway import admin


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(admin, "STATE_DIR", state)
    monkeypatch.setattr(admin, "DEFAULTS_DIR", state / "defaults")
    monkeypatch.setattr(admin, "BACKUP_DIR", state / "backups")
    # #43: the auto-refresh throttle is module state — a fresh dict per test so
    # one test's refresh can't throttle another's.
    monkeypatch.setattr(admin, "_last_refresh", {})
    # #46: the cc-registrations cache is likewise module state — reset per test.
    monkeypatch.setattr(admin, "_cc_reg_cache", {"ts": 0.0, "output": None})
    return state
