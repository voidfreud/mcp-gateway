#!/usr/bin/env python3
"""Verify the repository's local, non-publishing release contract.

This tool deliberately owns the expensive release-artifact build used by the
normal gate.  It never creates a tag, uploads an artifact, or contacts a
release service.  Network access is disabled for the build and clean-install
checks so a passing result is a receipt for the locked local environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "mcp-local-gateway"
# PEP 503/427 normalized distribution stem used by wheel, sdist, dist-info,
# and CycloneDX filenames. The import package (PACKAGE_PATH) keeps its own
# underscore name; the console command keeps its hyphenated script name.
DISTRIBUTION_NAME = "mcp_local_gateway"
PACKAGE_PATH = "mcp_gateway"
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
CHANGELOG_HEADING_RE = re.compile(
    r"^## \[(?P<version>[^\]]+)\](?:\([^)]*\))? "
    r"(?:- (?P<legacy_date>\d{4}-\d{2}-\d{2})|\((?P<rp_date>\d{4}-\d{2}-\d{2})\))$"
)
PR_TITLE_RE = re.compile(
    r"(?P<type>[a-z][a-z0-9-]*)(?:\((?P<scope>[a-z0-9][a-z0-9._/-]*)\))?"
    r"(?P<breaking>!)?: (?P<subject>\S(?:[^\r\n]*\S)?)"
)


class ContractError(RuntimeError):
    """A repository contract failed."""


class InputError(ContractError):
    """The caller supplied an unsafe or malformed CLI value."""


@dataclass(frozen=True)
class ReleaseArtifacts:
    """The publishable assets produced by one verified build."""

    wheel: Path
    sdist: Path
    sbom: Path
    checksums: Path

    @property
    def publishable(self) -> tuple[Path, Path, Path]:
        """Assets covered by SHA256SUMS, in deterministic name order."""
        return tuple(
            sorted((self.wheel, self.sdist, self.sbom), key=lambda path: path.name)
        )


def is_stable_semver(value: str) -> bool:
    """Return whether ``value`` is a strict, stable Semantic Version."""
    return bool(SEMVER_RE.fullmatch(value))


def _require_stable_semver(value: object, source: str) -> str:
    if not isinstance(value, str) or not is_stable_semver(value):
        raise ContractError(f"{source} must be strict stable SemVer, got {value!r}")
    return value


def validate_pr_title(title: str) -> re.Match[str] | None:
    """Accept a safe lowercase Conventional Commit-style pull-request title."""
    if not title.isprintable():
        return None
    return PR_TITLE_RE.fullmatch(title)


def next_version(baseline: str, title: str) -> str | None:
    """Classify one PR title for policy tests; this is not a release driver."""
    baseline = _require_stable_semver(baseline, "baseline")
    match = validate_pr_title(title)
    if match is None:
        raise InputError("title must use safe lowercase Conventional PR syntax")
    major, minor, patch = (int(part) for part in baseline.split("."))
    if match.group("breaking"):
        return f"{major + 1}.0.0"
    if match.group("type") == "feat":
        return f"{major}.{minor + 1}.0"
    if match.group("type") == "fix":
        return f"{major}.{minor}.{patch + 1}"
    return None


def _read_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ContractError(f"cannot read {path}: {exc}") from exc


def project_version(root: Path) -> str:
    """Read and validate the release version in pyproject.toml."""
    data = _read_toml(root / "pyproject.toml")
    project = data.get("project")
    if not isinstance(project, dict):
        raise ContractError("pyproject.toml is missing [project]")
    if project.get("name") != PACKAGE_NAME:
        raise ContractError(f"pyproject.toml [project].name must be {PACKAGE_NAME!r}")
    return _require_stable_semver(
        project.get("version"), "pyproject.toml [project].version"
    )


def lock_version(root: Path) -> str:
    """Read the one workspace package version from uv.lock."""
    data = _read_toml(root / "uv.lock")
    packages = data.get("package")
    if not isinstance(packages, list):
        raise ContractError("uv.lock is missing [[package]] entries")
    matches = [
        package
        for package in packages
        if isinstance(package, dict) and package.get("name") == PACKAGE_NAME
    ]
    if len(matches) != 1:
        raise ContractError(
            "uv.lock must contain exactly one "
            f"{PACKAGE_NAME!r} workspace package, found {len(matches)}"
        )
    return _require_stable_semver(matches[0].get("version"), "uv.lock package version")


def changelog_version(root: Path) -> str:
    """Return the first legacy or Release Please dated heading from CHANGELOG.md."""
    path = root / "CHANGELOG.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ContractError(f"cannot read {path}: {exc}") from exc
    for line in lines:
        match = CHANGELOG_HEADING_RE.fullmatch(line)
        if match:
            return _require_stable_semver(
                match.group("version"), "first dated CHANGELOG.md release heading"
            )
    raise ContractError("CHANGELOG.md has no dated release heading")


def parse_tag(tag: str) -> str:
    """Return a stable version from a release tag, rejecting tag expressions."""
    if not tag.startswith("v"):
        raise InputError("tag must be exactly vX.Y.Z")
    version = tag.removeprefix("v")
    if not is_stable_semver(version):
        raise InputError("tag must be exactly vX.Y.Z with stable SemVer")
    return version


def validate_versions(root: Path, tag: str | None = None) -> str:
    """Require pyproject, uv.lock, CHANGELOG, and an optional tag to agree."""
    tag_version = parse_tag(tag) if tag is not None else None
    versions = {
        "pyproject.toml": project_version(root),
        "uv.lock": lock_version(root),
        "CHANGELOG.md": changelog_version(root),
    }
    if len(set(versions.values())) != 1:
        rendered = ", ".join(f"{source}={value}" for source, value in versions.items())
        raise ContractError(f"release versions disagree: {rendered}")
    version = next(iter(versions.values()))
    if tag_version is not None and tag_version != version:
        raise ContractError(f"tag {tag!r} does not match release version v{version}")
    return version


def _output_directory(root: Path, raw_output: str) -> Path:
    """Resolve a build directory confined to the dedicated ``dist/`` tree."""
    requested = Path(raw_output)
    candidate = requested if requested.is_absolute() else root / requested
    if candidate.is_symlink():
        raise InputError("--out-dir must not be a symbolic link")
    dist_root = root / "dist"
    if dist_root.is_symlink():
        raise InputError("checkout dist directory must not be a symbolic link")
    resolved = candidate.resolve(strict=False)
    resolved_dist = dist_root.resolve(strict=False)
    if resolved != resolved_dist and resolved_dist not in resolved.parents:
        raise InputError("--out-dir must be dist or a directory inside dist")
    if candidate.exists() and not candidate.is_dir():
        raise InputError("--out-dir must name a directory")
    return resolved


def prepare_output_directory(root: Path, raw_output: str) -> Path:
    """Remove only a validated in-checkout output directory, then recreate it."""
    output = _output_directory(root.resolve(), raw_output)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=False)
    return output


def _run_checked(
    command: Sequence[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(command), cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        raise ContractError(f"command failed ({' '.join(command)}): {details}")
    return result


def _uv_executable() -> str:
    uv = shutil.which("uv")
    if uv is None:
        raise ContractError("uv is required for the release contract")
    return uv


def expected_artifact_names(version: str) -> tuple[str, str, str]:
    """Return wheel, sdist, and CycloneDX SBOM filenames for ``version``."""
    _require_stable_semver(version, "release version")
    return (
        f"{DISTRIBUTION_NAME}-{version}-py3-none-any.whl",
        f"{DISTRIBUTION_NAME}-{version}.tar.gz",
        f"{DISTRIBUTION_NAME}-{version}.cdx.json",
    )


def _metadata_from_bytes(content: bytes, source: str) -> object:
    try:
        return BytesParser().parsebytes(content)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"invalid package metadata in {source}: {exc}") from exc


def _assert_metadata(content: bytes, source: str, version: str) -> None:
    metadata = _metadata_from_bytes(content, source)
    if metadata.get("Name") != PACKAGE_NAME:
        raise ContractError(f"{source} has unexpected Name {metadata.get('Name')!r}")
    if metadata.get("Version") != version:
        raise ContractError(
            f"{source} has unexpected Version {metadata.get('Version')!r}"
        )


def _project_requirements(root: Path) -> tuple[str, ...]:
    """Read the runtime requirements that release metadata must declare."""
    data = _read_toml(root / "pyproject.toml")
    project = data.get("project")
    if not isinstance(project, dict):
        raise ContractError("pyproject.toml is missing [project]")
    requirements = project.get("dependencies")
    if not isinstance(requirements, list) or not requirements:
        raise ContractError("pyproject.toml [project].dependencies must not be empty")
    if not all(isinstance(requirement, str) for requirement in requirements):
        raise ContractError("pyproject.toml [project].dependencies must be strings")
    return tuple(sorted(requirements))


def _wheel_requirements(wheel: Path, version: str) -> tuple[str, ...]:
    """Read the declared runtime requirements from a built wheel's metadata."""
    metadata_path = f"{DISTRIBUTION_NAME}-{version}.dist-info/METADATA"
    try:
        with zipfile.ZipFile(wheel) as archive:
            content = archive.read(metadata_path)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ContractError(
            f"cannot inspect wheel requirements in {wheel}: {exc}"
        ) from exc
    _assert_metadata(content, f"{wheel.name} METADATA", version)
    metadata = _metadata_from_bytes(content, f"{wheel.name} METADATA")
    requirements = metadata.get_all("Requires-Dist") or []
    if not requirements or not all(
        isinstance(requirement, str) for requirement in requirements
    ):
        raise ContractError(
            f"{wheel.name} METADATA must declare runtime Requires-Dist entries"
        )
    return tuple(sorted(requirements))


