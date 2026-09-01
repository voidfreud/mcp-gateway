"""Regression checks for repository-only release hygiene."""

from __future__ import annotations

import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

LOCAL_ONLY_PATH_COMPONENTS = {
    ".gitnexus",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "htmlcov",
    "node_modules",
}


def test_tracked_tree_excludes_local_only_release_material() -> None:
    """GitHub review material must not contain local tooling, caches, or secrets."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    forbidden_exact = {".coverage", ".env", "secrets.env", "config.secret.toml"}
    for relative_path in result.stdout.split("\0"):
        if not relative_path:
            continue
        path = Path(relative_path)
        assert not (set(path.parts) & LOCAL_ONLY_PATH_COMPONENTS), relative_path
        assert path.name not in forbidden_exact, relative_path
        assert not path.name.startswith(".env."), relative_path


def test_tracked_deploy_material_has_no_personal_account_paths() -> None:
    """Deployable templates must not embed a contributor's home or account."""
    result = subprocess.run(
        ["git", "ls-files", "--", "deploy"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    for relative_path in result.stdout.splitlines():
        deploy_file = PROJECT_ROOT / relative_path
        if not deploy_file.is_file():
            continue
        content = deploy_file.read_text()
        assert "/Users/" not in content
        assert "alexanderbass" not in content.casefold()


def test_local_secret_and_agent_state_paths_are_ignored() -> None:
    """Sensitive configuration and local agent state must stay out of commits."""
    for local_path in (
        ".env",
        ".env.local",
        "secrets.env",
        "config.secret.toml",
        ".agents/skills/gitnexus-exploring/SKILL.md",
        ".agents/worktrees/example/checkout",
        ".claude/skills/gitnexus-exploring/SKILL.md",
        ".claude/worktrees/example/checkout",
        ".claude/hooks/pre-commit",
        ".claude/cache/index.json",
        ".gitnexus/cache/index.json",
        ".mypy_cache/3.12/cache.json",
        ".pytest_cache/v/cache/nodeids",
        ".ruff_cache/0.15/cache",
        "state/gateway.log",
        "tests/conformance/node_modules/package/index.js",
    ):
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", "--", local_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{local_path} is not ignored"
