"""Installable-package contracts that do not require a live backend."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
import zipfile
from importlib.resources import files
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def test_release_artifacts_contain_only_runtime_and_build_metadata(
    tmp_path: Path,
) -> None:
    """Both distributable formats must stay free of repository/local state."""
    uv = shutil.which("uv")
    assert uv, "uv is required to build release artifacts"
    result = subprocess.run(
        [uv, "build", "--offline", "--out-dir", str(tmp_path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    wheel_path = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel_path) as wheel:
        wheel_members = wheel.namelist()
    unexpected_wheel = [
        member
        for member in wheel_members
        if not (member.startswith("mcp_gateway/") or ".dist-info/" in member)
    ]
    assert not unexpected_wheel, unexpected_wheel
    assert "mcp_gateway/admin.html" in wheel_members
    assert "mcp_gateway/config.default.toml" in wheel_members
    assert any(
        member.endswith(".dist-info/licenses/LICENSE") for member in wheel_members
    )

    sdist_path = next(tmp_path.glob("*.tar.gz"))
    with tarfile.open(sdist_path) as sdist:
        sdist_members = [
            member.name for member in sdist.getmembers() if member.isfile()
        ]
    root = Path(sdist_members[0]).parts[0]
    allowed_sdist_files = {
        ".gitignore",
        "LICENSE",
        "PKG-INFO",
        "README.md",
        "pyproject.toml",
    }
    unexpected_sdist = [
        member
        for member in sdist_members
        if (relative := member.removeprefix(f"{root}/")) not in allowed_sdist_files
        and not relative.startswith("src/mcp_gateway/")
    ]
    assert not unexpected_sdist, unexpected_sdist
    assert f"{root}/LICENSE" in sdist_members
    assert f"{root}/README.md" in sdist_members
    assert f"{root}/src/mcp_gateway/admin.html" in sdist_members
    assert f"{root}/src/mcp_gateway/config.default.toml" in sdist_members