def _sdist_requirements(sdist: Path, version: str) -> tuple[str, ...]:
    """Read the declared runtime requirements from a built sdist's PKG-INFO."""
    metadata_path = f"{DISTRIBUTION_NAME}-{version}/PKG-INFO"
    try:
        with tarfile.open(sdist) as archive:
            extracted = archive.extractfile(metadata_path)
            if extracted is None:
                raise ContractError(f"{sdist.name} is missing {metadata_path}")
            content = extracted.read()
    except (OSError, tarfile.TarError) as exc:
        raise ContractError(
            f"cannot inspect sdist requirements in {sdist}: {exc}"
        ) from exc
    _assert_metadata(content, f"{sdist.name} PKG-INFO", version)
    metadata = _metadata_from_bytes(content, f"{sdist.name} PKG-INFO")
    requirements = metadata.get_all("Requires-Dist") or []
    if not requirements or not all(
        isinstance(requirement, str) for requirement in requirements
    ):
        raise ContractError(
            f"{sdist.name} PKG-INFO must declare runtime Requires-Dist entries"
        )
    return tuple(sorted(requirements))


def validate_runtime_metadata(
    root: Path, wheel: Path, sdist: Path, version: str
) -> None:
    """Require wheel and sdist dependency metadata to match project runtime deps."""
    source_requirements = _project_requirements(root)
    if _wheel_requirements(wheel, version) != source_requirements:
        raise ContractError(
            "wheel Requires-Dist does not match pyproject.toml runtime dependencies"
        )
    if _sdist_requirements(sdist, version) != source_requirements:
        raise ContractError(
            "sdist Requires-Dist does not match pyproject.toml runtime dependencies"
        )


