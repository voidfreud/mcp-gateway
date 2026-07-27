"""Direct coverage for the reusable Claude/Codex registration policy modules."""

from __future__ import annotations

import anyio

from mcp_gateway import admin, admin_cli, claude_client, codex_client


def test_admin_facade_reexports_client_registration_policy():
    """Existing Admin imports stay aliases of the client-specific policy."""
    assert admin.claude_mcp_command is claude_client.claude_mcp_command
    assert admin.parse_cc_registrations is claude_client.parse_cc_registrations
    assert admin.codex_mcp_command is codex_client.codex_mcp_command
    assert admin.codex_bearer_env_var is codex_client.codex_bearer_env_var
    assert admin.parse_codex_registrations is codex_client.parse_codex_registrations
    assert admin.CLAUDE_SCOPES == claude_client.CLAUDE_SCOPES
    assert admin.CLAUDE_CLI_TIMEOUT == claude_client.CLAUDE_CLI_TIMEOUT
    assert admin.CODEX_CLI_TIMEOUT == codex_client.CODEX_CLI_TIMEOUT


def test_admin_cli_discovery_wrappers_keep_legacy_patch_seam(monkeypatch):
    """Patching ``admin.shutil.which`` still controls both route factories."""
    monkeypatch.setattr(admin.shutil, "which", lambda name: f"/fake/{name}")
    assert admin.claude_cli_path() == "/fake/claude"
    assert admin.codex_cli_path() == "/fake/codex"


def test_codex_cli_path_accepts_injected_environment(tmp_path):
    """The extracted Codex discovery policy is independently reusable."""
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    assert codex_client.codex_cli_path(
        which=lambda _name: None,
        environ={"CODEX_CLI_PATH": str(binary)},
    ) == str(binary)


def test_run_cli_converts_non_utf8_child_output_to_error_tuple():
    """A child emitting non-UTF-8 bytes makes subprocess.run(text=True) raise
    UnicodeDecodeError — run_cli's "never raise" contract must convert it to
    the ``(-1, "", error)`` shape instead of escaping as a 500."""

    def fake_run(*_args, **_kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    rc, stdout, stderr = anyio.run(admin_cli.run_cli, fake_run, ["claude"], 5.0)
    assert rc == -1
    assert stdout == ""
    assert "UnicodeDecodeError" in stderr
