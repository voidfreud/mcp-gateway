"""Contracts for the application-owned macOS LaunchAgent lifecycle."""

from __future__ import annotations

import io
import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from mcp_gateway import cli, service


class FakeLaunchctl:
    def __init__(self, *, loaded: bool = False, pid: int = 4242) -> None:
        self.loaded = loaded
        self.pid = pid
        self.calls: list[list[str]] = []
        self.ps_output = ""

    def __call__(self, argv: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[0] == service.PS:
            return subprocess.CompletedProcess(argv, 0, self.ps_output, "")
        action = argv[1]
        if action == "print":
            output = f"\tpid = {self.pid}\n" if self.loaded else ""
            return subprocess.CompletedProcess(
                argv, 0 if self.loaded else 3, output, ""
            )
        if action == "bootout":
            self.loaded = False
        elif action == "bootstrap":
            self.loaded = True
        return subprocess.CompletedProcess(argv, 0, "", "")


def _runtime(fake: FakeLaunchctl) -> service.ServiceRuntime:
    return service.ServiceRuntime(
        runner=fake,
        sleep=lambda _seconds: None,
        platform="darwin",
        uid=501,
        probe=lambda _paths: None,
    )


def _paths(tmp_path: Path) -> service.ServicePaths:
    paths = service.service_paths(tmp_path)
    paths.binary.parent.mkdir(parents=True)
    paths.binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    paths.binary.chmod(0o755)
    return paths


def test_install_renders_versioned_atomic_service_and_captures_path(tmp_path):
    paths = _paths(tmp_path)
    fake = FakeLaunchctl()

    result = service.install_service(
        paths=paths,
        path_value="/custom/bin:/usr/bin",
        runtime=_runtime(fake),
    )

    assert result.changed is True
    assert result.reloaded is True
    assert paths.state_dir.is_dir()
    assert paths.config_dir.is_dir()
    assert paths.state_dir.stat().st_mode & 0o777 == 0o700
    assert paths.config_dir.stat().st_mode & 0o777 == 0o700
    assert paths.wrapper_dir.stat().st_mode & 0o777 == 0o700
    assert not paths.prompted_marker.exists()  # no first-run prompt marker
    assert paths.wrapper.stat().st_mode & 0o777 == 0o700
    assert paths.plist.stat().st_mode & 0o777 == 0o600
    payload = plistlib.loads(paths.plist.read_bytes())
    assert payload["ProgramArguments"] == [str(paths.wrapper)]
    assert payload["WorkingDirectory"] == str(paths.config_dir)
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert payload["ExitTimeOut"] == 15
    environment = payload["EnvironmentVariables"]
    assert environment["PATH"] == f"{paths.binary.parent}:/custom/bin:/usr/bin"
    assert environment["MCP_GATEWAY_CONFIG"] == str(paths.config)
    assert environment["MCP_GATEWAY_SERVICE_TEMPLATE_VERSION"] == "1"
    assert "/opt/homebrew" not in environment["PATH"]
    assert [call[1] for call in fake.calls] == ["print", "bootstrap", "kickstart"]


def test_reinstall_is_idempotent_and_does_not_double_bootstrap(tmp_path):
    paths = _paths(tmp_path)
    fake = FakeLaunchctl()
    runtime = _runtime(fake)
    service.install_service(paths=paths, path_value="/bin", runtime=runtime)
    calls_after_first = list(fake.calls)

    result = service.install_service(paths=paths, path_value="/bin", runtime=runtime)

    assert result.changed is False
    assert result.reloaded is False
    assert fake.calls[len(calls_after_first) :] == [
        [service.LAUNCHCTL, "print", f"gui/501/{service.LABEL}"]
    ]


def test_force_restart_reloads_unchanged_service_for_new_package_code(tmp_path):
    paths = _paths(tmp_path)
    fake = FakeLaunchctl()
    runtime = _runtime(fake)
    service.install_service(paths=paths, path_value="/bin", runtime=runtime)
    fake.calls.clear()

    result = service.install_service(
        paths=paths,
        path_value="/bin",
        force_restart=True,
        runtime=runtime,
    )

    assert result.changed is False
    assert result.reloaded is True
    assert [call[1] for call in fake.calls] == [
        "print",
        "print",
        "bootout",
        "print",
        "bootstrap",
        "kickstart",
    ]


def test_changed_install_boots_out_before_rebootstrap(tmp_path):
    paths = _paths(tmp_path)
    fake = FakeLaunchctl()
    runtime = _runtime(fake)
    service.install_service(paths=paths, path_value="/first", runtime=runtime)
    fake.calls.clear()

    result = service.install_service(paths=paths, path_value="/second", runtime=runtime)

    assert result.changed and result.reloaded
    actions = [call[1] for call in fake.calls]
    assert actions == ["print", "print", "bootout", "print", "bootstrap", "kickstart"]


def test_failed_replacement_restores_previous_service_files(tmp_path):
    paths = _paths(tmp_path)
    fake = FakeLaunchctl()
    runtime = _runtime(fake)
    service.install_service(paths=paths, path_value="/stable", runtime=runtime)
    previous_plist = paths.plist.read_bytes()
    previous_wrapper = paths.wrapper.read_bytes()
    fake.calls.clear()
    failing = service.ServiceRuntime(
        runner=fake,
        sleep=lambda _seconds: None,
        platform="darwin",
        uid=501,
        probe=lambda _paths: (_ for _ in ()).throw(RuntimeError("not ready")),
    )

    with pytest.raises(service.ServiceError, match="previous service state restored"):
        service.install_service(
            paths=paths,
            path_value="/broken-update",
            runtime=failing,
        )

    assert paths.plist.read_bytes() == previous_plist
    assert paths.wrapper.read_bytes() == previous_wrapper
    assert fake.loaded is True
    assert [call[1] for call in fake.calls] == [
        "print",
        "print",
        "bootout",
        "print",
        "bootstrap",
        "kickstart",
        "print",
        "bootout",
        "print",
        "bootstrap",
        "kickstart",
    ]


def test_atomic_replace_failure_keeps_previous_plist(tmp_path, monkeypatch):
    target = tmp_path / "service.plist"
    target.write_bytes(b"previous")

    def fail_replace(_source, _target):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(service.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated crash"):
        service._atomic_write(target, b"truncated-new-content", mode=0o600)

    assert target.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [target]


def test_stale_template_refreshes_without_launchctl_or_path_loss(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    fake = FakeLaunchctl()
    service.install_service(
        paths=paths, path_value="/captured/path", runtime=_runtime(fake)
    )
    payload = plistlib.loads(paths.plist.read_bytes())
    payload["EnvironmentVariables"]["MCP_GATEWAY_SERVICE_TEMPLATE_VERSION"] = "0"
    service._atomic_write(
        paths.plist,
        plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False),
        mode=0o600,
    )
    monkeypatch.setenv("PATH", "/must/not/replace/captured/path")

    assert service.refresh_installed_service(paths=paths, platform="darwin") is True

    refreshed = plistlib.loads(paths.plist.read_bytes())
    assert refreshed["EnvironmentVariables"]["PATH"].endswith("/captured/path")
    assert (
        refreshed["EnvironmentVariables"]["MCP_GATEWAY_SERVICE_TEMPLATE_VERSION"]
        == service.TEMPLATE_VERSION
    )
    for directory in (paths.state_dir, paths.config_dir, paths.wrapper_dir):
        directory.chmod(0o755)
    assert service.refresh_installed_service(paths=paths, platform="darwin") is False
    assert all(
        directory.stat().st_mode & 0o777 == 0o700
        for directory in (paths.state_dir, paths.config_dir, paths.wrapper_dir)
    )


def test_install_migrates_checkout_config_and_removes_legacy_symlink(tmp_path):
    paths = _paths(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "config.toml").write_text("host = '127.0.0.1'\n", encoding="utf-8")
    paths.legacy_link.parent.mkdir(parents=True)
    paths.legacy_link.symlink_to(checkout)

    result = service.install_service(paths=paths, runtime=_runtime(FakeLaunchctl()))

    assert result.migrated_config is True
    assert result.removed_legacy_link is True
    assert paths.config.read_text() == "host = '127.0.0.1'\n"
    assert paths.config.stat().st_mode & 0o777 == 0o600
    assert not paths.legacy_link.exists()


def test_install_cleans_legacy_prompt_marker(tmp_path):
    # #284: installs never create the first-run marker, but a stale one left by
    # a pre-#284 install is swept so no old prompt state survives
    paths = _paths(tmp_path)
    paths.state_dir.mkdir(parents=True)
    paths.prompted_marker.write_text("declined\n", encoding="utf-8")

    service.install_service(paths=paths, runtime=_runtime(FakeLaunchctl()))

    assert not paths.prompted_marker.exists()


def test_missing_binary_wrapper_exits_successfully_without_restart_signal(tmp_path):
    paths = _paths(tmp_path)
    service.install_service(paths=paths, runtime=_runtime(FakeLaunchctl()))
    paths.binary.unlink()

    result = subprocess.run(
        [str(paths.wrapper)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0
    assert "executable missing" in result.stderr
    assert "inert" in result.stderr


def test_uninstall_removes_all_service_artifacts_and_keeps_data(tmp_path):
    paths = _paths(tmp_path)
    fake = FakeLaunchctl()
    runtime = _runtime(fake)
    service.install_service(paths=paths, runtime=runtime)
    paths.legacy_link.parent.mkdir(parents=True)
    paths.legacy_link.symlink_to(tmp_path / "old-checkout")
    paths.config.write_text("host = '127.0.0.1'\n")

    result = service.uninstall_service(paths=paths, purge_data=False, runtime=runtime)

    assert result.unloaded is True
    assert not paths.plist.exists()
    assert not paths.wrapper.exists()
    assert not paths.prompted_marker.exists()
    assert not paths.legacy_link.is_symlink()
    assert paths.config.exists()
    assert paths.state_dir.is_dir()


def test_uninstall_removes_legacy_prompt_marker_without_install(tmp_path):
    # a pre-#284 marker is still cleaned on uninstall even when no install ever
    # ran in this checkout
    paths = _paths(tmp_path)
    paths.state_dir.mkdir(parents=True)
    paths.prompted_marker.write_text("declined\n", encoding="utf-8")

    result = service.uninstall_service(
        paths=paths, purge_data=False, runtime=_runtime(FakeLaunchctl())
    )

    assert not paths.prompted_marker.exists()
    assert paths.prompted_marker in result.removed


def test_uninstall_purges_data_only_when_explicit(tmp_path):
    paths = _paths(tmp_path)
    runtime = _runtime(FakeLaunchctl())
    service.install_service(paths=paths, runtime=runtime)
    paths.config.write_text("host = '127.0.0.1'\n")
    (paths.state_dir / "gateway.log").write_text("history\n")

    result = service.uninstall_service(paths=paths, purge_data=True, runtime=runtime)

    assert result.purged_data is True
    assert not paths.config_dir.exists()
    assert not paths.state_dir.exists()


def test_resource_status_reports_gateway_and_backend_process_tree():
    fake = FakeLaunchctl(loaded=True, pid=100)
    fake.ps_output = """\
100 1 51200 0.2 mcp-gateway
101 100 10240 0.0 backend-one
102 101 20480 0.1 backend-child
999 1 99999 9.9 unrelated
"""

    status = service.resource_status(fake, uid=501)

    assert status.loaded is True
    assert status.pid == 100
    assert status.gateway_rss_bytes == 51200 * 1024
    assert status.child_processes == 2
    assert status.children_rss_bytes == (10240 + 20480) * 1024
    assert status.total_rss_bytes == (51200 + 10240 + 20480) * 1024
    assert status.cpu_percent == 0.2


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_no_args_starts_foreground_without_prompt_or_marker(tmp_path, monkeypatch):
    # #284: the first-run service-install prompt is gone — a bare invocation
    # refreshes the installed service (if any) and runs the gateway in the
    # foreground, writing no prompt and no state marker even on a TTY.
    paths = _paths(tmp_path)
    refreshes: list[dict] = []
    monkeypatch.setattr(
        service,
        "refresh_installed_service",
        lambda **kwargs: refreshes.append(kwargs) or False,
    )
    starts: list[bool] = []
    monkeypatch.setattr(
        "mcp_gateway.server.run_foreground", lambda: starts.append(True)
    )
    output = TTY()

    cli.main([], stdin=TTY(), stdout=output, stderr=io.StringIO())

    assert refreshes == [{}]
    assert starts == [True]
    assert "login service" not in output.getvalue()
    assert not paths.prompted_marker.exists()
    assert not paths.state_dir.exists()


def test_service_uninstall_requires_yes():
    # #284: destructive operations gate on --yes and never prompt, even on a TTY.
    stderr = io.StringIO()
    with pytest.raises(SystemExit, match="1"):
        cli.main(
            ["service", "uninstall"],
            stdin=TTY(),
            stdout=TTY(),
            stderr=stderr,
        )
    assert "requires --yes (refusing to prompt)" in stderr.getvalue()


@pytest.mark.parametrize("extra,purge", [([], False), (["--purge-data"], True)])
def test_legacy_uninstall_flag_implies_yes(monkeypatch, extra, purge):
    # the pre-#284 flag was itself explicit consent: it maps onto the new tree
    # carrying --yes, so it keeps working without a data-retention prompt
    calls: list[dict] = []

    def uninstall(**kwargs):
        calls.append(kwargs)
        return service.UninstallResult(True, (Path("plist"),), purge)

    monkeypatch.setattr(service, "uninstall_service", uninstall)
    output = io.StringIO()

    cli.main(
        ["--uninstall-service", *extra],
        stdin=io.StringIO(),
        stdout=output,
        stderr=io.StringIO(),
    )

    assert calls == [{"purge_data": purge}]
    text = output.getvalue()
    expected = "config and state deleted" if purge else "config and state kept"
    assert "resident service removed;" in text
    assert expected in text


def test_service_install_is_polite_off_macos(tmp_path):
    paths = _paths(tmp_path)
    with pytest.raises(service.ServiceError, match="only on macOS"):
        service.install_service(
            paths=paths,
            runtime=service.ServiceRuntime(platform="linux"),
        )


def test_install_sh_is_thin_dry_run_wrapper(tmp_path):
    script = Path(__file__).resolve().parents[1] / "install.sh"
    result = subprocess.run(
        ["/bin/bash", str(script), "--dry-run"],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "uv tool install --force" in result.stdout
    assert ".local/bin/mcp-gateway --install-service --restart" in result.stdout
    assert not (tmp_path / ".local").exists()


def _update_runner(initial: str = "1.1.0"):
    state = {"version": initial}
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["tool", "install"]:
            state["version"] = argv[-1].rsplit("==", 1)[1]
            return subprocess.CompletedProcess(argv, 0, "installed\n", "")
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(
                argv, 0, f"mcp-gateway {state['version']}\n", ""
            )
        raise AssertionError(argv)

    return run, calls


def _published_update(monkeypatch, *, current: str = "1.1.0", latest: str = "1.2.0"):
    monkeypatch.setattr(service.updates, "installed_version", lambda: current)
    monkeypatch.setattr(service.updates, "latest_version", lambda: latest)
    monkeypatch.setattr(
        service.updates, "version_exists", lambda _version, **_kwargs: True
    )


def test_update_application_installs_exact_latest_and_verifies_command(
    tmp_path, monkeypatch
):
    paths = _paths(tmp_path)
    runner, calls = _update_runner()
    _published_update(monkeypatch)

    result = service.update_application(
        paths=paths,
        binary=paths.binary,
        uv_binary="/uv",
        runner=runner,
        platform="linux",
    )

    assert result == service.UpdateResult("1.1.0", "1.2.0", True, False)
    assert calls == [
        [
            "/uv",
            "tool",
            "install",
            "--no-config",
            "--default-index",
            "https://pypi.org/simple",
            "--index-strategy",
            "first-index",
            "--keyring-provider",
            "disabled",
            "--no-sources",
            "--reinstall",
            "--force",
            "mcp-local-gateway==1.2.0",
        ],
        [str(paths.binary.resolve()), "--version"],
    ]


def test_update_package_install_ignores_ambient_indexes(monkeypatch):
    unsafe = {
        "UV_INDEX": "https://attacker.invalid/simple",
        "UV_DEFAULT_INDEX": "https://attacker.invalid/simple",
        "UV_FIND_LINKS": "/tmp/untrusted-wheels",
        "UV_OVERRIDE": "/tmp/untrusted-overrides.txt",
        "PIP_INDEX_URL": "https://attacker.invalid/simple",
    }
    for key, value in unsafe.items():
        monkeypatch.setenv(key, value)
    observed: dict = {}

    def runner(argv, **kwargs):
        observed["argv"] = argv
        observed["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    service._replace_tool_version("1.2.0", uv="/uv", runner=runner)

    assert set(unsafe).isdisjoint(observed["env"])
    assert observed["argv"][-1] == "mcp-local-gateway==1.2.0"
    assert (
        observed["argv"][observed["argv"].index("--default-index") + 1]
        == "https://pypi.org/simple"
    )


def test_running_gateway_verification_rejects_stale_version_and_path(
    tmp_path, monkeypatch
):
    paths = _paths(tmp_path)
    tool_root = tmp_path / "tools" / "mcp-local-gateway"
    binary = tool_root / "bin" / "mcp-gateway"
    package = tool_root / "lib" / "python3.12" / "site-packages" / "mcp_gateway"
    payload = [f"ok mcp-gateway 1.2.0 @ {package}".encode()]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return payload[0]

    monkeypatch.setattr(
        service.urllib.request, "urlopen", lambda *_a, **_kw: Response()
    )

    service._verify_running_gateway(paths, "1.2.0", binary)

    payload[0] = f"ok mcp-gateway 1.1.0 @ {package}".encode()
    with pytest.raises(service.ServiceError, match="version mismatch"):
        service._verify_running_gateway(paths, "1.2.0", binary)

    payload[0] = f"ok mcp-gateway 1.2.0 @ {tmp_path / 'stale'}".encode()
    with pytest.raises(service.ServiceError, match="path mismatch"):
        service._verify_running_gateway(paths, "1.2.0", binary)


def test_update_application_noops_when_exact_version_is_current(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _published_update(monkeypatch, latest="1.1.0")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("package replacement should not run")

    result = service.update_application(
        "1.1.0",
        paths=paths,
        binary=paths.binary,
        uv_binary="/uv",
        runner=unexpected,
        platform="linux",
    )

    assert result == service.UpdateResult("1.1.0", "1.1.0", False, False)


def test_update_application_restarts_existing_resident_service(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    paths.agents_dir.mkdir(parents=True)
    paths.plist.write_text("old", encoding="utf-8")
    runner, _calls = _update_runner()
    _published_update(monkeypatch)
    installs: list[dict] = []

    def installer(**kwargs):
        installs.append(kwargs)
        return service.InstallResult(True, True, False, False)

    live_checks: list[tuple] = []

    def verify_live(live_paths, version, binary):
        live_checks.append((live_paths, version, binary))

    result = service.update_application(
        paths=paths,
        binary=paths.binary,
        uv_binary="/uv",
        runner=runner,
        platform="darwin",
        installer=installer,
        live_verifier=verify_live,
    )

    assert result.service_restarted is True
    assert installs == [
        {"paths": paths, "binary": paths.binary.resolve(), "force_restart": True}
    ]
    assert live_checks == [(paths, "1.2.0", paths.binary.resolve())]


def test_update_application_refuses_unpublished_target_before_mutation(
    tmp_path, monkeypatch
):
    paths = _paths(tmp_path)
    monkeypatch.setattr(service.updates, "installed_version", lambda: "1.1.0")
    monkeypatch.setattr(
        service.updates,
        "version_exists",
        lambda version, **_kwargs: version == "1.1.0",
    )
    runner, calls = _update_runner()

    with pytest.raises(service.ServiceError, match="is not published"):
        service.update_application(
            "9.9.9",
            paths=paths,
            uv_binary="/uv",
            runner=runner,
            platform="linux",
        )

    assert calls == []


def test_update_application_rolls_package_and_service_back_on_activation_failure(
    tmp_path, monkeypatch
):
    paths = _paths(tmp_path)
    paths.agents_dir.mkdir(parents=True)
    paths.plist.write_text("old", encoding="utf-8")
    runner, calls = _update_runner()
    _published_update(monkeypatch)
    attempts = 0

    def installer(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise service.ServiceError("new daemon unhealthy")
        return service.InstallResult(True, True, False, False)

    live_versions: list[str] = []

    with pytest.raises(
        service.ServiceError,
        match=r"update to 1\.2\.0 failed.*rolled back to 1\.1\.0",
    ):
        service.update_application(
            paths=paths,
            binary=paths.binary,
            uv_binary="/uv",
            runner=runner,
            platform="darwin",
            installer=installer,
            live_verifier=lambda _paths, version, _binary: live_versions.append(
                version
            ),
        )

    package_specs = [call[-1] for call in calls if call[1:3] == ["tool", "install"]]
    assert package_specs == [
        "mcp-local-gateway==1.2.0",
        "mcp-local-gateway==1.1.0",
    ]
    assert attempts == 2
    assert live_versions == ["1.1.0"]


def test_update_cli_supports_latest_and_exact_version(monkeypatch):
    requested: list[str | None] = []

    def update(version):
        requested.append(version)
        return service.UpdateResult("1.1.0", "1.2.0", True, True)

    monkeypatch.setattr(service, "update_application", update)
    latest_output = io.StringIO()
    exact_output = io.StringIO()

    cli.main(["update"], stdout=latest_output, stderr=io.StringIO())
    cli.main(
        ["update", "--version", "1.2.0"],
        stdout=exact_output,
        stderr=io.StringIO(),
    )

    assert requested == [None, "1.2.0"]
    assert "1.1.0 -> 1.2.0" in latest_output.getvalue()
    assert "health/readiness verified" in latest_output.getvalue()
    assert "1.1.0 -> 1.2.0" in exact_output.getvalue()


def test_help_aliases_print_command_guide_to_stdout():
    # the legacy --install-service/--uninstall-service/--service-status flags
    # are hidden aliases now, so the guide lists the new command tree instead
    for alias in ("-h", "--help"):
        out = io.StringIO()
        err = io.StringIO()
        with pytest.raises(SystemExit) as excinfo:
            cli.main([alias], stdin=io.StringIO(), stdout=out, stderr=err)
        assert excinfo.value.code == 0
        text = out.getvalue()
        assert text.startswith("usage: mcp-gateway")
        for command in (
            "run",
            "version",
            "update",
            "service",
            "status",
            "check",
            "restart",
            "logs",
        ):
            assert command in text
        assert "-h, --help" in text
        assert err.getvalue() == ""


def test_invalid_argument_prints_usage_to_stderr_and_exits_2():
    out = io.StringIO()
    err = io.StringIO()
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            ["--bogus"],
            stdin=io.StringIO(),
            stdout=out,
            stderr=err,
        )
    assert excinfo.value.code == 2
    assert out.getvalue() == ""
    assert err.getvalue().startswith("usage: mcp-gateway")


def test_help_flag_wins_over_trailing_args():
    # argparse exits 0 as soon as --help is parsed; trailing tokens are ignored
    out = io.StringIO()
    err = io.StringIO()
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help", "extra"], stdin=io.StringIO(), stdout=out, stderr=err)
    assert excinfo.value.code == 0
    assert out.getvalue().startswith("usage: mcp-gateway")
    assert err.getvalue() == ""


def test_first_successful_service_install_mentions_future_update_command(
    monkeypatch,
):
    fresh = service.InstallResult(
        changed=True, reloaded=True, migrated_config=False, removed_legacy_link=False
    )
    monkeypatch.setattr(service, "install_service", lambda **_kwargs: fresh)
    output = io.StringIO()

    cli.main(["--install-service"], stdout=output, stderr=io.StringIO())

    assert "resident service installed and started" in output.getvalue()
    assert "future updates: run `mcp-gateway update`" in output.getvalue()


@pytest.mark.parametrize("reloaded", [False, True])
def test_unchanged_service_install_does_not_repeat_update_command(
    monkeypatch, reloaded
):
    unchanged = service.InstallResult(
        changed=False,
        reloaded=reloaded,
        migrated_config=False,
        removed_legacy_link=False,
    )
    monkeypatch.setattr(service, "install_service", lambda **_kwargs: unchanged)
    output = io.StringIO()

    cli.main(["--install-service"], stdout=output, stderr=io.StringIO())

    assert "mcp-gateway update" not in output.getvalue()