def _validate_wheel(path: Path, version: str) -> None:
    dist_info = f"{DISTRIBUTION_NAME}-{version}.dist-info/"
    required = {
        f"{PACKAGE_PATH}/admin.html",
        f"{PACKAGE_PATH}/config.default.toml",
        f"{dist_info}METADATA",
        f"{dist_info}RECORD",
        f"{dist_info}entry_points.txt",
        f"{dist_info}licenses/LICENSE",
    }
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.namelist()
            unexpected = [
                member
                for member in members
                if not (
                    member.startswith(f"{PACKAGE_PATH}/")
                    or member.startswith(dist_info)
                )
            ]
            missing = sorted(required.difference(members))
            if unexpected or missing:
                raise ContractError(
                    "wheel contents invalid; "
                    f"unexpected={unexpected}, missing={missing}"
                )
            if any(
                "/__pycache__/" in member or member.endswith(".pyc")
                for member in members
            ):
                raise ContractError("wheel contains generated Python bytecode")
            _assert_metadata(
                archive.read(f"{dist_info}METADATA"), f"{path.name} METADATA", version
            )
    except (OSError, zipfile.BadZipFile) as exc:
        raise ContractError(f"cannot inspect wheel {path}: {exc}") from exc


def _validate_sdist(path: Path, version: str) -> None:
    root = f"{DISTRIBUTION_NAME}-{version}/"
    allowed_top_level = {
        ".gitignore",
        "LICENSE",
        "PKG-INFO",
        "README.md",
        "pyproject.toml",
    }
    required = {
        f"{root}LICENSE",
        f"{root}README.md",
        f"{root}src/{PACKAGE_PATH}/admin.html",
        f"{root}src/{PACKAGE_PATH}/config.default.toml",
        f"{root}PKG-INFO",
    }
    try:
        with tarfile.open(path) as archive:
            members = [
                member.name for member in archive.getmembers() if member.isfile()
            ]
            unexpected = []
            for member in members:
                if not member.startswith(root):
                    unexpected.append(member)
                    continue
                relative = member.removeprefix(root)
                if relative in allowed_top_level or relative.startswith(
                    f"src/{PACKAGE_PATH}/"
                ):
                    continue
                unexpected.append(member)
            missing = sorted(required.difference(members))
            if unexpected or missing:
                raise ContractError(
                    "sdist contents invalid; "
                    f"unexpected={unexpected}, missing={missing}"
                )
            _assert_metadata(
                archive.extractfile(f"{root}PKG-INFO").read(),
                f"{path.name} PKG-INFO",
                version,
            )
    except (OSError, tarfile.TarError, AttributeError) as exc:
        raise ContractError(f"cannot inspect sdist {path}: {exc}") from exc


