"""Installable-package contracts that do not require a live backend."""

from __future__ import annotations

import shutil
import subprocess
import sys
from importlib.resources import files


def test_package_contains_runtime_assets() -> None:
    package = files("mcp_gateway")
    assert package.joinpath("admin.html").is_file()
    assert package.joinpath("config.default.toml").is_file()


def test_module_entrypoint_supports_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "mcp_gateway", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("mcp-gateway ")


def test_synced_environment_console_entrypoint_supports_version() -> None:
    console = shutil.which("mcp-gateway")
    assert console, "the synced development environment must provide mcp-gateway"
    result = subprocess.run(
        [console, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("mcp-gateway ")
