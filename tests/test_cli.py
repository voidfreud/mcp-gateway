"""Behavioral contracts for the scriptable mcp-gateway control CLI (issue #284).

These tests drive ``mcp_gateway.cli.main`` end to end with a deterministic
fake ``AdminClient`` substituted for the real HTTP transport, so they pin the
observable CLI contract — exit codes, stdout/stderr shape, the exact admin-API
method/path/payload each command produces, and token handling — without any
network access or a real config on disk.

Assertions target behavior only: request method/path/payload, exit status,
whether stdout carries exactly one JSON value, and whether secrets leak into
output.  No assertion depends on incidental formatting or source text.
"""

from __future__ import annotations

import http.server
import io
import json
import os
import threading
from pathlib import Path

import pytest

from mcp_gateway import cli, cli_common, service
from mcp_gateway.cli_common import AdminClient as _RealAdminClient
from mcp_gateway.cli_common import CLIError
from mcp_gateway.metadata import gateway_version

# ---------------------------------------------------------------------------
# Deterministic fake AdminClient
# ---------------------------------------------------------------------------


class FakeAdminClient:
    """Stand-in for ``cli_common.AdminClient`` (same constructor and methods).

    Records every ``request``/``stream`` call and serves canned responses keyed
    by ``(method, path)``; a configured exception is raised instead, exactly
    like the real client converts HTTP/transport failures into ``CLIError``.
    """

    instances: list[FakeAdminClient] = []
    calls: list[dict] = []
    responses: dict[tuple[str, str], object] = {}
    stream_lines: dict[tuple[str, str], list[str]] = {}

    def __init__(self, base_url: str, token: str | None, timeout: float = 30.0):
        self.base_url = base_url
        self.token = token
        self.timeout = timeout
        FakeAdminClient.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.calls = []
        cls.responses = {}
        cls.stream_lines = {}

    def request(self, method, path, *, payload=None, params=None):
        FakeAdminClient.calls.append(
            {
                "method": method,
                "path": path,
                "payload": payload,
                "params": params,
            }
        )
        key = (method, path)
        if key in FakeAdminClient.responses:
            value = FakeAdminClient.responses[key]
            if isinstance(value, BaseException):
                raise value
            return value
        raise AssertionError(f"unexpected request: {method} {path}")

    def stream(self, path, *, params=None):
        FakeAdminClient.calls.append(
            {
                "method": "STREAM",
                "path": path,
                "payload": None,
                "params": params,
            }
        )
        key = (path, _freeze(params))
        if key in FakeAdminClient.stream_lines:
            return iter(FakeAdminClient.stream_lines[key])
        raise AssertionError(f"unexpected stream: {path} {params!r}")


def _freeze(value) -> object:
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


# Every env var the suite may touch (config paths, secrets file, token
# sources, and the named-token vars used by --token-env tests). Cleared by
# default; tests opt in explicitly with monkeypatch.setenv.
_SUITE_ENV_VARS = (
    "MCP_GATEWAY_CONFIG",
    "MCP_GATEWAY_SECRETS",
    "MCP_GATEWAY_ADMIN_TOKEN",
    "ADMIN_BEARER",
    "OAUTH_ADMIN",
    "MY_NAMED_TOKEN",
    "UNSET_VAR",
)


@pytest.fixture(autouse=True)
def fake_admin(monkeypatch, tmp_path):
    """Install the fake transport and isolate the environment before each test.

    ``cli.py`` imports ``AdminClient`` by name from ``cli_common``, so the
    binding inside ``mcp_gateway.cli`` is patched; ``cli_common`` itself is
    patched too in case any domain module constructs a client directly.

    HOME is redirected to the throwaway dir and every config/secrets/token
    variable the suite uses is deleted, so no test can accidentally read the
    developer's real config or resolve a real token; tests set what they need
    explicitly.
    """
    FakeAdminClient.reset()
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in _SUITE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "AdminClient", FakeAdminClient, raising=False)
    monkeypatch.setattr(cli_common, "AdminClient", FakeAdminClient, raising=False)
    return FakeAdminClient


# ---------------------------------------------------------------------------
# Fixture data (shapes match the real admin API responses)
# ---------------------------------------------------------------------------


def make_backend(name: str = "db", **overrides) -> dict:
    backend = {
        "id": f"gateway-{name}",
        "name": name,
        "display_name": None,
        "enabled": True,
        "endpoint": f"/{name}/mcp",
        "transport": "stdio",
        "url": None,
        "command": "sqlite3",
        "args": [],
        "auth_header": None,
        "auth_value": None,
        "stateless": False,
        "always_load": False,
        "introspected": True,
        "default_instructions": "default blurb",
        "instructions": None,
        # #286: stored/effective metadata limits + UTF-8 instruction bytes
        # (None stored = inherit the gateway global).
        "server_instructions_max_bytes": None,
        "effective_server_instructions_max_bytes": 2048,
        "tool_description_max_bytes": None,
        "effective_tool_description_max_bytes": None,
        "default_instructions_bytes": 13,
        "instructions_bytes": 13,
        "server_info": {"name": "sqlite", "version": "1.0"},
        "dangling": [],
        "tools": [
            {
                "original": "query",
                "default_name": "query",
                "default_title": None,
                "default_description": "Run a query",
                "name": None,
                "title": None,
                "description": None,
                "enabled": True,
                "always_load": False,
                "max_result_chars": None,
                # #286: stored/effective description cap + UTF-8 byte counts.
                "description_max_bytes": None,
                "effective_description_max_bytes": None,
                "default_description_bytes": 12,
                "effective_description_bytes": 12,
                "validate": None,
                "post_process": None,
                "hook_error": None,
                "output_schema": None,
                "meta": None,
                "annotations": None,
                "params": [],
            }
        ],
        "resources": [
            {
                "uri": "db://schema",
                "template": False,
                "default_name": "Schema",
                "default_title": None,
                "default_description": None,
                "mime_type": None,
                "name": None,
                "title": None,
                "description": None,
                "enabled": True,
            }
        ],
        "prompts": [
            {
                "original": "explain",
                "default_name": "explain",
                "default_title": None,
                "default_description": None,
                "name": None,
                "title": None,
                "description": None,
                "enabled": True,
                "args": [],
            }
        ],
    }
    backend.update(overrides)
    return backend


def make_state(*backends) -> dict:
    return {
        "host": "127.0.0.1",
        "port": 9100,
        "version": "1.3.2",
        "bearer_token": None,
        "introspect_interval": 0,
        "log_level": "INFO",
        "log_max_bytes": 10485760,
        "log_backup_count": 3,
        "update_check": True,
        # #286: gateway-global UTF-8 metadata limits (None tool cap = unbounded).
        "server_instructions_max_bytes": 2048,
        "tool_description_max_bytes": None,
        "update": {"status": "disabled"},
        "auth_mode": "none",
        "oauth": None,
        "backends": list(backends) or [make_backend()],
    }


def make_status(*states) -> dict:
    return {"backends": dict(states)}


def make_virtual_tool(name: str = "v1", *, enabled: bool = False) -> dict:
    return {
        "name": name,
        "description": "A virtual tool",
        "always_load": False,
        "dispatch": "all",
        "inputs": [{"name": "query", "type": "string", "required": True}],
        "members": [
            {"label": "db/query", "backend_id": "gateway-db", "tool_original": "query"}
        ],
        "router": None,
        "routing_input_max_chars": 4096,
        "max_result_bytes": 262144,
        # #286: stored/effective description cap + UTF-8 byte count.
        "description_max_bytes": None,
        "effective_description_max_bytes": None,
        "description_bytes": 14,
        "failure_policy": "partial",
        "enabled": enabled,
        "resolution": {
            "ok": True,
            "members": [
                {
                    "label": "db/query",
                    "resolved": True,
                    "backend": "db",
                    "backend_effective": "db",
                    "tool_effective": "query",
                }
            ],
        },
    }


def virtual_list_response(*tools) -> dict:
    return {"mounted": True, "endpoint": "/virtual/mcp", "tools": list(tools)}


# ---------------------------------------------------------------------------
# Runner helpers
# ---------------------------------------------------------------------------


def run_cli(argv, *, stdin_text: str = "", env: dict | None = None):
    """Run ``cli.main`` and normalize the exit to an integer.

    ``main`` raises SystemExit (0 success/help, 1 user-facing error, 2 usage);
    argparse may also raise it during parse.  This helper captures stdout and
    stderr so tests can assert on the streams, and converts SystemExit.code
    into a plain int.
    """
    out, err = io.StringIO(), io.StringIO()
    stdin = io.StringIO(stdin_text)
    try:
        rc = cli.main(
            list(argv),
            stdin=stdin,
            stdout=out,
            stderr=err,
        )
    except SystemExit as exc:
        code = exc.code
        if code is None:
            rc = 0
        elif isinstance(code, int):
            rc = code
        else:
            rc = 1
    else:
        # Success paths (foreground dispatch, version) return without raising.
        rc = 0
    return rc, out.getvalue(), err.getvalue()


def expect_calls(*expected):
    """Assert the fake client saw exactly these calls, in order."""
    assert FakeAdminClient.calls == [dict(call) for call in expected], (
        FakeAdminClient.calls
    )


def call(method: str, path: str, *, payload=None, params=None) -> dict:
    return {"method": method, "path": path, "payload": payload, "params": params}


def load_json(out: str):
    """Assert *out* is exactly one JSON value and return it."""
    try:
        return json.loads(out)
    except ValueError as exc:
        raise AssertionError(f"stdout is not exactly one JSON value: {out!r}") from exc


def json_contains(data, needle) -> bool:
    if data == needle:
        return True
    if isinstance(data, dict):
        return any(json_contains(v, needle) for v in data.values())
    if isinstance(data, list):
        return any(json_contains(v, needle) for v in data)
    return False


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Parser / help / usage
# ---------------------------------------------------------------------------


def test_root_help_lists_every_command_domain():
    rc, out, err = run_cli(["--help"])
    assert rc == 0
    assert err == ""
    assert "usage:" in out
    for domain in (
        "run",
        "version",
        "update",
        "service",
        "status",
        "check",
        "restart",
        "backend",
        "tool",
        "resource",
        "prompt",
        "instructions",
        "virtual",
        "settings",
        "logs",
    ):
        assert domain in out


def test_unknown_root_command_is_a_usage_error():
    rc, out, err = run_cli(["frobnicate"])
    assert rc == 2
    assert out == ""
    assert "frobnicate" in err


def test_missing_required_subcommand_is_a_usage_error():
    rc, out, err = run_cli(["service"])
    assert rc == 2
    assert out == ""
    assert "COMMAND" in err


def test_backend_help_lists_backend_subcommands():
    rc, out, err = run_cli(["backend", "--help"])
    assert rc == 0
    for sub in (
        "list",
        "show",
        "add",
        "remove",
        "rename",
        "display-name",
        "enable",
        "disable",
        "enable-all",
        "disable-all",
        "pin",
        "unpin",
        "session",
        "inspect",
        "refresh",
        "limits",
    ):
        assert sub in out


def test_limit_flags_appear_in_help():
    # #286: every command that reads/writes a metadata limit documents the
    # flag, its keyword, and the 1 MiB ceiling in --help.
    rc, out, err = run_cli(["settings", "set", "--help"])
    assert rc == 0
    assert "--server-instructions-max-bytes" in out
    assert "--tool-description-max-bytes" in out
    assert "N|unlimited" in out

    rc, out, err = run_cli(["backend", "limits", "--help"])
    assert rc == 0
    assert "--server-instructions-max-bytes" in out
    assert "--tool-description-max-bytes" in out
    assert "N|inherit" in out

    rc, out, err = run_cli(["tool", "set", "--help"])
    assert rc == 0
    assert "--description-max-bytes" in out
    assert "N|inherit" in out

    rc, out, err = run_cli(["virtual", "create", "--help"])
    assert rc == 0
    assert "--description-max-bytes" in out
    assert "N|inherit" in out


# ---------------------------------------------------------------------------
# No-argument foreground dispatch, `run`, and hidden legacy aliases
# ---------------------------------------------------------------------------


@pytest.fixture
def foreground(monkeypatch):
    """Patch the foreground launch path and record calls."""
    calls: list[str] = []
    monkeypatch.setattr(service, "refresh_installed_service", lambda *a, **k: True)
    monkeypatch.setattr(
        "mcp_gateway.server.run_foreground", lambda: calls.append("foreground")
    )
    return calls


def test_no_arguments_runs_foreground_without_install_prompt(foreground):
    rc, out, err = run_cli([])
    assert rc == 0
    assert foreground == ["foreground"]
    # The first-run service-install prompt is gone: nothing offered, nothing
    # written to stdout.
    assert out == ""
    assert err == ""
    assert "install" not in out.lower()


def test_run_subcommand_dispatches_foreground(foreground):
    rc, out, err = run_cli(["run"])
    assert rc == 0
    assert foreground == ["foreground"]


def test_legacy_foreground_alias_still_runs(foreground):
    rc, out, err = run_cli(["--foreground"])
    assert rc == 0
    assert foreground == ["foreground"]


def test_legacy_version_alias_and_version_subcommand_match():
    alias_rc, alias_out, _ = run_cli(["--version"])
    sub_rc, sub_out, _ = run_cli(["version"])
    assert alias_rc == 0
    assert sub_rc == 0
    assert gateway_version() in alias_out
    assert gateway_version() in sub_out


def test_version_json_emits_version_object():
    rc, out, err = run_cli(["version", "--json"])
    assert rc == 0
    assert load_json(out) == {"version": gateway_version()}


def _broken_config(tmp_path) -> Path:
    broken = tmp_path / "config.toml"
    broken.write_text("this is not [valid toml", encoding="utf-8")
    return broken


# Local (non-API) commands never read config or resolve tokens: they must work
# even when the config file is malformed or a --token-env variable is missing.


def test_version_ignores_malformed_config_and_missing_token(
    fake_admin, tmp_path, monkeypatch
):
    monkeypatch.setenv("MCP_GATEWAY_CONFIG", str(_broken_config(tmp_path)))

    rc, out, err = run_cli(["--token-env", "NEVER_SET", "version"])

    assert rc == 0
    assert gateway_version() in out
    assert FakeAdminClient.instances == []  # never even built a client