def validate_built_artifacts(output: Path, version: str) -> tuple[Path, Path]:
    """Require exactly the expected wheel and sdist before release assets exist."""
    wheel_name, sdist_name, _ = expected_artifact_names(version)
    wheel = output / wheel_name
    sdist = output / sdist_name
    files = sorted(path.name for path in output.iterdir() if path.is_file())
    unexpected = set(files).difference({wheel_name, sdist_name, ".gitignore"})
    if not wheel.is_file() or not sdist.is_file() or unexpected:
        raise ContractError(
            f"build must produce exactly one expected wheel and sdist; found={files}"
        )
    _validate_wheel(wheel, version)
    _validate_sdist(sdist, version)
    return wheel, sdist


def _venv_paths(environment: Path) -> tuple[Path, Path]:
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    console = scripts / ("mcp-gateway.exe" if os.name == "nt" else "mcp-gateway")
    return python, console


def verify_clean_install(wheel: Path, version: str, *, root: Path, uv: str) -> None:
    """Receipt a dependency-free offline wheel install and its declared launcher."""
    source_requirements = _project_requirements(root)
    wheel_requirements = _wheel_requirements(wheel, version)
    if wheel_requirements != source_requirements:
        raise ContractError(
            "wheel Requires-Dist does not match pyproject.toml runtime dependencies"
        )
    with tempfile.TemporaryDirectory(prefix="mcp-gateway-release-") as temporary:
        environment = Path(temporary) / "venv"
        _run_checked([sys.executable, "-m", "venv", str(environment)], cwd=root)
        python, console = _venv_paths(environment)
        _run_checked(
            [
                uv,
                "pip",
                "install",
                "--offline",
                "--no-deps",
                "--python",
                str(python),
                str(wheel),
            ],
            cwd=root,
        )
        expected_module = f"mcp-gateway {version}\n"
        module = _run_checked([str(python), "-m", PACKAGE_PATH, "--version"], cwd=root)
        if module.stdout != expected_module:
            raise ContractError(
                "module --version mismatch: "
                f"expected {expected_module!r}, got {module.stdout!r}"
            )
        if not console.is_file():
            raise ContractError(
                "clean wheel install did not create the mcp-gateway console script"
            )
        entry_points = _run_checked(
            [
                str(python),
                "-c",
                "import configparser, json; "
                "from importlib.metadata import distribution; "
                "text = distribution('mcp-local-gateway').read_text("
                "'entry_points.txt') or ''; "
                "parser = configparser.ConfigParser(); parser.read_string(text); "
                "print(json.dumps({section: sorted(parser[section].items()) "
                "for section in parser.sections()}))",
            ],
            cwd=root,
        )
        try:
            installed_entry_points = json.loads(entry_points.stdout)
        except json.JSONDecodeError as exc:
            raise ContractError(
                f"installed entry-point metadata was not JSON: {exc}"
            ) from exc
        if installed_entry_points != {
            "console_scripts": [["mcp-gateway", "mcp_gateway.__main__:main"]]
        }:
            raise ContractError(
                "installed console entry point must be exactly "
                "mcp-gateway = mcp_gateway.__main__:main"
            )


