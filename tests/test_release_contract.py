"""Focused contracts for the local, non-publishing release verifier."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from tools import release_contract as contract

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL = PROJECT_ROOT / "tools" / "release_contract.py"
VERSION = "1.0.0"


def _release_root(
    tmp_path: Path, version: str = VERSION, directory: str = "release-root"
) -> Path:
    root = tmp_path / directory
    root.mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "mcp-local-gateway"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        f'version = 1\n[[package]]\nname = "mcp-local-gateway"\n'
        f'version = "{version}"\n',
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [Unreleased]\n\n## [{version}] - 2026-07-12\n",
        encoding="utf-8",
    )
    return root


def _write_wheel(
    output: Path,
    version: str = VERSION,
    metadata_version: str | None = None,
    requirements: tuple[str, ...] = (),
) -> Path:
    wheel_name, _, _ = contract.expected_artifact_names(version)
    dist_info = f"mcp_local_gateway-{version}.dist-info"
    with zipfile.ZipFile(output / wheel_name, "w") as archive:
        archive.writestr("mcp_gateway/__init__.py", "")
        archive.writestr("mcp_gateway/admin.html", "<html></html>")
        archive.writestr("mcp_gateway/config.default.toml", "[gateway]\n")
        metadata = (
            "Metadata-Version: 2.4\nName: mcp-local-gateway\n"
            f"Version: {metadata_version or version}\n"
            + "".join(f"Requires-Dist: {requirement}\n" for requirement in requirements)
        )
        archive.writestr(f"{dist_info}/METADATA", metadata)
        archive.writestr(f"{dist_info}/RECORD", "")
        archive.writestr(
            f"{dist_info}/entry_points.txt",
            "[console_scripts]\nmcp-gateway = mcp_gateway.__main__:main\n",
        )
        archive.writestr(f"{dist_info}/licenses/LICENSE", "MIT\n")
    return output / wheel_name


def _tar_member(name: str, content: str) -> tuple[tarfile.TarInfo, io.BytesIO]:
    encoded = content.encode()
    member = tarfile.TarInfo(name)
    member.size = len(encoded)
    return member, io.BytesIO(encoded)


def _write_sdist(
    output: Path,
    version: str = VERSION,
    metadata_version: str | None = None,
    extra_member: str | None = None,
    requirements: tuple[str, ...] = (),
) -> Path:
    _, sdist_name, _ = contract.expected_artifact_names(version)
    root = f"mcp_local_gateway-{version}"
    entries = {
        ".gitignore": "*\n",
        "LICENSE": "MIT\n",
        "README.md": "# gateway\n",
        "pyproject.toml": "[project]\n",
        "PKG-INFO": "Metadata-Version: 2.4\nName: mcp-local-gateway\n"
        f"Version: {metadata_version or version}\n"
        + "".join(f"Requires-Dist: {requirement}\n" for requirement in requirements),
        "src/mcp_gateway/__init__.py": "",
        "src/mcp_gateway/admin.html": "<html></html>",
        "src/mcp_gateway/config.default.toml": "[gateway]\n",
    }
    if extra_member:
        entries[extra_member] = "not a release input\n"
    with tarfile.open(output / sdist_name, "w:gz") as archive:
        for relative, content in entries.items():
            member, content_file = _tar_member(f"{root}/{relative}", content)
            archive.addfile(member, content_file)
    return output / sdist_name


def _write_valid_artifacts(output: Path, version: str = VERSION) -> tuple[Path, Path]:
    output.mkdir(exist_ok=True)
    return _write_wheel(output, version), _write_sdist(output, version)


@pytest.mark.parametrize(
    "title",
    [
        "feat: add a release contract",
        "fix(parser)!: reject unsafe title input",
        "chore(main): release 1.1.0",
    ],
)
def test_safe_conventional_pr_titles_are_accepted(title: str) -> None:
    assert contract.validate_pr_title(title) is not None


@pytest.mark.parametrize(
    "title",
    [
        "Feat: uppercase types are unsafe",
        "fix(scope):",
        "fix(scope): trailing space ",
        "fix: injected\ncheck: title",
        "fix(scope):no space after colon",
    ],
)
def test_unsafe_or_invalid_pr_titles_are_rejected(title: str) -> None:
    assert contract.validate_pr_title(title) is None


def test_pr_title_cli_and_versions_cli_are_directly_exercised() -> None:
    title = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "pr-title",
            "--title",
            "chore(main): release 1.1.0",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert title.returncode == 0, title.stderr

    invalid = subprocess.run(
        [sys.executable, str(TOOL), "pr-title", "--title", "fix: unsafe\ntitle"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode == 2

    canonical_version = contract.project_version(PROJECT_ROOT)
    versions = subprocess.run(
        [sys.executable, str(TOOL), "versions", "--tag", f"v{canonical_version}"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert versions.returncode == 0, versions.stderr
    assert versions.stdout.strip() == canonical_version


def test_version_contract_requires_project_lock_changelog_and_tag_to_agree(
    tmp_path: Path,
) -> None:
    root = _release_root(tmp_path, directory="unique-workspace-root")
    assert contract.validate_versions(root, "v1.0.0") == VERSION

    (root / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "mcp-local-gateway"\nversion = "1.0.1"\n',
        encoding="utf-8",
    )
    with pytest.raises(contract.ContractError, match="disagree"):
        contract.validate_versions(root)

    with pytest.raises(contract.InputError, match="exactly vX.Y.Z"):
        contract.validate_versions(root, "release-1.0.0")


def test_version_contract_rejects_nonstable_and_nonunique_workspace_versions(
    tmp_path: Path,
) -> None:
    root = _release_root(tmp_path, "1.0.0-rc.1")
    with pytest.raises(contract.ContractError, match="strict stable SemVer"):
        contract.validate_versions(root)

    root = _release_root(tmp_path, directory="unique-lock-root")
    with (root / "uv.lock").open("a", encoding="utf-8") as lock:
        lock.write('[[package]]\nname = "mcp-local-gateway"\nversion = "1.0.0"\n')
    with pytest.raises(contract.ContractError, match="exactly one"):
        contract.validate_versions(root)


def test_first_dated_changelog_heading_is_part_of_the_version_contract(
    tmp_path: Path,
) -> None:
    root = _release_root(tmp_path)
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [1.0.1] - 2026-07-13\n"
        "\n## [1.0.0] - 2026-07-12\n",
        encoding="utf-8",
    )
    with pytest.raises(contract.ContractError, match="disagree"):
        contract.validate_versions(root)


def test_release_please_linked_changelog_heading_is_the_current_release(
    tmp_path: Path,
) -> None:
    root = _release_root(tmp_path, "1.1.0")
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n"
        "## [1.1.0](https://github.com/voidfreud/mcp-gateway/compare/v1.0.0...v1.1.0) "
        "(2026-07-19)\n\n## [1.0.0] - 2026-07-12\n",
        encoding="utf-8",
    )
    assert contract.validate_versions(root) == "1.1.0"


def test_next_version_is_only_a_pure_policy_classifier() -> None:
    assert contract.next_version("1.0.0", "feat: add release automation") == "1.1.0"
    assert contract.next_version("1.0.0", "fix: correct a typo") == "1.0.1"
    assert contract.next_version("1.0.0", "feat!: remove compatibility") == "2.0.0"
    assert contract.next_version("1.0.0", "docs: clarify deployment") is None


def test_archive_allowlists_and_metadata_are_enforced(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    wheel, sdist = _write_valid_artifacts(output)
    assert contract.validate_built_artifacts(output, VERSION) == (wheel, sdist)

    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("docs/private-notes.md", "must not ship")
    with pytest.raises(contract.ContractError, match="wheel contents invalid"):
        contract.validate_built_artifacts(output, VERSION)

    output = tmp_path / "metadata-dist"
    output.mkdir()
    _write_wheel(output, metadata_version="1.0.1")
    _write_sdist(output)
    with pytest.raises(contract.ContractError, match="unexpected Version"):
        contract.validate_built_artifacts(output, VERSION)

    output = tmp_path / "sdist-dist"
    output.mkdir()
    _write_wheel(output)
    _write_sdist(output, extra_member="docs/stale.md")
    with pytest.raises(contract.ContractError, match="sdist contents invalid"):
        contract.validate_built_artifacts(output, VERSION)


def test_checksum_file_is_sorted_and_excludes_itself(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    output.mkdir()
    assets = []
    for name, content in (("z.tar.gz", b"z"), ("a.whl", b"a"), ("m.cdx.json", b"m")):
        asset = output / name
        asset.write_bytes(content)
        assets.append(asset)
    checksums = contract.write_checksums(output, tuple(reversed(assets)))
    lines = checksums.read_text(encoding="utf-8").splitlines()
    assert [line.rsplit("  ", 1)[1] for line in lines] == [
        "a.whl",
        "m.cdx.json",
        "z.tar.gz",
    ]
    assert all("SHA256SUMS" not in line for line in lines)


def test_clean_install_receipt_is_offline_no_deps_and_checks_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _release_root(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "mcp-local-gateway"\nversion = "1.0.0"\n'
        'dependencies = ["fastmcp==3.4.4"]\n',
        encoding="utf-8",
    )
    output = root / "dist"
    output.mkdir()
    wheel = _write_wheel(output, requirements=("fastmcp==3.4.4",))
    commands: list[list[str]] = []
    entry_points = {"console_scripts": [["mcp-gateway", "mcp_gateway.__main__:main"]]}
    create_console_script = True

    def fake_run(command, *, cwd):
        commands.append(list(command))
        if command[1:3] == ["-m", "venv"]:
            scripts = Path(command[-1]) / "bin"
            scripts.mkdir(parents=True)
            (scripts / "python").touch()
            if create_console_script:
                (scripts / "mcp-gateway").touch()
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1:3] == ["pip", "install"]:
            assert "--offline" in command
            assert "--no-deps" in command
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1:4] == ["-m", "mcp_gateway", "--version"]:
            return subprocess.CompletedProcess(command, 0, "mcp-gateway 1.0.0\n", "")
        if "-c" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(entry_points),
                "",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(contract, "_run_checked", fake_run)
    contract.verify_clean_install(wheel, VERSION, root=root, uv="uv")
    assert not any(command[0].endswith("mcp-gateway") for command in commands)

    entry_points["console_scripts"] = [["mcp-gateway", "wrong.module:main"]]
    with pytest.raises(contract.ContractError, match="console entry point"):
        contract.verify_clean_install(wheel, VERSION, root=root, uv="uv")

    entry_points["console_scripts"] = [["mcp-gateway", "mcp_gateway.__main__:main"]]
    create_console_script = False
    with pytest.raises(contract.ContractError, match="did not create"):
        contract.verify_clean_install(wheel, VERSION, root=root, uv="uv")


def test_clean_install_receipt_rejects_missing_or_drifted_runtime_metadata(
    tmp_path: Path,
) -> None:
    root = _release_root(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "mcp-local-gateway"\nversion = "1.0.0"\n'
        'dependencies = ["fastmcp==3.4.4"]\n',
        encoding="utf-8",
    )
    output = root / "dist"
    output.mkdir()
    wheel = _write_wheel(output, requirements=("structlog>=26.1.0",))
    with pytest.raises(contract.ContractError, match="Requires-Dist"):
        contract.verify_clean_install(wheel, VERSION, root=root, uv="uv")


def test_wheel_and_sdist_requirements_must_match_project_metadata(
    tmp_path: Path,
) -> None:
    root = _release_root(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "mcp-local-gateway"\nversion = "1.0.0"\n'
        'dependencies = ["fastmcp==3.4.4"]\n',
        encoding="utf-8",
    )
    output = root / "dist"
    output.mkdir()
    wheel = _write_wheel(output, requirements=("fastmcp==3.4.4",))
    sdist = _write_sdist(output, requirements=("fastmcp==3.4.4",))
    contract.validate_runtime_metadata(root, wheel, sdist, VERSION)

    _write_sdist(output, requirements=("structlog>=26.1.0",))
    with pytest.raises(contract.ContractError, match="sdist Requires-Dist"):
        contract.validate_runtime_metadata(root, wheel, sdist, VERSION)


def test_sbom_export_requires_supported_uv_and_validates_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "dist"
    output.mkdir()

    def fake_run(command, *, cwd):
        if command[1:] == ["export", "--help"]:
            return subprocess.CompletedProcess(command, 0, "cyclonedx1.5", "")
        destination = Path(command[-1])
        destination.write_text(
            '{"bomFormat": "CycloneDX", "specVersion": "1.5"}', encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(contract, "_run_checked", fake_run)
    sbom = contract.export_sbom(output, VERSION, root=tmp_path, uv="uv")
    assert sbom.name == "mcp_local_gateway-1.0.0.cdx.json"

    monkeypatch.setattr(
        contract,
        "_run_checked",
        lambda command, *, cwd: subprocess.CompletedProcess(command, 0, "", ""),
    )
    with pytest.raises(contract.ContractError, match="blocker"):
        contract.export_sbom(output, VERSION, root=tmp_path, uv="uv")


def test_build_contract_has_one_offline_build_and_writes_release_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _release_root(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command, *, cwd):
        calls.append(list(command))
        if command[1:4] == ["build", "--offline", "--out-dir"]:
            _write_valid_artifacts(Path(command[4]))
        return subprocess.CompletedProcess(command, 0, "", "")

    def fake_sbom(output, version, *, root, uv):
        path = output / contract.expected_artifact_names(version)[2]
        path.write_text(
            '{"bomFormat": "CycloneDX", "specVersion": "1.5"}', encoding="utf-8"
        )
        return path

    monkeypatch.setattr(contract, "_uv_executable", lambda: "uv")
    monkeypatch.setattr(contract, "_run_checked", fake_run)
    monkeypatch.setattr(contract, "validate_runtime_metadata", lambda *args: None)
    monkeypatch.setattr(contract, "verify_clean_install", lambda *args, **kwargs: None)
    monkeypatch.setattr(contract, "export_sbom", fake_sbom)

    artifacts = contract.build_release(root, "dist", "v1.0.0")
    assert [command[1] for command in calls].count("build") == 1
    assert [asset.name for asset in artifacts.publishable] == [
        "mcp_local_gateway-1.0.0-py3-none-any.whl",
        "mcp_local_gateway-1.0.0.cdx.json",
        "mcp_local_gateway-1.0.0.tar.gz",
    ]
    assert artifacts.checksums.read_text(encoding="utf-8")


def test_output_directory_cannot_delete_checkout_or_escape_via_symlink(
    tmp_path: Path,
) -> None:
    root = _release_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("do not delete", encoding="utf-8")
    escape = root / "escape"
    escape.symlink_to(outside, target_is_directory=True)

    with pytest.raises(contract.InputError, match="symbolic link"):
        contract.prepare_output_directory(root, "escape")
    source_file = root / "src" / "keep.py"
    source_file.parent.mkdir()
    source_file.write_text("keep", encoding="utf-8")
    with pytest.raises(contract.InputError, match="inside dist"):
        contract.prepare_output_directory(root, ".")
    with pytest.raises(contract.InputError, match="inside dist"):
        contract.prepare_output_directory(root, "src")
    assert sentinel.read_text(encoding="utf-8") == "do not delete"
    assert source_file.read_text(encoding="utf-8") == "keep"