def test_run_ignores_malformed_config(foreground, fake_admin, tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_CONFIG", str(_broken_config(tmp_path)))

    rc, out, err = run_cli(["run"])

    assert rc == 0
    assert foreground == ["foreground"]
    assert FakeAdminClient.instances == []


def test_update_ignores_malformed_config(
    patched_service, fake_admin, tmp_path, monkeypatch
):
    monkeypatch.setenv("MCP_GATEWAY_CONFIG", str(_broken_config(tmp_path)))

    rc, out, err = run_cli(["update"])

    assert rc == 0
    assert patched_service == [("update", (None,), {})]


def test_service_commands_ignore_missing_token(patched_service, fake_admin):
    rc, out, err = run_cli(["--token-env", "NEVER_SET", "service", "status"])
    assert rc == 0
    assert patched_service == [("status", (), {})]

    rc, out, err = run_cli(["service", "install", "--token-env", "NEVER_SET"])
    assert rc == 0
    assert patched_service == [
        ("status", (), {}),
        ("install", (), {"force_restart": False}),
    ]


# ---------------------------------------------------------------------------
# Legacy service aliases (hidden compatibility, old semantics preserved)
# ---------------------------------------------------------------------------


class _ServiceCallLog(list):
    """A call log that also carries per-function canned results."""

    def __init__(self, results: dict[str, object]) -> None:
        super().__init__()
        self.results = results


@pytest.fixture
def patched_service(monkeypatch):
    """Patch every service function the CLI may call and record invocations.

    The returned log compares like a plain list (``== [("install", ...)]``)
    and exposes ``.results`` so a test can swap a canned return value.
    """
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    calls = _ServiceCallLog(
        {
            "install": service.InstallResult(True, True, False, False),
            "uninstall": service.UninstallResult(True, (Path("/plist"),), False),
            "status": service.ResourceStatus(True, 4242, None, 0, None, None, None),
            "update": service.UpdateResult("1.3.1", "9.9.9", True, True),
        }
    )

    def recorder(name: str):
        def fn(*args, **kwargs):
            calls.append((name, args, kwargs))
            return calls.results[name]

        return fn

    for attr, name in (
        ("install_service", "install"),
        ("uninstall_service", "uninstall"),
        ("resource_status", "status"),
        ("update_application", "update"),
    ):
        monkeypatch.setattr(service, attr, recorder(name))
    return calls


def test_legacy_install_service_alias(patched_service):
    rc, out, err = run_cli(["--install-service"])
    assert rc == 0
    assert patched_service == [("install", (), {"force_restart": False})]
    assert "resident service installed and started" in out


def test_legacy_install_service_restart_flag(patched_service):
    rc, out, err = run_cli(["--install-service", "--restart"])
    assert rc == 0
    assert patched_service == [("install", (), {"force_restart": True})]


def test_legacy_uninstall_service_keep_data_is_explicit_consent(patched_service):
    # The legacy flag was itself explicit, non-interactive consent: it must
    # not require an additional --yes.
    rc, out, err = run_cli(["--uninstall-service", "--keep-data"])
    assert rc == 0
    assert patched_service == [("uninstall", (), {"purge_data": False})]
    assert "config and state kept" in out


def test_legacy_uninstall_service_purge_data(patched_service):
    rc, out, err = run_cli(["--uninstall-service", "--purge-data"])
    assert rc == 0
    assert patched_service == [("uninstall", (), {"purge_data": True})]
    assert "config and state deleted" in out


def test_legacy_uninstall_service_rejects_unknown_flags(patched_service):
    rc, out, err = run_cli(["--uninstall-service", "--bogus"])
    assert rc == 1
    assert patched_service == []
    assert "--uninstall-service" in err


def test_legacy_service_status_alias(patched_service):
    rc, out, err = run_cli(["--service-status"])
    assert rc == 0
    assert patched_service == [("status", (), {})]
    assert "service: loaded" in out
    assert "pid: 4242" in out


# ---------------------------------------------------------------------------
# service / update subcommands
# ---------------------------------------------------------------------------


def test_service_install_subcommand(patched_service):
    rc, out, err = run_cli(["service", "install"])
    assert rc == 0
    assert patched_service == [("install", (), {"force_restart": False})]
    assert "resident service installed and started" in out


def test_service_install_json(patched_service):
    rc, out, err = run_cli(["service", "install", "--json"])
    assert rc == 0
    data = load_json(out)
    assert data["changed"] is True
    assert data["reloaded"] is True


def test_service_status_subcommand(patched_service):
    rc, out, err = run_cli(["service", "status"])
    assert rc == 0
    assert patched_service == [("status", (), {})]
    assert "service: loaded" in out


def test_service_status_rejects_non_macos(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    rc, out, err = run_cli(["service", "status"])
    assert rc == 1
    assert out == ""
    assert "available only for the macOS resident service" in err


def test_service_uninstall_requires_yes(patched_service):
    rc, out, err = run_cli(["service", "uninstall"])
    assert rc == 1
    assert patched_service == []  # nothing was removed
    assert "requires --yes" in err
    assert "refusing to prompt" in err


def test_service_uninstall_with_yes_purges_data(patched_service):
    rc, out, err = run_cli(["service", "uninstall", "--yes", "--purge-data"])
    assert rc == 0
    assert patched_service == [("uninstall", (), {"purge_data": True})]


def test_service_uninstall_keep_data_default(patched_service):
    rc, out, err = run_cli(["service", "uninstall", "--yes", "--keep-data"])
    assert rc == 0
    assert patched_service == [("uninstall", (), {"purge_data": False})]


def test_update_passes_requested_version(patched_service):
    rc, out, err = run_cli(["update", "--version", "9.9.9"])
    assert rc == 0
    assert patched_service == [("update", ("9.9.9",), {})]
    assert "updated mcp-gateway 1.3.1 -> 9.9.9" in out


def test_update_already_installed(patched_service):
    patched_service.results["update"] = service.UpdateResult(
        "1.3.2", "1.3.2", False, False
    )
    rc, out, err = run_cli(["update"])
    assert rc == 0
    assert "already installed" in out


def test_service_error_surfaces_on_stderr(patched_service, monkeypatch):
    def boom(*a, **k):
        raise service.ServiceError("launchd refused")

    monkeypatch.setattr(service, "install_service", boom)
    rc, out, err = run_cli(["service", "install"])
    assert rc == 1
    assert "launchd refused" in err


# ---------------------------------------------------------------------------
# URL / config / token resolution — queries never seed config
# ---------------------------------------------------------------------------


def _assert_client() -> FakeAdminClient:
    assert len(FakeAdminClient.instances) == 1
    return FakeAdminClient.instances[0]


def test_query_without_config_uses_loopback_default_and_creates_nothing(
    fake_admin, tmp_path
):
    # The autouse fixture already cleared MCP_GATEWAY_CONFIG and redirected
    # HOME to tmp_path, so no real config can be read here.
    fake_admin.responses[("GET", "/admin/api/status")] = make_status()

    rc, out, err = run_cli(["status"])

    assert rc == 0
    assert _assert_client().base_url == "http://127.0.0.1:9100"
    assert _assert_client().token is None
    # A query command must never seed or create a config file.
    assert list(tmp_path.rglob("config.toml")) == []
    assert list(tmp_path.rglob("*")) == []


def test_config_derives_url_and_resolved_token(fake_admin, tmp_path, monkeypatch):
    cfg = write_config(
        tmp_path, 'host = "127.0.0.1"\nport = 9255\nbearer_token = "${ADMIN_BEARER}"\n'
    )
    monkeypatch.setenv("MCP_GATEWAY_CONFIG", str(cfg))
    monkeypatch.setenv("ADMIN_BEARER", "cfg-tok")
    fake_admin.responses[("GET", "/admin/api/status")] = make_status()

    rc, out, err = run_cli(["status"])

    assert rc == 0
    assert _assert_client().base_url == "http://127.0.0.1:9255"
    assert _assert_client().token == "cfg-tok"


def test_global_config_flag_overrides_environment(fake_admin, tmp_path, monkeypatch):
    env_cfg = write_config(tmp_path / "env", 'host = "127.0.0.1"\nport = 9255\n')
    flag_cfg = write_config(tmp_path / "flag", 'host = "127.0.0.1"\nport = 9266\n')
    monkeypatch.setenv("MCP_GATEWAY_CONFIG", str(env_cfg))
    fake_admin.responses[("GET", "/admin/api/status")] = make_status()

    rc, out, err = run_cli(["--config", str(flag_cfg), "status"])

    assert rc == 0
    assert _assert_client().base_url == "http://127.0.0.1:9266"


def test_global_url_flag_overrides_config(fake_admin, tmp_path, monkeypatch):
    cfg = write_config(tmp_path, 'host = "127.0.0.1"\nport = 9255\n')
    monkeypatch.setenv("MCP_GATEWAY_CONFIG", str(cfg))
    fake_admin.responses[("GET", "/admin/api/status")] = make_status()

    rc, out, err = run_cli(["status", "--url", "http://127.0.0.1:9999"])

    assert rc == 0
    assert _assert_client().base_url == "http://127.0.0.1:9999"


def test_broken_config_fails_cleanly(fake_admin, tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text("not = [valid toml", encoding="utf-8")
    monkeypatch.setenv("MCP_GATEWAY_CONFIG", str(cfg))
    fake_admin.responses[("GET", "/admin/api/status")] = make_status()

    rc, out, err = run_cli(["status"])

    assert rc == 1
    assert "could not read config" in err


# ---------------------------------------------------------------------------
# Token precedence and secret non-disclosure
# ---------------------------------------------------------------------------


def _status_ok(fake_admin):
    fake_admin.responses[("GET", "/admin/api/status")] = make_status()


def test_token_env_wins_over_generic_env(fake_admin, monkeypatch):
    monkeypatch.setenv("MY_NAMED_TOKEN", "named-tok")
    monkeypatch.setenv("MCP_GATEWAY_ADMIN_TOKEN", "generic-tok")
    _status_ok(fake_admin)

    rc, out, err = run_cli(["status", "--token-env", "MY_NAMED_TOKEN"])

    assert rc == 0
    assert _assert_client().token == "named-tok"


def test_missing_token_env_is_an_error_without_any_request(fake_admin, monkeypatch):
    monkeypatch.delenv("UNSET_VAR", raising=False)
    _status_ok(fake_admin)

    rc, out, err = run_cli(["status", "--token-env", "UNSET_VAR"])

    assert rc == 1
    assert "UNSET_VAR" in err
    assert FakeAdminClient.instances == []  # never even built a client
    assert FakeAdminClient.calls == []


def test_generic_admin_token_env(fake_admin, monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_ADMIN_TOKEN", "generic-tok")
    _status_ok(fake_admin)

    rc, out, err = run_cli(["status"])

    assert rc == 0
    assert _assert_client().token == "generic-tok"


def test_configured_token_resolved_from_env(fake_admin, tmp_path, monkeypatch):
    cfg = write_config(tmp_path, 'bearer_token = "${ADMIN_BEARER}"\n')
    monkeypatch.setenv("MCP_GATEWAY_CONFIG", str(cfg))
    monkeypatch.setenv("ADMIN_BEARER", "cfg-tok")
    _status_ok(fake_admin)

    rc, out, err = run_cli(["status"])

    assert rc == 0
    assert _assert_client().token == "cfg-tok"


def test_oauth_admin_bearer_token_fallback(fake_admin, tmp_path, monkeypatch):
    cfg = write_config(
        tmp_path,
        '[oauth]\npublic_base_url = "http://127.0.0.1:9100"\n'
        'authorization_servers = ["http://127.0.0.1:9100"]\n'
        'issuer = "http://127.0.0.1:9100"\n'
        'jwks_uri = "http://127.0.0.1:9100/keys"\n'
        'admin_bearer_token = "${OAUTH_ADMIN}"\n',
    )
    monkeypatch.setenv("MCP_GATEWAY_CONFIG", str(cfg))
    monkeypatch.setenv("OAUTH_ADMIN", "oauth-tok")
    _status_ok(fake_admin)

    rc, out, err = run_cli(["status"])

    assert rc == 0
    assert _assert_client().token == "oauth-tok"


def test_env_token_beats_configured_token(fake_admin, tmp_path, monkeypatch):
    cfg = write_config(tmp_path, 'bearer_token = "${ADMIN_BEARER}"\n')
    monkeypatch.setenv("MCP_GATEWAY_CONFIG", str(cfg))
    monkeypatch.setenv("ADMIN_BEARER", "cfg-tok")
    monkeypatch.setenv("MCP_GATEWAY_ADMIN_TOKEN", "env-tok")
    _status_ok(fake_admin)

    rc, out, err = run_cli(["status"])

    assert rc == 0
    assert _assert_client().token == "env-tok"


def test_missing_configured_token_variable_fails_loudly(
    fake_admin, tmp_path, monkeypatch
):
    cfg = write_config(tmp_path, 'bearer_token = "${GONE}"\n')
    monkeypatch.setenv("MCP_GATEWAY_CONFIG", str(cfg))
    _status_ok(fake_admin)

    rc, out, err = run_cli(["status"])

    assert rc == 1
    assert "GONE" in err


def test_secrets_never_appear_in_output(fake_admin, monkeypatch):
    secret = "sekrit-token-7f3a9"
    monkeypatch.setenv("MCP_GATEWAY_ADMIN_TOKEN", secret)
    fake_admin.responses[("GET", "/admin/api/status")] = make_status()
    fake_admin.responses[("GET", "/admin/api/state")] = CLIError(
        "gateway 500 Internal Server Error", response={"error": "boom"}
    )

    ok_rc, ok_out, ok_err = run_cli(["status", "--json"])
    assert ok_rc == 0
    assert secret not in ok_out and secret not in ok_err

    fail_rc, fail_out, fail_err = run_cli(["backend", "list"])
    assert fail_rc == 1
    assert secret not in fail_out and secret not in fail_err


def test_backend_show_never_echoes_auth_value(fake_admin):
    backend = make_backend(auth_header="Authorization", auth_value="${DB_SECRET}")
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(backend)
    fake_admin.responses[("GET", "/admin/api/status")] = make_status()

    rc, out, err = run_cli(["backend", "show", "db", "--json"])

    assert rc == 0
    data = load_json(out)
    assert "auth_value" not in data
    assert data["auth_value_set"] is True
    assert "${DB_SECRET}" not in out
    assert "Authorization" in data["auth_header"]


# ---------------------------------------------------------------------------
# HTTP / network error exit behavior
# ---------------------------------------------------------------------------


def test_http_error_exits_nonzero_with_detail_on_stderr(fake_admin):
    fake_admin.responses[("GET", "/admin/api/status")] = CLIError(
        "gateway 503 Service Unavailable for "
        "http://127.0.0.1:9100/admin/api/status: down",
        response={"error": "down"},
    )

    rc, out, err = run_cli(["status"])

    assert rc == 1
    assert out == ""
    assert "503" in err
    assert "down" in err


def test_json_query_error_prints_no_partial_json(fake_admin):
    fake_admin.responses[("GET", "/admin/api/status")] = CLIError(
        "gateway 500 Internal Server Error for http://127.0.0.1:9100/admin/api/status"
    )

    rc, out, err = run_cli(["status", "--json"])

    assert rc == 1
    assert out == ""  # never a partial/mixed JSON document


def test_network_error_exits_nonzero(fake_admin):
    fake_admin.responses[("GET", "/admin/api/status")] = CLIError(
        "could not reach the gateway at http://127.0.0.1:9100: "
        "[Errno 61] Connection refused"
    )

    rc, out, err = run_cli(["status"])

    assert rc == 1
    assert out == ""
    assert "could not reach the gateway" in err
    assert "Connection refused" in err


def test_unknown_backend_show_is_a_clean_error(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state()
    fake_admin.responses[("GET", "/admin/api/status")] = make_status()

    rc, out, err = run_cli(["backend", "show", "nope"])

    assert rc == 1
    assert "unknown backend" in err
    assert "'nope'" in err


# ---------------------------------------------------------------------------
# Security contracts: explicit --url token scoping, scheme policy, redirects,
# control-char escaping
# ---------------------------------------------------------------------------


def test_explicit_url_never_uses_ambient_env_token(fake_admin, monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_ADMIN_TOKEN", "ambient-tok")
    fake_admin.responses[("GET", "/admin/api/status")] = make_status()

    rc, out, err = run_cli(["status", "--url", "http://127.0.0.1:9999"])

    assert rc == 0
    assert _assert_client().base_url == "http://127.0.0.1:9999"
    assert _assert_client().token is None  # ambient token never forwarded


def test_explicit_url_ignores_configured_token(fake_admin, tmp_path, monkeypatch):
    cfg = write_config(tmp_path, 'bearer_token = "${ADMIN_BEARER}"\n')
    monkeypatch.setenv("MCP_GATEWAY_CONFIG", str(cfg))
    monkeypatch.setenv("ADMIN_BEARER", "cfg-tok")
    fake_admin.responses[("GET", "/admin/api/status")] = make_status()

    rc, out, err = run_cli(["status", "--url", "http://127.0.0.1:9999"])

    assert rc == 0
    assert _assert_client().token is None


def test_explicit_url_uses_only_token_env(fake_admin, monkeypatch):
    monkeypatch.setenv("MY_NAMED_TOKEN", "named-tok")
    monkeypatch.setenv("MCP_GATEWAY_ADMIN_TOKEN", "ambient-tok")
    fake_admin.responses[("GET", "/admin/api/status")] = make_status()

    rc, out, err = run_cli(
        ["status", "--url", "http://127.0.0.1:9999", "--token-env", "MY_NAMED_TOKEN"]
    )

    assert rc == 0
    assert _assert_client().token == "named-tok"


def test_explicit_url_with_missing_token_env_still_errors(fake_admin):
    rc, out, err = run_cli(
        ["status", "--url", "http://127.0.0.1:9999", "--token-env", "NEVER_SET"]
    )

    assert rc == 1
    assert "NEVER_SET" in err
    assert FakeAdminClient.instances == []


def test_admin_client_rejects_unsupported_scheme():
    with pytest.raises(CLIError, match="unsupported gateway URL scheme"):
        _RealAdminClient("ftp://example.com", None)


def test_admin_client_rejects_userinfo():
    with pytest.raises(CLIError, match="userinfo"):
        _RealAdminClient("http://user" + ":pass@127.0.0.1:9100", None)


def test_admin_client_rejects_path_query_and_fragment():
    with pytest.raises(CLIError, match="without a path"):
        _RealAdminClient("http://127.0.0.1:9100/admin", None)
    with pytest.raises(CLIError, match="query or fragment"):
        _RealAdminClient("http://127.0.0.1:9100/?x=1", None)


def test_admin_client_refuses_token_over_remote_http():
    with pytest.raises(CLIError, match="refusing to send a bearer token over http"):
        _RealAdminClient("http://example.com:9100", "tok")


def test_admin_client_allows_token_over_loopback_http_and_https():
    for url in (
        "http://127.0.0.1:9100",
        "http://localhost:9100",
        "https://example.com",
    ):
        client = _RealAdminClient(url, "tok")
        assert client.token == "tok"
        assert client.base_url.rstrip("/") == url


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    """Answers every request with a 302 and records the hop's headers."""

    hits: list[dict[str, str]] = []

    def do_GET(self):  # noqa: N802
        _RedirectHandler.hits.append({k.lower(): v for k, v in self.headers.items()})
        self.send_response(302)
        self.send_header("Location", "/target")
        self.end_headers()

    def log_message(self, *args):  # silence test noise
        pass


@pytest.fixture
def redirect_server():
    _RedirectHandler.hits = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_admin_client_refuses_redirects_and_never_follows(redirect_server):
    client = _RealAdminClient(
        f"http://127.0.0.1:{redirect_server.server_port}", "sekrit"
    )

    with pytest.raises(CLIError) as exc:
        client.request("GET", "/admin/api/state")

    assert "redirect" in str(exc.value)
    assert "302" in str(exc.value)
    # Exactly one hop: the redirect was refused, so the Location target never
    # received a request and Authorization was never re-sent.
    assert len(_RedirectHandler.hits) == 1
    assert _RedirectHandler.hits[0].get("authorization") == "Bearer sekrit"


def test_human_output_escapes_control_chars_json_preserves(fake_admin):
    backend = make_backend("db")
    backend["tools"][0]["original"] = "evil\x1b[31mname"
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(backend)

    rc, out, err = run_cli(["tool", "list"])
    assert rc == 0
    assert "\x1b" not in out  # no raw escape sequence in human output
    assert "\\x1b" in out  # visible \x1b escape

    rc, out, err = run_cli(["tool", "list", "--json"])
    assert rc == 0
    data = load_json(out)
    assert data[0]["original"] == "evil\x1b[31mname"  # exact string preserved


def test_logs_human_escapes_control_chars(fake_admin):
    fake_admin.responses[("GET", "/admin/api/logs")] = {
        "entries": [{"timestamp": "t", "level": "INFO", "event": "evil\x1b[2J"}],
        "stats": {},
        "filters": {"limit": 100, "level": None, "event": None},
    }

    rc, out, err = run_cli(["logs", "show"])

    assert rc == 0
    assert "\x1b" not in out
    assert "\\x1b" in out


def test_stderr_errors_escape_control_chars(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state()

    rc, out, err = run_cli(["backend", "show", "bad\x1bname"])

    assert rc == 1
    assert "\x1b" not in err
    assert "\\x1b" in err


def test_backend_add_server_error_detail_surfaces(fake_admin):
    fake_admin.responses[("POST", "/admin/api/backend")] = CLIError(
        "gateway 400 Bad Request for "
        "http://127.0.0.1:9100/admin/api/backend: backend name already exists",
        response={"error": "backend name already exists"},
    )

    rc, out, err = run_cli(
        ["backend", "add", "git", "--transport", "stdio", "--command", "x"]
    )

    assert rc == 1
    assert "backend name already exists" in err
    assert out == ""


# ---------------------------------------------------------------------------
# Clean --json: exactly one JSON value, anywhere in argv
# ---------------------------------------------------------------------------


def test_json_emits_one_value_even_with_human_lines(fake_admin):
    fake_admin.responses[("GET", "/admin/api/status")] = make_status(
        ("db", {"state": "ok", "tools": 1, "ms": 5})
    )

    rc, out, err = run_cli(["--json", "status"])

    assert rc == 0
    data = load_json(out)  # the whole stdout must parse
    assert data["backends"]["db"]["state"] == "ok"
    assert "db: ok" not in out  # human rendering never mixes with JSON


def test_json_flag_accepted_after_subcommand(fake_admin):
    fake_admin.responses[("GET", "/admin/api/status")] = make_status()

    rc, out, err = run_cli(["status", "--json"])

    assert rc == 0
    load_json(out)


def test_timeout_global_is_accepted_anywhere_and_validated(fake_admin):
    fake_admin.responses[("GET", "/admin/api/status")] = make_status()

    rc, out, err = run_cli(["status", "--timeout", "5"])

    assert rc == 0
    assert _assert_client().timeout == 5.0


def test_invalid_timeout_is_a_clean_error(fake_admin):
    fake_admin.responses[("GET", "/admin/api/status")] = make_status()

    rc, out, err = run_cli(["status", "--timeout", "soon"])

    assert rc == 1
    assert "timeout" in err.lower()
    assert FakeAdminClient.calls == []


# ---------------------------------------------------------------------------
# status / check / restart
# ---------------------------------------------------------------------------


def test_status_human_and_json(fake_admin):
    fake_admin.responses[("GET", "/admin/api/status")] = make_status(
        ("db", {"state": "ok", "tools": 1, "ms": 5}),
        ("slow", {"state": "error", "error": "timeout"}),
        ("off", {"state": "disabled"}),
    )

    rc, out, err = run_cli(["status"])

    assert rc == 0
    assert "db: ok (1 tools, 5 ms)" in out
    assert "slow: error (timeout)" in out
    assert "off: disabled" in out

    rc, out, err = run_cli(["status", "--json"])
    assert rc == 0
    data = load_json(out)
    assert data["backends"]["db"]["state"] == "ok"


def test_status_with_no_backends(fake_admin):
    fake_admin.responses[("GET", "/admin/api/status")] = {"backends": {}}

    rc, out, err = run_cli(["status"])

    assert rc == 0
    assert "no backends configured" in out


def test_check_hits_ready_and_exits_zero_when_ready(fake_admin):
    fake_admin.responses[("GET", "/ready")] = {
        "ready": True,
        "mounted": ["db"],
        "enabled": ["db"],
        "missing": [],
        "virtual": {"mounted": True, "endpoint": "/virtual/mcp", "error": None},
    }

    rc, out, err = run_cli(["check"])

    assert rc == 0
    assert "ready" in out
    expect_calls(call("GET", "/ready"))


def test_check_reports_not_ready_and_exits_nonzero(fake_admin):
    fake_admin.responses[("GET", "/ready")] = CLIError(
        "gateway 503 Service Unavailable for http://127.0.0.1:9100/ready",
        response={
            "ready": False,
            "mounted": [],
            "enabled": ["db"],
            "missing": ["db"],
            "virtual": {"mounted": True, "endpoint": "/virtual/mcp", "error": None},
        },
    )

    rc, out, err = run_cli(["check"])

    assert rc == 1
    assert "not ready" in out
    assert "missing: db" in out
    assert "not ready" in err


def test_restart_managed(fake_admin):
    fake_admin.responses[("POST", "/admin/api/restart")] = {
        "ok": True,
        "reloaded": "restarting",
    }

    rc, out, err = run_cli(["restart"])

    assert rc == 0
    assert "restart scheduled" in out
    expect_calls(call("POST", "/admin/api/restart"))


def test_restart_dev_no_restart(fake_admin):
    fake_admin.responses[("POST", "/admin/api/restart")] = {
        "ok": True,
        "reloaded": "dev-no-restart",
    }

    rc, out, err = run_cli(["restart"])

    assert rc == 0
    assert "dev/foreground-managed" in out


# ---------------------------------------------------------------------------
# logs show / follow
# ---------------------------------------------------------------------------


def test_logs_show_human_and_params(fake_admin):
    fake_admin.responses[("GET", "/admin/api/logs")] = {
        "entries": [
            {
                "timestamp": "2026-08-04T10:00:00Z",
                "level": "INFO",
                "event": "gateway_started",
                "backend": "db",
            }
        ],
        "stats": {"path": "/tmp/gateway.log"},
        "filters": {"limit": 100, "level": None, "event": None},
    }

    rc, out, err = run_cli(["logs", "show"])

    assert rc == 0
    assert "gateway_started" in out
    assert "INFO" in out
    assert "backend=db" in out
    expect_calls(call("GET", "/admin/api/logs", params={"limit": 100}))


def test_logs_show_filters_and_json(fake_admin):
    fake_admin.responses[("GET", "/admin/api/logs")] = {
        "entries": [{"timestamp": "t", "level": "ERROR", "event": "boom"}],
        "stats": {},
        "filters": {"limit": 5, "level": "ERROR", "event": "boom"},
    }

    rc, out, err = run_cli(
        [
            "logs",
            "show",
            "--limit",
            "5",
            "--level",
            "error",
            "--event",
            "boom",
            "--json",
        ]
    )

    assert rc == 0
    data = load_json(out)
    assert data["entries"][0]["event"] == "boom"
    expect_calls(
        call(
            "GET",
            "/admin/api/logs",
            params={"limit": 5, "level": "ERROR", "event": "boom"},
        )
    )


def test_logs_show_rejects_out_of_range_limit(fake_admin):
    rc, out, err = run_cli(["logs", "show", "--limit", "9999"])
    assert rc == 2
    assert "between 1 and 500" in err
    assert FakeAdminClient.calls == []


def test_logs_show_empty_tail(fake_admin):
    fake_admin.responses[("GET", "/admin/api/logs")] = {
        "entries": [],
        "stats": {},
        "filters": {"limit": 100, "level": None, "event": None},
    }

    rc, out, err = run_cli(["logs", "show"])

    assert rc == 0
    assert "(no log entries)" in out


def test_logs_follow_streams_sse_frames(fake_admin):
    fake_admin.stream_lines[("/admin/api/logs/stream", _freeze({}))] = [
        'data: {"timestamp": "t", "level": "INFO", "event": "x", "backend": "db"}',
        ": keepalive",
        'data: {"timestamp": "u", "level": "WARN", "event": "y"}',
    ]

    rc, out, err = run_cli(["logs", "follow"])

    assert rc == 0
    assert "INFO x backend=db" in out
    assert "WARN y" in out
    assert "keepalive" not in out
    expect_calls(call("STREAM", "/admin/api/logs/stream", params={}))


def test_logs_follow_json_passes_raw_events_through(fake_admin):
    events = [
        '{"timestamp": "t", "level": "INFO", "event": "x"}',
        '{"timestamp": "u", "level": "WARN", "event": "y"}',
    ]
    fake_admin.stream_lines[("/admin/api/logs/stream", _freeze({}))] = [
        f"data: {event}" for event in events
    ] + [": keepalive"]

    rc, out, err = run_cli(["logs", "follow", "--json"])

    assert rc == 0
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 2  # keepalive never surfaces
    for line, event in zip(lines, events, strict=True):
        assert load_json(line) == json.loads(event)


def test_logs_follow_passes_level_filter(fake_admin):
    fake_admin.stream_lines[
        ("/admin/api/logs/stream", _freeze({"level": "ERROR"}))
    ] = []
    rc, out, err = run_cli(["logs", "follow", "--level", "error"])
    assert rc == 0
    expect_calls(call("STREAM", "/admin/api/logs/stream", params={"level": "ERROR"}))


def test_logs_follow_rejects_event_filter(fake_admin):
    # Only `logs show` filters by event; a follow with --event must fail at
    # parse time instead of silently dropping the filter.
    rc, out, err = run_cli(["logs", "follow", "--event", "boom"])

    assert rc == 2
    assert "--event" in err
    assert FakeAdminClient.calls == []


# ---------------------------------------------------------------------------
# backend domain
# ---------------------------------------------------------------------------


def test_backend_list_merges_state_and_status(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(
        make_backend("db"), make_backend("git", transport="http", url="http://x/git")
    )
    fake_admin.responses[("GET", "/admin/api/status")] = make_status(
        ("db", {"state": "ok", "tools": 1, "ms": 5}),
        ("git", {"state": "error", "error": "refused"}),
    )

    rc, out, err = run_cli(["backend", "list"])

    assert rc == 0
    assert "db" in out
    assert "git" in out
    expect_calls(
        call("GET", "/admin/api/state"),
        call("GET", "/admin/api/status"),
    )


def test_backend_list_json_payload(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    fake_admin.responses[("GET", "/admin/api/status")] = make_status(
        ("db", {"state": "ok", "tools": 1, "ms": 5})
    )

    rc, out, err = run_cli(["backend", "list", "--json"])

    assert rc == 0
    data = load_json(out)
    assert isinstance(data, list)
    assert data[0]["name"] == "db"
    assert data[0]["status"]["state"] == "ok"


def test_backend_list_survives_status_failure(fake_admin):
    # The dashboard keeps reading when one probe fails; the CLI must too.
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    fake_admin.responses[("GET", "/admin/api/status")] = CLIError("gateway 500")

    rc, out, err = run_cli(["backend", "list"])

    assert rc == 0
    assert "db" in out
    assert "n/a" in out


def test_backend_show_json_redacts_auth_and_merges_status(fake_admin):
    backend = make_backend("db", auth_header="Authorization", auth_value="${DB_SECRET}")
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(backend)
    fake_admin.responses[("GET", "/admin/api/status")] = make_status(
        ("db", {"state": "ok", "tools": 1, "ms": 5})
    )

    rc, out, err = run_cli(["backend", "show", "db", "--json"])

    assert rc == 0
    data = load_json(out)
    assert data["name"] == "db"
    assert data["auth_value_set"] is True
    assert "auth_value" not in data
    assert data["status"]["state"] == "ok"


def test_backend_add_passes_env_and_required_fields(fake_admin):
    fake_admin.responses[("POST", "/admin/api/backend")] = {
        "ok": True,
        "reloaded": "hot-add",
        "backend": "git",
    }

    rc, out, err = run_cli(
        [
            "backend",
            "add",
            "git",
            "--transport",
            "stdio",
            "--command",
            "/usr/bin/git",
            "--arg",
            "status",
            "--env",
            "TOKEN=${GIT_TOKEN}",
            "--env-literal",
            "LANG=C",
        ]
    )

    assert rc == 0
    assert "added backend 'git'" in out
    expect_calls(
        call(
            "POST",
            "/admin/api/backend",
            payload={
                "name": "git",
                "transport": "stdio",
                "command": "/usr/bin/git",
                "args": ["status"],
                "env": {"TOKEN": "${GIT_TOKEN}", "LANG": "C"},
            },
        )
    )


def test_backend_add_requires_transport(fake_admin):
    rc, out, err = run_cli(["backend", "add", "git"])
    assert rc == 1
    assert "--transport is required" in err
    assert FakeAdminClient.calls == []


def test_backend_add_rejects_reserved_name(fake_admin):
    rc, out, err = run_cli(
        ["backend", "add", "admin", "--transport", "stdio", "--command", "x"]
    )
    assert rc == 1
    assert "reserved" in err
    assert FakeAdminClient.calls == []


def test_backend_add_passes_auth_and_headers_env_refs(fake_admin):
    # auth_value / credential-header values must be ${ENV_VAR} references
    # (resolved by the daemon, never here); non-credential headers pass through.
    fake_admin.responses[("POST", "/admin/api/backend")] = {
        "ok": True,
        "reloaded": "hot-add",
        "backend": "git",
    }

    rc, out, err = run_cli(
        [
            "backend",
            "add",
            "git",
            "--transport",
            "stdio",
            "--command",
            "x",
            "--auth-header",
            "Authorization",
            "--auth-value",
            "${GIT_TOKEN}",
            "--header",
            "X-API-Key:${API_KEY}",
            "--header-literal",
            "X-Tenant: acme",
        ]
    )

    assert rc == 0
    payload = FakeAdminClient.calls[0]["payload"]
    assert payload["auth_value"] == "${GIT_TOKEN}"
    assert payload["headers"] == {"X-API-Key": "${API_KEY}", "X-Tenant": "acme"}


def test_backend_add_rejects_raw_auth_value_secret(fake_admin):
    rc, out, err = run_cli(
        [
            "backend",
            "add",
            "git",
            "--transport",
            "stdio",
            "--command",
            "x",
            "--auth-value",
            "hunter2",
        ]
    )
    assert rc == 1
    assert "never mixed with raw text" in err
    assert "hunter2" not in err
    assert FakeAdminClient.calls == []


def test_backend_add_rejects_mixed_raw_and_ref(fake_admin):
    # A raw secret smuggled next to an unrelated ${REF} must be rejected, not
    # pass a contains-check.
    rc, out, err = run_cli(
        [
            "backend",
            "add",
            "git",
            "--transport",
            "stdio",
            "--command",
            "x",
            "--auth-value",
            "Bearer raw-secret ${HOME}",
        ]
    )
    assert rc == 1
    assert "never mixed with raw text" in err
    assert "raw-secret" not in err
    assert FakeAdminClient.calls == []

    rc, out, err = run_cli(
        [
            "backend",
            "add",
            "git",
            "--transport",
            "stdio",
            "--command",
            "x",
            "--env",
            "TOKEN=raw ${HOME}",
        ]
    )
    assert rc == 1
    assert "never mixed with raw text" in err
    assert FakeAdminClient.calls == []


def test_backend_add_accepts_prefixed_env_ref_auth_value(fake_admin):
    fake_admin.responses[("POST", "/admin/api/backend")] = {
        "ok": True,
        "reloaded": "hot-add",
        "backend": "git",
    }

    rc, out, err = run_cli(
        [
            "backend",
            "add",
            "git",
            "--transport",
            "http",
            "--backend-url",
            "http://example.com/git",
            "--auth-value",
            "Bearer ${GIT_TOKEN}",
        ]
    )

    assert rc == 0
    payload = FakeAdminClient.calls[0]["payload"]
    assert payload["auth_value"] == "Bearer ${GIT_TOKEN}"


def test_backend_add_rejects_raw_secret_in_credential_header(fake_admin):
    rc, out, err = run_cli(
        [
            "backend",
            "add",
            "git",
            "--transport",
            "stdio",
            "--command",
            "x",
            "--header",
            "Authorization:hunter2",
        ]
    )
    assert rc == 1
    assert "--header Authorization must be exactly a ${VAR}" in err
    assert "hunter2" not in err
    assert FakeAdminClient.calls == []


def test_backend_add_accepts_bearer_basic_token_templates(fake_admin):
    fake_admin.responses[("POST", "/admin/api/backend")] = {
        "ok": True,
        "reloaded": "hot-add",
        "backend": "git",
    }

    rc, out, err = run_cli(
        [
            "backend",
            "add",
            "git",
            "--transport",
            "http",
            "--backend-url",
            "http://example.com/git",
            "--auth-value",
            "Basic ${BASIC}",
            "--header",
            "Authorization:Token ${TOK}",
            "--env",
            "GITHUB_TOKEN=${GITHUB_TOKEN}",
        ]
    )

    assert rc == 0
    payload = FakeAdminClient.calls[0]["payload"]
    assert payload["auth_value"] == "Basic ${BASIC}"
    assert payload["headers"] == {"Authorization": "Token ${TOK}"}
    assert payload["env"] == {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}


def test_backend_add_rejects_raw_env_value(fake_admin):
    rc, out, err = run_cli(
        [
            "backend",
            "add",
            "git",
            "--transport",
            "stdio",
            "--command",
            "x",
            "--env",
            "KEY=hunter2",
        ]
    )
    assert rc == 1
    assert "--env KEY must be exactly a ${VAR}" in err
    assert "hunter2" not in err  # the value is never echoed
    assert FakeAdminClient.calls == []


def test_backend_add_rejects_raw_header_value(fake_admin):
    rc, out, err = run_cli(
        [
            "backend",
            "add",
            "git",
            "--transport",
            "stdio",
            "--command",
            "x",
            "--header",
            "X-Tenant:acme",
        ]
    )
    assert rc == 1
    assert "--header X-Tenant must be exactly a ${VAR}" in err
    assert FakeAdminClient.calls == []


def test_backend_add_literal_flags_reject_credential_like_names(fake_admin):
    cases = [
        ["--header-literal", "Authorization:tok"],
        ["--header-literal", "Cookie:abc"],
        ["--header-literal", "X-Access-Key:abc"],
        ["--env-literal", "GITHUB_TOKEN=abc"],
        ["--env-literal", "DATABASE_URL=raw-database-value"],
        ["--env-literal", "DB_PASSWORD=abc"],
    ]
    for extra in cases:
        FakeAdminClient.calls.clear()
        rc, out, err = run_cli(
            [
                "backend",
                "add",
                "git",
                "--transport",
                "stdio",
                "--command",
                "x",
                *extra,
            ]
        )
        assert rc == 1, extra
        assert "cannot carry a credential-like name" in err, extra
        assert FakeAdminClient.calls == [], extra


def test_backend_add_literal_flags_accept_ordinary_names(fake_admin):
    fake_admin.responses[("POST", "/admin/api/backend")] = {
        "ok": True,
        "reloaded": "hot-add",
        "backend": "git",
    }

    rc, out, err = run_cli(
        [
            "backend",
            "add",
            "git",
            "--transport",
            "stdio",
            "--command",
            "x",
            "--env-literal",
            "LANG=C",
            "--env-literal",
            "PORT=8080",
            "--header-literal",
            "X-Trace:abc123",
            "--header-literal",
            "X-Client-Id:42",
        ]
    )

    assert rc == 0
    payload = FakeAdminClient.calls[0]["payload"]
    assert payload["env"] == {"LANG": "C", "PORT": "8080"}
    assert payload["headers"] == {"X-Trace": "abc123", "X-Client-Id": "42"}


def test_backend_add_env_literal_accepts_credential_store_location_keys(fake_admin):
    # Env keys naming a credential STORE location (suffixes -file/-path/-dir/
    # -directory) are ordinary metadata, not credentials themselves.
    fake_admin.responses[("POST", "/admin/api/backend")] = {
        "ok": True,
        "reloaded": "hot-add",
        "backend": "git",
    }

    rc, out, err = run_cli(
        [
            "backend",
            "add",
            "git",
            "--transport",
            "stdio",
            "--command",
            "x",
            "--env-literal",
            "PASSWORD_STORE_DIR=/tmp/store",
            "--env-literal",
            "PASSWORD_FILE=/run/secret",
            "--env-literal",
            "TOKEN_CACHE_DIR=/tmp/cache",
        ]
    )

    assert rc == 0
    payload = FakeAdminClient.calls[0]["payload"]
    assert payload["env"] == {
        "PASSWORD_STORE_DIR": "/tmp/store",
        "PASSWORD_FILE": "/run/secret",
        "TOKEN_CACHE_DIR": "/tmp/cache",
    }


def test_backend_add_env_literal_still_rejects_composite_credential_names(fake_admin):
    # The suffix exemption does not rescue a full concept match like
    # DATABASE_URL (normalized database-url is itself a credential concept).
    rc, out, err = run_cli(
        [
            "backend",
            "add",
            "git",
            "--transport",
            "stdio",
            "--command",
            "x",
            "--env-literal",
            "DATABASE_URL=raw-database-value",
        ]
    )
    assert rc == 1
    assert "cannot carry a credential-like name" in err
    assert FakeAdminClient.calls == []


def test_backend_add_header_literal_stays_strict_for_suffix_names(fake_admin):
    # The -file/-path/-dir exemption is ENV-ONLY: a credential-like header
    # name stays strict even with a path-like suffix.
    rc, out, err = run_cli(
        [
            "backend",
            "add",
            "git",
            "--transport",
            "stdio",
            "--command",
            "x",
            "--header-literal",
            "Password-File:abc",
        ]
    )
    assert rc == 1
    assert "cannot carry a credential-like name" in err
    assert FakeAdminClient.calls == []


def test_backend_add_env_literal_rejects_unsuffixed_credential_names(fake_admin):
    # The suffix exemption requires the EXACT trailing -file/-path/-dir/
    # -directory; a plain credential concept or a -bak variant stays strict,
    # and the key is echoed as-given.
    for extra in (
        ["--env-literal", "PASSWORD=x"],
        ["--env-literal", "PASSWORD_FILE_BAK=x"],
    ):
        FakeAdminClient.calls.clear()
        rc, out, err = run_cli(
            [
                "backend",
                "add",
                "git",
                "--transport",
                "stdio",
                "--command",
                "x",
                *extra,
            ]
        )
        assert rc == 1, extra
        assert "cannot carry a credential-like name" in err, extra
        assert extra[1].split("=")[0] in err, extra  # key echoed, case preserved
        assert FakeAdminClient.calls == [], extra


def test_backend_add_file_env_uses_env_classifier(fake_admin, tmp_path):
    source = tmp_path / "backend.json"

    # Exempt suffix: a literal store-path value is nonsecret metadata.
    source.write_text(
        json.dumps(
            {
                "name": "git",
                "transport": "stdio",
                "command": "x",
                "env": {"PASSWORD_FILE": "/x"},
            }
        ),
        encoding="utf-8",
    )
    fake_admin.responses[("POST", "/admin/api/backend")] = {
        "ok": True,
        "reloaded": "hot-add",
        "backend": "git",
    }
    rc, out, err = run_cli(["backend", "add", "--file", str(source)])
    assert rc == 0
    assert FakeAdminClient.calls[0]["payload"]["env"] == {"PASSWORD_FILE": "/x"}

    # Unsuffixed credential concept in the file: rejected before the request.
    FakeAdminClient.calls.clear()
    source.write_text(
        json.dumps(
            {
                "name": "git",
                "transport": "stdio",
                "command": "x",
                "env": {"PASSWORD": "hunter2"},
            }
        ),
        encoding="utf-8",
    )
    rc, out, err = run_cli(["backend", "add", "--file", str(source)])
    assert rc == 1
    assert "backend env 'PASSWORD' must be exactly a ${VAR}" in err
    assert "hunter2" not in err
    assert FakeAdminClient.calls == []


def test_backend_add_header_literal_credential_path_names_rejected(fake_admin):
    # Headers use the strict generic classifier: no suffix exemption.
    rc, out, err = run_cli(
        [
            "backend",
            "add",
            "git",
            "--transport",
            "stdio",
            "--command",
            "x",
            "--header-literal",
            "X-Password-File:/x",
        ]
    )
    assert rc == 1
    assert "cannot carry a credential-like name" in err
    assert "X-Password-File" in err  # key echoed as-given
    assert FakeAdminClient.calls == []


def test_backend_add_rejects_composite_multi_ref_values(fake_admin):
    # Only a single ${VAR} (or scheme + single ${VAR}) is safe; any composite
    # of multiple references or raw text is rejected everywhere.
    for extra in (
        ["--env", "TOKEN=${A} ${B}"],
        ["--auth-value", "${U}:${P}@${H}/db"],
    ):
        FakeAdminClient.calls.clear()
        rc, out, err = run_cli(
            [
                "backend",
                "add",
                "git",
                "--transport",
                "stdio",
                "--command",
                "x",
                *extra,
            ]
        )
        assert rc == 1, extra
        assert "must be exactly a ${VAR}" in err, extra
        assert FakeAdminClient.calls == [], extra


def test_backend_add_env_duplicate_last_wins(fake_admin):
    fake_admin.responses[("POST", "/admin/api/backend")] = {
        "ok": True,
        "reloaded": "hot-add",
        "backend": "git",
    }

    rc, out, err = run_cli(
        [
            "backend",
            "add",
            "git",
            "--transport",
            "stdio",
            "--command",
            "x",
            "--env",
            "K=${SECRET}",
            "--env-literal",
            "K=1",
        ]
    )
    assert rc == 0
    assert FakeAdminClient.calls[0]["payload"]["env"] == {"K": "1"}

    FakeAdminClient.calls.clear()
    rc, out, err = run_cli(
        [
            "backend",
            "add",
            "git",
            "--transport",
            "stdio",
            "--command",
            "x",
            "--env-literal",
            "K=1",
            "--env",
            "K=${SECRET}",
        ]
    )
    assert rc == 0
    assert FakeAdminClient.calls[0]["payload"]["env"] == {"K": "${SECRET}"}


def test_backend_add_file_env_headers_pass_through_unvalidated(fake_admin, tmp_path):
    # --file JSON is verbatim fidelity: env/headers in the file are sent as-is
    # (name-based ref rules are enforced by the server model validation).
    payload = {
        "name": "git",
        "transport": "stdio",
        "command": "x",
        "env": {"K": "raw-value"},
        "headers": {"X-Tenant": "acme"},
    }
    source = tmp_path / "backend.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    fake_admin.responses[("POST", "/admin/api/backend")] = {
        "ok": True,
        "reloaded": "hot-add",
        "backend": "git",
    }

    rc, out, err = run_cli(["backend", "add", "--file", str(source)])

    assert rc == 0
    assert FakeAdminClient.calls[0]["payload"] == payload


def test_backend_add_file_rejects_unknown_key(fake_admin, tmp_path):
    source = tmp_path / "backend.json"
    source.write_text(
        json.dumps({"name": "git", "transport": "stdio", "command": "x", "bogus": 1}),
        encoding="utf-8",
    )

    rc, out, err = run_cli(["backend", "add", "--file", str(source)])

    assert rc == 1
    assert "backend add payload contains unknown field: bogus" in err
    assert FakeAdminClient.calls == []


def test_backend_add_file_rejects_raw_auth_value(fake_admin, tmp_path):
    source = tmp_path / "backend.json"
    source.write_text(
        json.dumps(
            {
                "name": "git",
                "transport": "stdio",
                "command": "x",
                "auth_value": "raw-secret",
            }
        ),
        encoding="utf-8",
    )

    rc, out, err = run_cli(["backend", "add", "--file", str(source)])

    assert rc == 1
    assert "backend auth_value must be exactly a ${VAR}" in err
    assert "raw-secret" not in err
    assert FakeAdminClient.calls == []


def test_backend_add_file_rejects_raw_credential_header_and_env(fake_admin, tmp_path):
    source = tmp_path / "backend.json"
    source.write_text(
        json.dumps(
            {
                "name": "git",
                "transport": "stdio",
                "command": "x",
                "headers": {"Authorization": "raw-secret"},
            }
        ),
        encoding="utf-8",
    )

    rc, out, err = run_cli(["backend", "add", "--file", str(source)])

    assert rc == 1
    assert "backend headers 'Authorization' must be exactly a ${VAR}" in err
    assert FakeAdminClient.calls == []

    FakeAdminClient.calls.clear()
    source.write_text(
        json.dumps(
            {
                "name": "git",
                "transport": "stdio",
                "command": "x",
                "env": {"GITHUB_TOKEN": "raw-secret"},
            }
        ),
        encoding="utf-8",
    )
    rc, out, err = run_cli(["backend", "add", "--file", str(source)])
    assert rc == 1
    assert "backend env 'GITHUB_TOKEN' must be exactly a ${VAR}" in err
    assert FakeAdminClient.calls == []


def test_backend_add_uses_backend_url_flag_for_http_transport(fake_admin):
    fake_admin.responses[("POST", "/admin/api/backend")] = {
        "ok": True,
        "reloaded": "hot-add",
        "backend": "git",
    }

    rc, out, err = run_cli(
        [
            "backend",
            "add",
            "git",
            "--transport",
            "http",
            "--backend-url",
            "http://example.com/git",
        ]
    )

    assert rc == 0
    payload = FakeAdminClient.calls[0]["payload"]
    assert payload["url"] == "http://example.com/git"


def test_backend_add_global_url_flag_does_not_serve_as_backend_url(fake_admin):
    # The global --url is reserved for the Admin API base URL anywhere in argv;
    # a backend URL must come from --backend-url.
    rc, out, err = run_cli(
        ["backend", "add", "git", "--transport", "http", "--url", "http://x/git"]
    )
    assert rc == 1
    assert "--backend-url is required for http backends" in err
    assert FakeAdminClient.calls == []


def test_backend_remove_requires_yes(fake_admin):
    rc, out, err = run_cli(["backend", "remove", "db"])
    assert rc == 1
    assert "requires --yes" in err
    assert FakeAdminClient.calls == []


def test_backend_remove_with_yes(fake_admin):
    fake_admin.responses[("DELETE", "/admin/api/backend/db")] = {
        "ok": True,
        "reloaded": "restarting",
    }

    rc, out, err = run_cli(["backend", "remove", "db", "--yes"])

    assert rc == 0
    assert "removed backend 'db'" in out
    expect_calls(call("DELETE", "/admin/api/backend/db"))


def test_backend_rename_payload(fake_admin):
    fake_admin.responses[("POST", "/admin/api/backend/db/rename")] = {
        "ok": True,
        "reloaded": "hot-rename",
        "old_endpoint": "http://127.0.0.1:9100/db/mcp",
        "new_endpoint": "http://127.0.0.1:9100/data/mcp",
        "old_registration": "gateway-db",
        "new_registration": "gateway-data",
    }

    rc, out, err = run_cli(["backend", "rename", "db", "data"])

    assert rc == 0
    assert "renamed 'db' -> 'data'" in out
    expect_calls(
        call(
            "POST",
            "/admin/api/backend/db/rename",
            payload={"value": "data"},
        )
    )


def test_backend_rename_rejects_invalid_name(fake_admin):
    rc, out, err = run_cli(["backend", "rename", "db", "bad name!"])
    assert rc == 1
    assert "invalid backend name" in err
    assert FakeAdminClient.calls == []


def test_backend_display_name_sets_value(fake_admin):
    fake_admin.responses[("POST", "/admin/api/backend/db/display-name")] = {"ok": True}

    rc, out, err = run_cli(["backend", "display-name", "db", "Primary DB"])

    assert rc == 0
    assert "display name of 'db' set to 'Primary DB'" in out
    expect_calls(
        call(
            "POST",
            "/admin/api/backend/db/display-name",
            payload={"value": "Primary DB"},
        )
    )


def test_backend_display_name_clear_flag_sends_empty_value(fake_admin):
    fake_admin.responses[("POST", "/admin/api/backend/db/display-name")] = {"ok": True}

    rc, out, err = run_cli(["backend", "display-name", "db", "--clear"])

    assert rc == 0
    assert "display name of 'db' cleared" in out
    expect_calls(
        call(
            "POST",
            "/admin/api/backend/db/display-name",
            payload={"value": ""},
        )
    )


def test_backend_display_name_empty_positional_also_clears(fake_admin):
    fake_admin.responses[("POST", "/admin/api/backend/db/display-name")] = {"ok": True}

    rc, out, err = run_cli(["backend", "display-name", "db", ""])

    assert rc == 0
    assert "display name of 'db' cleared" in out
    expect_calls(
        call(
            "POST",
            "/admin/api/backend/db/display-name",
            payload={"value": ""},
        )
    )


def test_backend_display_name_requires_value_or_clear(fake_admin):
    rc, out, err = run_cli(["backend", "display-name", "db"])
    assert rc == 1
    assert "specify a display name VALUE, or --clear" in err
    assert FakeAdminClient.calls == []


def test_backend_display_name_rejects_clear_with_value(fake_admin):
    rc, out, err = run_cli(["backend", "display-name", "db", "x", "--clear"])
    assert rc == 1
    assert "cannot be combined" in err
    assert FakeAdminClient.calls == []


@pytest.mark.parametrize(
    "argv, method, path, payload, human",
    [
        (
            ["backend", "enable", "db"],
            "POST",
            "/admin/api/backend/db/enabled",
            {"value": True},
            "enabled 'db'",
        ),
        (
            ["backend", "disable", "db"],
            "POST",
            "/admin/api/backend/db/enabled",
            {"value": False},
            "disabled 'db'",
        ),
        (
            ["backend", "pin", "db"],
            "POST",
            "/admin/api/backend/db/pin",
            {"value": True},
            "pinned 'db'",
        ),
        (
            ["backend", "unpin", "db"],
            "POST",
            "/admin/api/backend/db/pin",
            {"value": False},
            "unpinned 'db'",
        ),
        (
            ["backend", "session", "db", "--stateless"],
            "POST",
            "/admin/api/backend/db/stateless",
            {"value": True},
            "stateless sessions",
        ),
        (
            ["backend", "session", "db", "--warm"],
            "POST",
            "/admin/api/backend/db/stateless",
            {"value": False},
            "warm sessions",
        ),
        (
            ["backend", "enable-all"],
            "POST",
            "/admin/api/enabled",
            {"value": True},
            "enabled all backends",
        ),
        (
            ["backend", "disable-all"],
            "POST",
            "/admin/api/enabled",
            {"value": False},
            "disabled all backends",
        ),
    ],
)
def test_backend_flag_toggles(fake_admin, argv, method, path, payload, human):
    fake_admin.responses[(method, path)] = {"ok": True, "reloaded": "in-process"}

    rc, out, err = run_cli(argv)

    assert rc == 0
    assert human in out
    expect_calls(call(method, path, payload=payload))


def test_backend_session_requires_strategy(fake_admin):
    rc, out, err = run_cli(["backend", "session", "db"])
    assert rc == 2  # argparse mutually-exclusive group is required
    assert FakeAdminClient.calls == []


def test_backend_inspect(fake_admin):
    fake_admin.responses[("POST", "/admin/api/introspect/db")] = {
        "ok": True,
        "status": "refreshed",
        "changed": True,
        "tools": 3,
    }

    rc, out, err = run_cli(["backend", "inspect", "db"])

    assert rc == 0
    assert "re-inspected 'db'" in out
    assert "3 tools" in out
    expect_calls(call("POST", "/admin/api/introspect/db"))


def test_backend_refresh(fake_admin):
    fake_admin.responses[("POST", "/admin/api/refresh")] = {
        "ok": True,
        "backends": {"db": {"status": "refreshed", "tools": 2, "changed": False}},
    }

    rc, out, err = run_cli(["backend", "refresh"])

    assert rc == 0
    assert "db: refreshed, 2 tools, unchanged" in out
    expect_calls(call("POST", "/admin/api/refresh"))


# ---------------------------------------------------------------------------
# surface domain: tool / resource / prompt / instructions
# ---------------------------------------------------------------------------


def _dangling_backend() -> dict:
    return make_backend(
        "db",
        dangling=[
            {
                "original": "old_q",
                "name": "old_q",
                "has_description": True,
                "enabled": True,
            }
        ],
    )


def _renamed_tool_backend() -> dict:
    tool = make_backend()["tools"][0] | {"name": "run_query"}
    return make_backend("db", tools=[tool])


# -- tool ----------------------------------------------------------------


# ---------------------------------------------------------------------------
# backend limits (show / update, #286)
# ---------------------------------------------------------------------------


def test_backend_limits_read_view_without_flags(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))

    rc, out, err = run_cli(["backend", "limits", "db"])

    assert rc == 0
    assert "backend: db" in out
    assert "server_instructions_max_bytes: inherit (2048 effective)" in out
    assert "tool_description_max_bytes: inherit (unlimited effective)" in out
    assert "instructions_bytes: 13" in out
    expect_calls(call("GET", "/admin/api/state", params=None))

    rc, out, err = run_cli(["backend", "limits", "db", "--json"])
    assert rc == 0
    data = load_json(out)
    assert data["backend"] == "db"
    assert data["server_instructions_max_bytes"] is None
    assert data["effective_server_instructions_max_bytes"] == 2048
    assert data["tool_description_max_bytes"] is None
    assert data["effective_tool_description_max_bytes"] is None
    assert data["instructions_bytes"] == 13


def test_backend_limits_read_view_shows_stored_values(fake_admin):
    backend = make_backend(
        "db",
        server_instructions_max_bytes=4096,
        effective_server_instructions_max_bytes=4096,
        tool_description_max_bytes=8192,
        effective_tool_description_max_bytes=8192,
        instructions_bytes=42,
    )
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(backend)

    rc, out, err = run_cli(["backend", "limits", "db"])

    assert rc == 0
    assert "server_instructions_max_bytes: 4096" in out
    assert "tool_description_max_bytes: 8192" in out
    assert "instructions_bytes: 42" in out


def test_backend_limits_set_finite_values(fake_admin):
    fake_admin.responses[("PUT", "/admin/api/backend/db/limits")] = {
        "ok": True,
        "reloaded": "in-process",
    }

    rc, out, err = run_cli(
        [
            "backend",
            "limits",
            "db",
            "--server-instructions-max-bytes",
            "4096",
            "--tool-description-max-bytes",
            "8192",
        ]
    )

    assert rc == 0
    assert "limits of 'db' updated (hot reload)" in out
    expect_calls(
        call(
            "PUT",
            "/admin/api/backend/db/limits",
            payload={
                "server_instructions_max_bytes": 4096,
                "tool_description_max_bytes": 8192,
            },
        )
    )


def test_backend_limits_set_inherit_sends_null(fake_admin):
    # 'inherit' clears the override: null in the payload (gateway global
    # applies). Only the given keys are sent — absent keys keep stored values.
    fake_admin.responses[("PUT", "/admin/api/backend/db/limits")] = {
        "ok": True,
        "reloaded": "in-process",
    }

    rc, out, err = run_cli(
        ["backend", "limits", "db", "--tool-description-max-bytes", "inherit"]
    )

    assert rc == 0
    expect_calls(
        call(
            "PUT",
            "/admin/api/backend/db/limits",
            payload={"tool_description_max_bytes": None},
        )
    )


def test_backend_limits_rejects_invalid_values_locally(fake_admin):
    cases = [
        ["--server-instructions-max-bytes", "true"],
        ["--server-instructions-max-bytes", "0"],
        ["--server-instructions-max-bytes", "1048577"],
        ["--tool-description-max-bytes", "false"],
        ["--tool-description-max-bytes", "-1"],
        ["--tool-description-max-bytes", "2.5"],
    ]
    for extra in cases:
        rc, out, err = run_cli(["backend", "limits", "db", *extra])
        assert rc == 1, extra
        assert "must be an integer between 1 and 1048576" in err, extra
        assert FakeAdminClient.calls == [], extra


def test_backend_limits_unknown_backend(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))

    rc, out, err = run_cli(["backend", "limits", "nope"])

    assert rc == 1
    assert "unknown backend" in err


def test_backend_limits_server_400_propagates(fake_admin):
    fake_admin.responses[("PUT", "/admin/api/backend/db/limits")] = CLIError(
        "gateway 400 Bad Request for "
        "http://127.0.0.1:9100/admin/api/backend/db/limits: unknown key",
        response={"error": "unknown key"},
    )

    rc, out, err = run_cli(
        ["backend", "limits", "db", "--server-instructions-max-bytes", "4096"]
    )

    assert rc == 1
    assert out == ""
    assert "400" in err
    assert "unknown key" in err


def test_backend_show_human_includes_metadata_limits(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    fake_admin.responses[("GET", "/admin/api/status")] = make_status()

    rc, out, err = run_cli(["backend", "show", "db"])

    assert rc == 0
    assert "server_instructions_max_bytes: inherit (2048 effective)" in out
    assert "tool_description_max_bytes: inherit (unlimited effective)" in out
    assert "instructions_bytes: 13" in out


def test_tool_list_human_and_json(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))

    rc, out, err = run_cli(["tool", "list"])

    assert rc == 0
    assert "db" in out
    assert "query" in out
    assert "enabled" in out

    rc, out, err = run_cli(["tool", "list", "--json"])
    assert rc == 0
    data = load_json(out)
    assert data[0]["backend"] == "db"
    assert data[0]["original"] == "query"


def test_tool_list_backend_filter_and_unknown_backend(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(
        make_backend("db"), make_backend("git")
    )

    rc, out, err = run_cli(["tool", "list", "--backend", "git"])
    assert rc == 0
    assert "git" in out
    assert "db" not in out

    rc, out, err = run_cli(["tool", "list", "--backend", "nope"])
    assert rc == 1
    assert "unknown backend" in err


def test_tool_list_dangling(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(_dangling_backend())

    rc, out, err = run_cli(["tool", "list", "--dangling"])

    assert rc == 0
    assert "old_q" in out
    assert "query" not in out  # live tools are not part of the dangling view


def test_tool_show(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))

    rc, out, err = run_cli(["tool", "show", "db", "query", "--json"])

    assert rc == 0
    data = load_json(out)
    assert data["backend"] == "db"
    assert data["original"] == "query"


def test_tool_show_missing_tool(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))

    rc, out, err = run_cli(["tool", "show", "db", "nope"])

    assert rc == 1
    assert "not found" in err


def test_tool_set_scalar_flags_build_override(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    fake_admin.responses[("PUT", "/admin/api/override")] = {
        "ok": True,
        "reloaded": "in-process",
    }

    rc, out, err = run_cli(
        [
            "tool",
            "set",
            "db",
            "query",
            "--name",
            "run_query",
            "--description",
            "executes a query",
            "--pin",
        ]
    )

    assert rc == 0
    assert "updated query on db" in out
    expect_calls(
        call("GET", "/admin/api/state"),
        call(
            "PUT",
            "/admin/api/override",
            payload={
                "backend": "db",
                "tool_original": "query",
                "override": {
                    "name": "run_query",
                    "description": "executes a query",
                    "always_load": True,
                },
            },
        ),
    )


def test_tool_set_from_file_including_params(fake_admin, tmp_path):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    fake_admin.responses[("PUT", "/admin/api/override")] = {
        "ok": True,
        "reloaded": "in-process",
    }
    override = {
        "description": "from file",
        "params": [{"original": "q", "name": "question"}],
    }
    source = tmp_path / "override.json"
    source.write_text(json.dumps(override), encoding="utf-8")

    rc, out, err = run_cli(["tool", "set", "db", "query", "--file", str(source)])

    assert rc == 0
    payload = FakeAdminClient.calls[1]["payload"]
    assert payload["override"]["description"] == "from file"
    assert payload["override"]["params"] == [{"original": "q", "name": "question"}]


def test_tool_set_auto_uniquify_adds_on_collision(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    fake_admin.responses[("PUT", "/admin/api/override")] = {
        "ok": True,
        "reloaded": "in-process",
        "uniquified": True,
        "name": "run_query_2",
    }

    rc, out, err = run_cli(
        [
            "tool",
            "set",
            "db",
            "query",
            "--name",
            "run_query",
            "--auto-uniquify",
        ]
    )

    assert rc == 0
    assert "uniquified to 'run_query_2'" in out
    payload = FakeAdminClient.calls[1]["payload"]
    assert payload["on_collision"] == "uniquify"
    assert payload["backend"] == "db"
    assert payload["tool_original"] == "query"
    assert payload["override"] == {"name": "run_query"}


def test_tool_set_without_auto_uniquify_omits_on_collision(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    fake_admin.responses[("PUT", "/admin/api/override")] = {"ok": True}

    rc, out, err = run_cli(["tool", "set", "db", "query", "--name", "run_query"])

    assert rc == 0
    payload = FakeAdminClient.calls[1]["payload"]
    assert "on_collision" not in payload


def test_tool_set_file_rejects_unknown_top_level(fake_admin, tmp_path):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    source = tmp_path / "override.json"
    source.write_text(json.dumps({"name": "run_query", "bogus": 1}), encoding="utf-8")

    rc, out, err = run_cli(["tool", "set", "db", "query", "--file", str(source)])

    assert rc == 1
    assert "override file" in err
    assert "contains unknown field: bogus" in err
    # only the read-only state fetch happened; no mutation was sent
    assert [c["path"] for c in FakeAdminClient.calls] == ["/admin/api/state"]


def test_tool_set_file_rejects_unknown_param_keys(fake_admin, tmp_path):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    source = tmp_path / "override.json"
    source.write_text(
        json.dumps({"params": [{"original": "q", "bogus": 1}]}),
        encoding="utf-8",
    )

    rc, out, err = run_cli(["tool", "set", "db", "query", "--file", str(source)])

    assert rc == 1
    assert "override param 'q' contains unknown field: bogus" in err
    assert [c["path"] for c in FakeAdminClient.calls] == ["/admin/api/state"]


def test_resource_set_file_rejects_unknown_key(fake_admin, tmp_path):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    source = tmp_path / "override.json"
    source.write_text(json.dumps({"name": "Schema v2", "bogus": 1}), encoding="utf-8")

    rc, out, err = run_cli(
        ["resource", "set", "db", "db://schema", "--file", str(source)]
    )

    assert rc == 1
    assert "override file" in err
    assert "contains unknown field: bogus" in err
    assert [c["path"] for c in FakeAdminClient.calls] == ["/admin/api/state"]


def test_prompt_set_file_rejects_unknown_keys(fake_admin, tmp_path):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    source = tmp_path / "override.json"
    source.write_text(json.dumps({"bogus": 1}), encoding="utf-8")

    rc, out, err = run_cli(["prompt", "set", "db", "explain", "--file", str(source)])

    assert rc == 1
    assert "override file" in err
    assert "contains unknown field: bogus" in err
    assert [c["path"] for c in FakeAdminClient.calls] == ["/admin/api/state"]


def test_prompt_set_file_rejects_unknown_arg_keys(fake_admin, tmp_path):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    source = tmp_path / "override.json"
    source.write_text(
        json.dumps({"args": [{"original": "a1", "bogus": 1}]}),
        encoding="utf-8",
    )

    rc, out, err = run_cli(["prompt", "set", "db", "explain", "--file", str(source)])

    assert rc == 1
    assert "override arg 'a1' contains unknown field: bogus" in err
    assert [c["path"] for c in FakeAdminClient.calls] == ["/admin/api/state"]


def test_tool_set_nothing_to_change(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))

    rc, out, err = run_cli(["tool", "set", "db", "query"])

    assert rc == 1
    assert "nothing to change" in err
    expect_calls(call("GET", "/admin/api/state"))


def test_tool_set_description_limit_finite(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    fake_admin.responses[("PUT", "/admin/api/override")] = {"ok": True}

    rc, out, err = run_cli(
        ["tool", "set", "db", "query", "--description-max-bytes", "4096"]
    )

    assert rc == 0
    expect_calls(
        call("GET", "/admin/api/state", params=None),
        call(
            "PUT",
            "/admin/api/override",
            payload={
                "backend": "db",
                "tool_original": "query",
                "override": {"description_max_bytes": 4096},
            },
        ),
    )


def test_tool_set_description_limit_inherit_sends_null(fake_admin):
    # 'inherit' clears a stored per-tool cap: null override = inherit the
    # backend/gateway limit (server #139 merge: present key, null value).
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    fake_admin.responses[("PUT", "/admin/api/override")] = {"ok": True}

    rc, out, err = run_cli(
        ["tool", "set", "db", "query", "--description-max-bytes", "inherit"]
    )

    assert rc == 0
    expect_calls(
        call("GET", "/admin/api/state", params=None),
        call(
            "PUT",
            "/admin/api/override",
            payload={
                "backend": "db",
                "tool_original": "query",
                "override": {"description_max_bytes": None},
            },
        ),
    )


def test_tool_set_description_limit_rejects_invalid_locally(fake_admin):
    for value in ("true", "false", "0", "-1", "1048577", "abc", "1.5"):
        rc, out, err = run_cli(
            ["tool", "set", "db", "query", "--description-max-bytes", value]
        )
        assert rc == 1, value
        assert "must be an integer between 1 and 1048576" in err, value
        assert FakeAdminClient.calls == [], value


def test_tool_set_file_accepts_description_max_bytes(fake_admin, tmp_path):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    fake_admin.responses[("PUT", "/admin/api/override")] = {"ok": True}
    source = tmp_path / "override.json"
    source.write_text(json.dumps({"description_max_bytes": 2048}), encoding="utf-8")

    rc, out, err = run_cli(["tool", "set", "db", "query", "--file", str(source)])

    assert rc == 0
    put = FakeAdminClient.calls[1]
    assert put["payload"]["override"]["description_max_bytes"] == 2048


def test_tool_show_human_includes_description_bytes(fake_admin):
    tool = make_backend("db")["tools"][0]
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(
        make_backend("db", tools=[{**tool, "description_max_bytes": 4096}])
    )

    rc, out, err = run_cli(["tool", "show", "db", "query"])

    assert rc == 0
    assert "description_max_bytes: 4096" in out
    assert "description_bytes: 12" in out


def test_tool_list_json_includes_limit_fields(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))

    rc, out, err = run_cli(["tool", "list", "--json"])

    assert rc == 0
    data = load_json(out)
    assert data[0]["description_max_bytes"] is None
    assert data[0]["effective_description_max_bytes"] is None
    assert data[0]["default_description_bytes"] == 12
    assert data[0]["effective_description_bytes"] == 12


def test_tool_reset_requires_yes(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))

    rc, out, err = run_cli(["tool", "reset", "db", "query"])

    assert rc == 1
    assert "requires --yes" in err
    expect_calls(call("GET", "/admin/api/state"))


def test_tool_reset_with_yes(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    fake_admin.responses[("POST", "/admin/api/reset")] = {"ok": True}

    rc, out, err = run_cli(["tool", "reset", "db", "query", "--yes"])

    assert rc == 0
    assert "reset query on db to defaults" in out
    expect_calls(
        call("GET", "/admin/api/state"),
        call(
            "POST",
            "/admin/api/reset",
            payload={"backend": "db", "tool_original": "query"},
        ),
    )


def test_tool_run_posts_args_and_resolves_broadcast_name(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(
        _renamed_tool_backend()
    )
    fake_admin.responses[("POST", "/admin/api/run")] = {
        "ok": True,
        "is_error": False,
        "ms": 2.5,
        "content": [{"type": "text", "text": "42"}],
        "structured": None,
    }

    rc, out, err = run_cli(
        ["tool", "run", "db", "query", "--arg", "q", "hello", "--arg", "limit", "10"]
    )

    assert rc == 0
    assert "42" in out
    expect_calls(
        call("GET", "/admin/api/state"),
        call(
            "POST",
            "/admin/api/run",
            payload={
                "backend": "db",
                "tool": "run_query",  # original resolved to the broadcast name
                "args": {"q": "hello", "limit": 10},
            },
        ),
    )


def test_tool_run_args_from_file(fake_admin, tmp_path):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    fake_admin.responses[("POST", "/admin/api/run")] = {
        "ok": True,
        "is_error": False,
        "ms": 1.0,
        "content": [],
        "structured": {"rows": 3},
    }
    args_file = tmp_path / "args.json"
    args_file.write_text(json.dumps({"q": "SELECT 1"}), encoding="utf-8")

    rc, out, err = run_cli(["tool", "run", "db", "query", "--file", str(args_file)])

    assert rc == 0
    assert "rows" in out  # structured result is rendered
    payload = FakeAdminClient.calls[1]["payload"]
    assert payload["args"] == {"q": "SELECT 1"}


def test_tool_run_is_error_exits_nonzero(fake_admin):
    # HTTP 200 with is_error=true means the tool ran but failed: the receipt
    # is still emitted, and the exit code reflects the failure.
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    fake_admin.responses[("POST", "/admin/api/run")] = {
        "ok": True,
        "is_error": True,
        "ms": 9.9,
        "content": [{"type": "text", "text": "boom happened"}],
        "structured": None,
    }

    rc, out, err = run_cli(["tool", "run", "db", "query", "--arg", "q", "x"])

    assert rc == 1
    assert "boom happened" in out  # the receipt is still shown
    assert "tool error" in out
    assert "tool-level error" in err
    assert "boom happened" not in err  # stderr stays concise, no content dump


def test_tool_run_success_exits_zero(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    fake_admin.responses[("POST", "/admin/api/run")] = {
        "ok": True,
        "is_error": False,
        "ms": 1.0,
        "content": [],
        "structured": None,
    }

    rc, out, err = run_cli(["tool", "run", "db", "query"])

    assert rc == 0
    assert "(ok · 1.0 ms)" in out


def test_tool_migrate_posts_from_and_to(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(_dangling_backend())
    fake_admin.responses[("POST", "/admin/api/backend/db/migrate-override")] = {
        "ok": True,
        "reloaded": "in-process",
        "carried_params": ["q"],
    }

    rc, out, err = run_cli(["tool", "migrate", "db", "old_q", "query"])

    assert rc == 0
    assert "migrated old_q" in out
    assert "query on db" in out
    assert "carried: q" in out
    expect_calls(
        call("GET", "/admin/api/state"),
        call(
            "POST",
            "/admin/api/backend/db/migrate-override",
            payload={"from": "old_q", "to": "query"},
        ),
    )


def test_tool_migrate_rejects_non_dangling_source(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(_dangling_backend())

    rc, out, err = run_cli(["tool", "migrate", "db", "query", "other"])

    assert rc == 1
    assert "no dangling override" in err
    expect_calls(call("GET", "/admin/api/state"))


def test_tool_discard_requires_yes(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(_dangling_backend())

    rc, out, err = run_cli(["tool", "discard", "db", "old_q"])

    assert rc == 1
    assert "requires --yes" in err
    expect_calls(call("GET", "/admin/api/state"))


def test_tool_discard_with_yes(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(_dangling_backend())
    fake_admin.responses[("POST", "/admin/api/backend/db/discard-override")] = {
        "ok": True,
        "reloaded": "in-process",
    }

    rc, out, err = run_cli(["tool", "discard", "db", "old_q", "--yes"])

    assert rc == 0
    assert "discarded stale override old_q on db" in out
    expect_calls(
        call("GET", "/admin/api/state"),
        call(
            "POST",
            "/admin/api/backend/db/discard-override",
            payload={"original": "old_q"},
        ),
    )


def test_tool_discard_rejects_live_tool(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))

    rc, out, err = run_cli(["tool", "discard", "db", "query", "--yes"])

    assert rc == 1
    assert "no dangling override" in err
    expect_calls(call("GET", "/admin/api/state"))


# -- resource ------------------------------------------------------------


def test_resource_list_and_show(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))

    rc, out, err = run_cli(["resource", "list"])
    assert rc == 0
    assert "db://schema" in out

    rc, out, err = run_cli(["resource", "show", "db", "db://schema", "--json"])
    assert rc == 0
    data = load_json(out)
    assert data["uri"] == "db://schema"
    assert data["backend"] == "db"


def test_resource_set_payload(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    fake_admin.responses[("PUT", "/admin/api/resource-override")] = {
        "ok": True,
        "reloaded": "in-process",
    }

    rc, out, err = run_cli(
        ["resource", "set", "db", "db://schema", "--name", "Schema v2"]
    )

    assert rc == 0
    assert "updated resource db://schema on db" in out
    expect_calls(
        call("GET", "/admin/api/state"),
        call(
            "PUT",
            "/admin/api/resource-override",
            payload={
                "backend": "db",
                "uri": "db://schema",
                "override": {"name": "Schema v2"},
            },
        ),
    )


def test_resource_reset_requires_yes_then_posts(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))

    rc, out, err = run_cli(["resource", "reset", "db", "db://schema"])
    assert rc == 1
    assert "requires --yes" in err

    FakeAdminClient.calls.clear()
    fake_admin.responses[("POST", "/admin/api/resource-reset")] = {"ok": True}
    rc, out, err = run_cli(["resource", "reset", "db", "db://schema", "--yes"])
    assert rc == 0
    expect_calls(
        call("GET", "/admin/api/state"),
        call(
            "POST",
            "/admin/api/resource-reset",
            payload={"backend": "db", "uri": "db://schema"},
        ),
    )


# -- prompt --------------------------------------------------------------


def test_prompt_list_and_show(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))

    rc, out, err = run_cli(["prompt", "list"])
    assert rc == 0
    assert "explain" in out

    rc, out, err = run_cli(["prompt", "show", "db", "explain", "--json"])
    assert rc == 0
    data = load_json(out)
    assert data["original"] == "explain"
    assert data["backend"] == "db"


def test_prompt_set_payload(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    fake_admin.responses[("PUT", "/admin/api/prompt-override")] = {
        "ok": True,
        "reloaded": "in-process",
    }

    rc, out, err = run_cli(
        ["prompt", "set", "db", "explain", "--description", "Explain SQL"]
    )

    assert rc == 0
    assert "updated prompt explain on db" in out
    expect_calls(
        call("GET", "/admin/api/state"),
        call(
            "PUT",
            "/admin/api/prompt-override",
            payload={
                "backend": "db",
                "prompt_original": "explain",
                "override": {"description": "Explain SQL"},
            },
        ),
    )


def test_prompt_reset_requires_yes_then_posts(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))

    rc, out, err = run_cli(["prompt", "reset", "db", "explain"])
    assert rc == 1
    assert "requires --yes" in err

    FakeAdminClient.calls.clear()
    fake_admin.responses[("POST", "/admin/api/prompt-reset")] = {"ok": True}
    rc, out, err = run_cli(["prompt", "reset", "db", "explain", "--yes"])
    assert rc == 0
    expect_calls(
        call("GET", "/admin/api/state"),
        call(
            "POST",
            "/admin/api/prompt-reset",
            payload={"backend": "db", "prompt_original": "explain"},
        ),
    )


# -- instructions --------------------------------------------------------


def test_instructions_show_one_and_all(fake_admin):
    backend = make_backend(
        "db", default_instructions="be helpful", instructions="be brief"
    )
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(backend)

    rc, out, err = run_cli(["instructions", "show", "db"])
    assert rc == 0
    assert "be brief" in out

    rc, out, err = run_cli(["instructions", "show", "--json"])
    assert rc == 0
    data = load_json(out)
    assert data[0]["backend"] == "db"
    assert data[0]["instructions"] == "be brief"
    assert data[0]["effective"] == "be brief"


def test_instructions_set_positional_and_file(fake_admin, tmp_path):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    fake_admin.responses[("PUT", "/admin/api/instructions")] = {"ok": True}

    rc, out, err = run_cli(["instructions", "set", "db", "be brief"])
    assert rc == 0
    assert "updated instructions for db" in out
    expect_calls(
        call("GET", "/admin/api/state"),
        call(
            "PUT",
            "/admin/api/instructions",
            payload={"backend": "db", "value": "be brief"},
        ),
    )

    text_file = tmp_path / "instructions.txt"
    text_file.write_text("multi\nline\n", encoding="utf-8")
    rc, out, err = run_cli(["instructions", "set", "db", "--file", str(text_file)])
    assert rc == 0
    payload = FakeAdminClient.calls[-1]["payload"]
    assert payload == {"backend": "db", "value": "multi\nline\n"}


def test_instructions_clear_sends_empty_value(fake_admin):
    fake_admin.responses[("GET", "/admin/api/state")] = make_state(make_backend("db"))
    fake_admin.responses[("PUT", "/admin/api/instructions")] = {"ok": True}

    rc, out, err = run_cli(["instructions", "clear", "db"])

    assert rc == 0
    assert "cleared instructions override for db" in out
    expect_calls(
        call("GET", "/admin/api/state"),
        call("PUT", "/admin/api/instructions", payload={"backend": "db", "value": ""}),
    )


# ---------------------------------------------------------------------------
# virtual domain
# ---------------------------------------------------------------------------


def test_virtual_list(fake_admin):
    fake_admin.responses[("GET", "/admin/api/virtual-tools")] = virtual_list_response(
        make_virtual_tool("v1", enabled=True), make_virtual_tool("v2")
    )

    rc, out, err = run_cli(["virtual", "list"])

    assert rc == 0
    assert "v1" in out
    assert "v2" in out
    assert "active" in out and "draft" in out
    expect_calls(call("GET", "/admin/api/virtual-tools"))


def test_virtual_show(fake_admin):
    tool = make_virtual_tool("v1")
    fake_admin.responses[("GET", "/admin/api/virtual-tools")] = virtual_list_response(
        tool
    )

    rc, out, err = run_cli(["virtual", "show", "v1", "--json"])

    assert rc == 0
    data = load_json(out)
    assert data["name"] == "v1"
    assert data["members"][0]["tool_original"] == "query"


def test_virtual_show_unknown(fake_admin):
    fake_admin.responses[("GET", "/admin/api/virtual-tools")] = virtual_list_response(
        make_virtual_tool("v1")
    )

    rc, out, err = run_cli(["virtual", "show", "nope"])

    assert rc == 1
    assert "unknown virtual tool" in err


def test_virtual_catalog(fake_admin):
    fake_admin.responses[("GET", "/admin/api/virtual-catalog")] = {
        "backends": [
            {
                "id": "gateway-db",
                "name": "db",
                "effective_name": "db",
                "enabled": True,
                "tools": [
                    {
                        "original": "query",
                        "effective_name": "query",
                        "description": None,
                        "enabled": True,
                        "params": [],
                    }
                ],
            }
        ]
    }

    rc, out, err = run_cli(["virtual", "catalog"])

    assert rc == 0
    assert "db" in out
    assert "query" in out
    expect_calls(call("GET", "/admin/api/virtual-catalog"))


def test_virtual_create_from_file(fake_admin, tmp_path):
    definition = {
        "name": "v1",
        "description": "A virtual tool",
        "dispatch": "all",
        "inputs": [],
        "members": [{"backend_id": "gateway-db", "tool_original": "query"}],
    }
    source = tmp_path / "def.json"
    source.write_text(json.dumps(definition), encoding="utf-8")
    fake_admin.responses[("POST", "/admin/api/virtual-tools")] = {
        "ok": True,
        "tool": {**definition, "enabled": False},
        "lifecycle": "draft",
    }

    rc, out, err = run_cli(["virtual", "create", "--file", str(source)])

    assert rc == 0
    assert "created virtual tool 'v1' as draft" in out
    expect_calls(call("POST", "/admin/api/virtual-tools", payload=definition))


def test_virtual_create_from_stdin_dash(fake_admin):
    definition = {
        "name": "v1",
        "description": "from stdin",
        "dispatch": "all",
        "members": [],
    }
    fake_admin.responses[("POST", "/admin/api/virtual-tools")] = {
        "ok": True,
        "tool": {**definition, "enabled": False},
        "lifecycle": "draft",
    }

    rc, out, err = run_cli(
        ["virtual", "create", "--file", "-"], stdin_text=json.dumps(definition)
    )

    assert rc == 0
    expect_calls(call("POST", "/admin/api/virtual-tools", payload=definition))


def test_virtual_create_rejects_invalid_json_source(fake_admin, tmp_path):
    source = tmp_path / "bad.json"
    source.write_text("{not json", encoding="utf-8")

    rc, out, err = run_cli(["virtual", "create", "--file", str(source)])

    assert rc == 1
    assert "not valid JSON" in err
    assert FakeAdminClient.calls == []


def test_virtual_update_merges_over_listed_definition(fake_admin):
    tool = make_virtual_tool("v1")
    fake_admin.responses[("GET", "/admin/api/virtual-tools")] = virtual_list_response(
        tool
    )
    fake_admin.responses[("PUT", "/admin/api/virtual-tools/v1")] = {
        "ok": True,
        "tool": {**tool, "description": "new text"},
        "lifecycle": "draft",
    }

    rc, out, err = run_cli(["virtual", "update", "v1", "--description", "new text"])

    assert rc == 0
    assert "updated virtual tool 'v1' as draft" in out
    put = FakeAdminClient.calls[1]
    assert put["method"] == "PUT"
    assert put["path"] == "/admin/api/virtual-tools/v1"
    assert put["payload"]["name"] == "v1"
    assert put["payload"]["description"] == "new text"
    # listing decorations and lifecycle fields are never PUT back
    assert "resolution" not in put["payload"]
    assert "enabled" not in put["payload"]


def test_virtual_update_with_nothing_to_change(fake_admin):
    fake_admin.responses[("GET", "/admin/api/virtual-tools")] = virtual_list_response(
        make_virtual_tool("v1")
    )

    rc, out, err = run_cli(["virtual", "update", "v1"])

    assert rc == 1
    assert "nothing to change" in err
    assert [c["path"] for c in FakeAdminClient.calls] == ["/admin/api/virtual-tools"]


def test_virtual_create_description_limit_finite(fake_admin):
    fake_admin.responses[("POST", "/admin/api/virtual-tools")] = {
        "ok": True,
        "tool": {"name": "v1", "enabled": False},
        "lifecycle": "draft",
    }

    rc, out, err = run_cli(
        ["virtual", "create", "--name", "v1", "--description-max-bytes", "4096"]
    )

    assert rc == 0
    assert "created virtual tool 'v1' as draft" in out
    expect_calls(
        call(
            "POST",
            "/admin/api/virtual-tools",
            payload={"name": "v1", "description_max_bytes": 4096},
        )
    )


def test_virtual_create_description_limit_inherit_sends_null(fake_admin):
    # 'inherit' on a virtual tool = null: follow the gateway-global tool
    # description cap (itself 'unlimited' by default).
    fake_admin.responses[("POST", "/admin/api/virtual-tools")] = {
        "ok": True,
        "tool": {"name": "v1", "enabled": False},
        "lifecycle": "draft",
    }

    rc, out, err = run_cli(
        ["virtual", "create", "--name", "v1", "--description-max-bytes", "inherit"]
    )

    assert rc == 0
    expect_calls(
        call(
            "POST",
            "/admin/api/virtual-tools",
            payload={"name": "v1", "description_max_bytes": None},
        )
    )


def test_virtual_create_description_limit_rejects_invalid_locally(fake_admin):
    for value in ("true", "0", "1048577", "1.5"):
        rc, out, err = run_cli(
            ["virtual", "create", "--name", "v1", "--description-max-bytes", value]
        )
        assert rc == 1, value
        assert "must be an integer between 1 and 1048576" in err, value
        assert FakeAdminClient.calls == [], value


def test_virtual_update_description_limit_merges(fake_admin):
    tool = make_virtual_tool("v1")
    fake_admin.responses[("GET", "/admin/api/virtual-tools")] = virtual_list_response(
        tool
    )
    fake_admin.responses[("PUT", "/admin/api/virtual-tools/v1")] = {
        "ok": True,
        "tool": {**tool, "description_max_bytes": 2048},
        "lifecycle": "draft",
    }

    rc, out, err = run_cli(
        ["virtual", "update", "v1", "--description-max-bytes", "2048"]
    )

    assert rc == 0
    put = FakeAdminClient.calls[1]
    assert put["method"] == "PUT"
    assert put["path"] == "/admin/api/virtual-tools/v1"
    assert put["payload"]["description_max_bytes"] == 2048
    # the stored definition round-trips (inherited limit stays explicit null)
    assert put["payload"]["name"] == "v1"


def test_virtual_update_description_limit_inherit_sends_null(fake_admin):
    tool = make_virtual_tool("v1")
    fake_admin.responses[("GET", "/admin/api/virtual-tools")] = virtual_list_response(
        tool
    )
    fake_admin.responses[("PUT", "/admin/api/virtual-tools/v1")] = {
        "ok": True,
        "tool": tool,
        "lifecycle": "draft",
    }

    rc, out, err = run_cli(
        ["virtual", "update", "v1", "--description-max-bytes", "inherit"]
    )

    assert rc == 0
    put = FakeAdminClient.calls[1]
    assert put["payload"]["description_max_bytes"] is None


def test_virtual_show_human_includes_description_limit(fake_admin):
    fake_admin.responses[("GET", "/admin/api/virtual-tools")] = virtual_list_response(
        make_virtual_tool("v1")
    )

    rc, out, err = run_cli(["virtual", "show", "v1"])

    assert rc == 0
    assert "Description limit: inherit (unlimited effective)" in out
    assert "Description bytes: 14" in out


def test_virtual_delete_requires_yes(fake_admin):
    rc, out, err = run_cli(["virtual", "delete", "v1"])
    assert rc == 1
    assert "requires --yes" in err
    assert FakeAdminClient.calls == []


def test_virtual_delete_with_yes(fake_admin):
    fake_admin.responses[("DELETE", "/admin/api/virtual-tools/v1")] = {"ok": True}

    rc, out, err = run_cli(["virtual", "delete", "v1", "--yes"])

    assert rc == 0
    assert "deleted virtual tool 'v1'" in out
    expect_calls(call("DELETE", "/admin/api/virtual-tools/v1"))


def test_virtual_validate_ok(fake_admin):
    fake_admin.responses[("POST", "/admin/api/virtual-tools/v1/validate")] = {
        "ok": True,
        "members": [
            {
                "label": "db/query",
                "resolved": True,
                "backend": "db",
                "tool_effective": "query",
            }
        ],
    }

    rc, out, err = run_cli(["virtual", "validate", "v1"])

    assert rc == 0
    assert "resolved and valid" in out
    expect_calls(call("POST", "/admin/api/virtual-tools/v1/validate"))


def test_virtual_validate_failure_carries_errors(fake_admin):
    fake_admin.responses[("POST", "/admin/api/virtual-tools/v1/validate")] = CLIError(
        "gateway 400 Bad Request for "
        "http://127.0.0.1:9100/admin/api/virtual-tools/v1/validate",
        response={"ok": False, "errors": ["member db/query unresolved: no such tool"]},
    )

    rc, out, err = run_cli(["virtual", "validate", "v1"])

    assert rc == 1
    assert "validation failed" in err
    assert "unresolved" in err


def test_virtual_test_arguments_from_file_and_flags(fake_admin, tmp_path):
    args_file = tmp_path / "args.json"
    args_file.write_text(json.dumps({"mode": "fast"}), encoding="utf-8")
    fake_admin.responses[("POST", "/admin/api/virtual-tools/v1/test")] = {
        "ok": True,
        "result": {"structured": {"selected": ["db/query"]}},
        "last_test": {"ok": True, "status": "passed", "ms": 3.2},
    }

    rc, out, err = run_cli(
        [
            "virtual",
            "test",
            "v1",
            "--arguments",
            str(args_file),
            "--arg",
            "limit=10",
            "--arg",
            "name=hello",
        ]
    )

    assert rc == 0
    assert "test passed" in out
    expect_calls(
        call(
            "POST",
            "/admin/api/virtual-tools/v1/test",
            payload={"arguments": {"mode": "fast", "limit": 10, "name": "hello"}},
        )
    )


def test_virtual_test_ok_false_exits_nonzero(fake_admin):
    # The test endpoint returns 200 with ok:false when the tool ran but its
    # result is an error: the receipt is emitted, then the exit is nonzero.
    fake_admin.responses[("POST", "/admin/api/virtual-tools/v1/test")] = {
        "ok": False,
        "result": {
            "is_error": True,
            "content": [{"type": "text", "text": "member failed"}],
        },
        "last_test": {"ok": False, "status": "failed", "ms": 1.0},
    }

    rc, out, err = run_cli(["virtual", "test", "v1", "--arg", "q=1"])

    assert rc == 1
    assert "test failed" in out  # receipt shown first
    assert "virtual tool 'v1' test failed" in err


def test_virtual_test_failure_carries_errors(fake_admin):
    fake_admin.responses[("POST", "/admin/api/virtual-tools/v1/test")] = CLIError(
        "gateway 400 Bad Request",
        response={"ok": False, "errors": ["member db/query failed: backend down"]},
    )

    rc, out, err = run_cli(["virtual", "test", "v1"])

    assert rc == 1
    assert "test failed" in err
    assert "backend down" in err


def test_virtual_activate_and_disable(fake_admin):
    fake_admin.responses[("POST", "/admin/api/virtual-tools/v1/activate")] = {
        "ok": True,
        "enabled": True,
        "reloaded": "hot",
    }
    fake_admin.responses[("POST", "/admin/api/virtual-tools/v1/disable")] = {
        "ok": True,
        "enabled": False,
        "reloaded": "hot",
    }

    rc, out, err = run_cli(["virtual", "activate", "v1"])
    assert rc == 0
    assert "activated virtual tool 'v1'" in out
    expect_calls(call("POST", "/admin/api/virtual-tools/v1/activate"))

    rc, out, err = run_cli(["virtual", "disable", "v1"])
    assert rc == 0
    assert "disabled virtual tool 'v1'" in out
    expect_calls(
        call("POST", "/admin/api/virtual-tools/v1/activate"),
        call("POST", "/admin/api/virtual-tools/v1/disable"),
    )


# ---------------------------------------------------------------------------
# settings domain
# ---------------------------------------------------------------------------


def test_settings_show(fake_admin):
    fake_admin.responses[("GET", "/admin/api/settings")] = {
        "bearer_token": "${MCP_GATEWAY_TOKEN}",
        "introspect_interval": 0,
        "log_level": "INFO",
        "log_max_bytes": 10485760,
        "log_backup_count": 3,
        "update_check": True,
    }

    rc, out, err = run_cli(["settings", "show"])

    assert rc == 0
    assert "log_level: INFO" in out
    assert "update_check: True" in out

    rc, out, err = run_cli(["settings", "show", "--json"])
    assert rc == 0
    data = load_json(out)
    assert data["log_level"] == "INFO"


def test_settings_show_includes_utf8_metadata_limits(fake_admin):
    # #286: the gateway-wide UTF-8 metadata limits surface in both views,
    # verbatim from the server (None tool cap renders as 'unlimited').
    fake_admin.responses[("GET", "/admin/api/settings")] = {
        "bearer_token": None,
        "introspect_interval": 0,
        "log_level": "INFO",
        "log_max_bytes": 10485760,
        "log_backup_count": 3,
        "update_check": True,
        "server_instructions_max_bytes": 2048,
        "tool_description_max_bytes": None,
    }

    rc, out, err = run_cli(["settings", "show"])

    assert rc == 0
    assert "server_instructions_max_bytes: 2048" in out
    assert "tool_description_max_bytes: unlimited" in out

    rc, out, err = run_cli(["settings", "show", "--json"])
    assert rc == 0
    data = load_json(out)
    assert data["server_instructions_max_bytes"] == 2048
    assert data["tool_description_max_bytes"] is None


def test_settings_set_limit_flags_finite_values(fake_admin):
    fake_admin.responses[("PUT", "/admin/api/settings")] = {
        "ok": True,
        "reloaded": "restarting",
        "changed": "gateway-settings",
    }

    rc, out, err = run_cli(
        [
            "settings",
            "set",
            "--server-instructions-max-bytes",
            "4096",
            "--tool-description-max-bytes",
            "8192",
        ]
    )

    assert rc == 0
    assert "settings saved" in out
    expect_calls(
        call(
            "PUT",
            "/admin/api/settings",
            payload={
                "server_instructions_max_bytes": 4096,
                "tool_description_max_bytes": 8192,
            },
        )
    )


def test_settings_set_tool_limit_unlimited_sends_null(fake_admin):
    # The top-level tool limit uses the 'unlimited' keyword: null in the
    # payload = no cap (the default).
    fake_admin.responses[("PUT", "/admin/api/settings")] = {
        "ok": True,
        "reloaded": "restarting",
        "changed": "gateway-settings",
    }

    rc, out, err = run_cli(
        ["settings", "set", "--tool-description-max-bytes", "unlimited"]
    )

    assert rc == 0
    expect_calls(
        call("PUT", "/admin/api/settings", payload={"tool_description_max_bytes": None})
    )


def test_settings_set_limits_via_json_pairs(fake_admin):
    fake_admin.responses[("PUT", "/admin/api/settings")] = {
        "ok": True,
        "reloaded": "restarting",
        "changed": "gateway-settings",
    }

    rc, out, err = run_cli(
        [
            "settings",
            "set",
            "--set",
            "server_instructions_max_bytes=4096",
            "--set",
            "tool_description_max_bytes=null",
        ]
    )

    assert rc == 0
    expect_calls(
        call(
            "PUT",
            "/admin/api/settings",
            payload={
                "server_instructions_max_bytes": 4096,
                "tool_description_max_bytes": None,
            },
        )
    )


def test_settings_set_rejects_invalid_limit_values_locally(fake_admin):
    # Bool-like aliases, 0, negatives, non-integers, and >1 MiB must fail
    # before any request — both on the flags and on the --set JSON path.
    flag_cases = [
        ["--server-instructions-max-bytes", "true"],
        ["--server-instructions-max-bytes", "0"],
        ["--server-instructions-max-bytes", "-1"],
        ["--server-instructions-max-bytes", "1048577"],
        ["--server-instructions-max-bytes", "1.5"],
        ["--server-instructions-max-bytes", "1e3"],
        ["--tool-description-max-bytes", "false"],
        ["--tool-description-max-bytes", "0"],
        ["--tool-description-max-bytes", "1048577"],
    ]
    for extra in flag_cases:
        rc, out, err = run_cli(["settings", "set", *extra])
        assert rc == 1, extra
        assert "must be an integer between 1 and 1048576" in err, extra
        assert FakeAdminClient.calls == [], extra

    for pair in (
        "server_instructions_max_bytes=true",
        "server_instructions_max_bytes=0",
        "server_instructions_max_bytes=1048577",
        "tool_description_max_bytes=false",
        "tool_description_max_bytes=0",
        "tool_description_max_bytes=1048577",
    ):
        rc, out, err = run_cli(["settings", "set", "--set", pair])
        assert rc == 1, pair
        assert "must be an integer between 1 and 1048576" in err, pair
        assert FakeAdminClient.calls == [], pair


def test_settings_set_limit_server_400_propagates(fake_admin):
    # The server stays the authority: its 400 (e.g. a cross-field conflict)
    # surfaces on stderr with exit 1, like every other settings rejection.
    fake_admin.responses[("PUT", "/admin/api/settings")] = CLIError(
        "gateway 400 Bad Request for "
        "http://127.0.0.1:9100/admin/api/settings: instructions cap out of range",
        response={"error": "instructions cap out of range"},
    )

    rc, out, err = run_cli(
        ["settings", "set", "--server-instructions-max-bytes", "4096"]
    )

    assert rc == 1
    assert out == ""
    assert "400" in err
    assert "instructions cap out of range" in err


def test_settings_set_pairs_and_flags(fake_admin):
    fake_admin.responses[("PUT", "/admin/api/settings")] = {
        "ok": True,
        "reloaded": "restarting",
        "changed": "gateway-settings",
    }

    rc, out, err = run_cli(
        [
            "settings",
            "set",
            "--set",
            'log_level="DEBUG"',
            "--set",
            "introspect_interval=60",
            "--no-update-check",
        ]
    )

    assert rc == 0
    assert "settings saved" in out
    expect_calls(
        call(
            "PUT",
            "/admin/api/settings",
            payload={
                "log_level": "DEBUG",
                "introspect_interval": 60,
                "update_check": False,
            },
        )
    )


def test_settings_set_bearer_token_accepts_only_env_ref(fake_admin):
    rc, out, err = run_cli(["settings", "set", "--bearer-token", "hunter2"])
    assert rc == 1
    assert "never paste the secret" in err
    assert FakeAdminClient.calls == []

    fake_admin.responses[("PUT", "/admin/api/settings")] = {
        "ok": True,
        "reloaded": "restarting",
    }
    rc, out, err = run_cli(
        ["settings", "set", "--bearer-token", "${MCP_GATEWAY_TOKEN}"]
    )
    assert rc == 0
    expect_calls(
        call(
            "PUT",
            "/admin/api/settings",
            payload={"bearer_token": "${MCP_GATEWAY_TOKEN}"},
        )
    )


def test_settings_set_rejects_unknown_and_readonly_keys(fake_admin):
    rc, out, err = run_cli(["settings", "set", "--set", "bogus=1"])
    assert rc == 1
    assert "unknown settings key" in err
    assert FakeAdminClient.calls == []

    rc, out, err = run_cli(["settings", "set", "--set", 'host="0.0.0.0"'])
    assert rc == 1
    assert "edit config.toml" in err
    assert FakeAdminClient.calls == []


def test_settings_set_rejects_invalid_value_locally(fake_admin):
    rc, out, err = run_cli(["settings", "set", "--set", "introspect_interval=-5"])
    assert rc == 1
    assert "introspect_interval" in err
    assert FakeAdminClient.calls == []


def test_settings_set_with_nothing_to_change(fake_admin):
    rc, out, err = run_cli(["settings", "set"])
    assert rc == 1
    assert "nothing to change" in err
    assert FakeAdminClient.calls == []


def test_settings_export_writes_raw_json(fake_admin):
    bundle = {
        "kind": "mcp-gateway/settings",
        "version": 1,
        "backends": {"db": {"instructions": "be helpful"}},
    }
    fake_admin.responses[("GET", "/admin/api/export")] = bundle

    rc, out, err = run_cli(["settings", "export"])

    assert rc == 0
    assert load_json(out) == bundle
    expect_calls(call("GET", "/admin/api/export"))


def test_settings_export_full_param_and_output_file(fake_admin, tmp_path):
    fake_admin.responses[("GET", "/admin/api/export")] = {"backends": {}}
    target = tmp_path / "bundle.json"

    rc, out, err = run_cli(["settings", "export", "--full", "--output", str(target)])

    assert rc == 0
    assert "settings bundle written to" in out
    assert load_json(target.read_text(encoding="utf-8")) == {"backends": {}}
    expect_calls(call("GET", "/admin/api/export", params={"full": "true"}))


def test_settings_export_unwritable_parent_dir(fake_admin, tmp_path):
    fake_admin.responses[("GET", "/admin/api/export")] = {"backends": {}}
    target = tmp_path / "missing" / "bundle.json"

    rc, out, err = run_cli(["settings", "export", "--output", str(target)])

    assert rc == 1
    assert out == ""  # no partial JSON, no traceback
    assert "could not write" in err
    assert "does not exist" in err
    assert "bundle.json" in err
    assert "Traceback" not in err


def test_settings_export_refuses_existing_file_without_force(fake_admin, tmp_path):
    fake_admin.responses[("GET", "/admin/api/export")] = {"backends": {}}
    target = tmp_path / "bundle.json"
    target.write_text("original", encoding="utf-8")

    rc, out, err = run_cli(["settings", "export", "--output", str(target)])

    assert rc == 1
    assert "refusing to overwrite existing" in err
    assert "--force" in err
    assert target.read_text(encoding="utf-8") == "original"  # untouched
    assert out == ""


def test_settings_export_force_replaces_regular_file_mode_0600(fake_admin, tmp_path):
    bundle = {"backends": {"db": {"instructions": "x"}}}
    fake_admin.responses[("GET", "/admin/api/export")] = bundle
    target = tmp_path / "bundle.json"
    target.write_text("old", encoding="utf-8")

    rc, out, err = run_cli(["settings", "export", "--force", "--output", str(target)])

    assert rc == 0
    assert load_json(target.read_text(encoding="utf-8")) == bundle
    assert target.stat().st_mode & 0o777 == 0o600


def test_settings_export_fresh_install_is_0600_regardless_of_umask(
    fake_admin, tmp_path
):
    fake_admin.responses[("GET", "/admin/api/export")] = {"backends": {}}
    target = tmp_path / "bundle.json"
    old_umask = os.umask(0o022)
    try:
        rc, out, err = run_cli(["settings", "export", "--output", str(target)])
    finally:
        os.umask(old_umask)

    assert rc == 0
    assert target.stat().st_mode & 0o777 == 0o600


def test_settings_export_refuses_symlink_target(fake_admin, tmp_path):
    fake_admin.responses[("GET", "/admin/api/export")] = {"backends": {}}
    real = tmp_path / "real.json"
    real.write_text("precious", encoding="utf-8")
    link = tmp_path / "bundle.json"
    link.symlink_to(real.name)

    for extra in ([], ["--force"]):
        rc, out, err = run_cli(["settings", "export", *extra, "--output", str(link)])
        assert rc == 1
        assert "refusing" in err
        assert out == ""

    assert real.read_text(encoding="utf-8") == "precious"  # target untouched


def test_settings_export_leaves_no_temp_leftovers(fake_admin, tmp_path):
    fake_admin.responses[("GET", "/admin/api/export")] = {"backends": {}}
    target = tmp_path / "bundle.json"

    rc, out, err = run_cli(["settings", "export", "--output", str(target)])
    assert rc == 0

    # A refused overwrite must also leave the directory clean.
    rc, out, err = run_cli(["settings", "export", "--output", str(target)])
    assert rc == 1

    assert [p.name for p in tmp_path.iterdir()] == ["bundle.json"]


def test_settings_import_requires_yes(fake_admin):
    rc, out, err = run_cli(["settings", "import", "-"], stdin_text='{"backends": {}}')
    assert rc == 1
    assert "requires --yes" in err
    assert FakeAdminClient.calls == []


def test_settings_import_posts_bundle_with_replace_mode(fake_admin, tmp_path):
    bundle = {"backends": {"db": {"instructions": "be helpful"}}}
    source = tmp_path / "bundle.json"
    source.write_text(json.dumps(bundle), encoding="utf-8")
    fake_admin.responses[("POST", "/admin/api/import")] = {
        "ok": True,
        "backends": ["db"],
        "mode": "replace",
    }

    rc, out, err = run_cli(["settings", "import", "--yes", str(source)])

    assert rc == 0
    assert "imported settings for 1 backend(s): db (mode: replace)" in out
    expect_calls(
        call(
            "POST",
            "/admin/api/import",
            payload={"settings": bundle, "mode": "replace"},
        )
    )


def test_settings_import_merge_mode(fake_admin, tmp_path):
    bundle = {"backends": {"db": {"log_level": None}}}
    source = tmp_path / "bundle.json"
    source.write_text(json.dumps(bundle), encoding="utf-8")
    fake_admin.responses[("POST", "/admin/api/import")] = {
        "ok": True,
        "backends": ["db"],
        "mode": "merge",
    }

    rc, out, err = run_cli(
        ["settings", "import", "--yes", "--mode", "merge", str(source)]
    )

    assert rc == 0
    expect_calls(
        call(
            "POST",
            "/admin/api/import",
            payload={"settings": bundle, "mode": "merge"},
        )
    )


def test_settings_import_surfaces_server_errors(fake_admin, tmp_path):
    source = tmp_path / "bundle.json"
    source.write_text(json.dumps({"backends": {"ghost": {}}}), encoding="utf-8")
    fake_admin.responses[("POST", "/admin/api/import")] = CLIError(
        "gateway 400 Bad Request",
        response={
            "ok": False,
            "errors": ["ghost: backend not configured on this gateway"],
            "applied": False,
        },
    )

    rc, out, err = run_cli(["settings", "import", "--yes", str(source)])

    assert rc == 1
    assert "backend not configured" in err
    assert out == ""