def export_sbom(output: Path, version: str, *, root: Path, uv: str) -> Path:
    """Export a locked CycloneDX SBOM when this installed uv supports it."""
    support = _run_checked([uv, "export", "--help"], cwd=root)
    if "cyclonedx1.5" not in support.stdout:
        raise ContractError(
            "blocker: installed uv does not support --format cyclonedx1.5; "
            "upgrade uv rather than fabricating an SBOM"
        )
    _, _, sbom_name = expected_artifact_names(version)
    sbom = output / sbom_name
    _run_checked(
        [
            uv,
            "export",
            "--locked",
            "--no-dev",
            "--format",
            "cyclonedx1.5",
            "--output-file",
            str(sbom),
        ],
        cwd=root,
    )
    try:
        payload = json.loads(sbom.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(
            f"CycloneDX export did not produce valid JSON: {exc}"
        ) from exc
    if payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != "1.5":
        raise ContractError("CycloneDX export did not produce a CycloneDX 1.5 SBOM")
    return sbom


def write_checksums(output: Path, assets: Sequence[Path]) -> Path:
    """Write deterministic SHA-256 entries for every publishable release asset."""
    checksums = output / "SHA256SUMS"
    lines = []
    for asset in sorted(assets, key=lambda path: path.name):
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        lines.append(f"{digest}  {asset.name}\n")
    checksums.write_text("".join(lines), encoding="utf-8")
    return checksums


def build_release(
    root: Path, raw_output: str, tag: str | None = None
) -> ReleaseArtifacts:
    """Build, inspect, install-check, SBOM, and checksum one local release."""
    root = root.resolve()
    version = validate_versions(root, tag)
    output = prepare_output_directory(root, raw_output)
    uv = _uv_executable()
    _run_checked(
        [
            uv,
            "build",
            "--offline",
            "--no-build-isolation",
            "--out-dir",
            str(output),
        ],
        cwd=root,
    )
    wheel, sdist = validate_built_artifacts(output, version)
    validate_runtime_metadata(root, wheel, sdist, version)
    verify_clean_install(wheel, version, root=root, uv=uv)
    sbom = export_sbom(output, version, root=root, uv=uv)
    checksums = write_checksums(output, (wheel, sdist, sbom))
    return ReleaseArtifacts(wheel=wheel, sdist=sdist, sbom=sbom, checksums=checksums)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    title = commands.add_parser("pr-title", help="validate a pull-request title")
    title.add_argument("--title", required=True)

    versions = commands.add_parser("versions", help="require release versions to agree")
    versions.add_argument("--tag")

    build = commands.add_parser("build", help="build and validate local release assets")
    build.add_argument("--out-dir", default="dist")
    build.add_argument("--tag")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected local release-contract command."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "pr-title":
            if validate_pr_title(args.title) is None:
                raise InputError("title must use safe lowercase Conventional PR syntax")
            print("valid PR title")
            return 0
        if args.command == "versions":
            print(validate_versions(PROJECT_ROOT, args.tag))
            return 0
        artifacts = build_release(PROJECT_ROOT, args.out_dir, args.tag)
        print("verified release assets:")
        for asset in (*artifacts.publishable, artifacts.checksums):
            print(asset)
        return 0
    except InputError as exc:
        print(f"release-contract input error: {exc}", file=sys.stderr)
        return 2
    except ContractError as exc:
        print(f"release-contract failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
