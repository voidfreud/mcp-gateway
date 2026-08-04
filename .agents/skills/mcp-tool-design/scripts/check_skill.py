#!/usr/bin/env python3
"""Check that the tracked MCP surface-design skill remains portable and lean.

Usage:
    uv run python .agents/skills/mcp-tool-design/scripts/check_skill.py

The check is intentionally stdlib-only.  It derives the repository root from
this file, so it is safe to invoke from any current working directory.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import date
from pathlib import Path

SKILL_ROOT_PARTS = (".agents", "skills", "mcp-tool-design")
_FRONTMATTER_RE = re.compile(r"\A---\n(?P<body>.*?)\n---\n", re.DOTALL)
_FIELD_RE = re.compile(r"^(name|description):\s+\S.*$")
_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
_URL_RE = re.compile(r"https://[^\s)>]+")
_DATE_RE = re.compile(r"\*\*Verified on:\*\*\s*(\d{4}-\d{2}-\d{2})")
_CONFIDENCE_RE = re.compile(r"\*\*Confidence:\*\*\s*(\S.+)")
_OLD_MARKERS = (
    "${CLAUDE_SKILL_DIR}",
    "~/.config",
    "~/.local/state",
    "/Users/",
    "/home/",
)
_CLIENT_BEHAVIOR_RE = re.compile(
    r"(?:claude(?: code)?|codex).{0,80}"
    r"(?:tool search|truncat|512|2\s*kb|eager.?load|ranking|visibility|reload)",
    re.IGNORECASE | re.DOTALL,
)


def project_root() -> Path:
    """Locate the checkout from this script's own path."""
    current = Path(__file__).resolve()
    for candidate in (current.parent, *current.parents):
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "AGENTS.md"
        ).is_file():
            return candidate
    raise RuntimeError("project root not found")


def _line_number(text: str, offset: int = 0) -> int:
    return text.count("\n", 0, offset) + 1


def _display(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _diagnostic(path: Path, line: int, rule: str, message: str, root: Path) -> str:
    return f"{_display(path, root)}:{line}: {rule}: {message}"


def _read(path: Path, root: Path, diagnostics: list[str]) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        diagnostics.append(_diagnostic(path, 1, "read", "must be UTF-8 text", root))
        return ""


def _frontmatter(
    path: Path, root: Path, diagnostics: list[str]
) -> tuple[dict[str, str], str]:
    text = _read(path, root, diagnostics)
    match = _FRONTMATTER_RE.match(text)
    if not match:
        diagnostics.append(
            _diagnostic(
                path, 1, "frontmatter", "must start with closed YAML frontmatter", root
            )
        )
        return {}, text
    fields: dict[str, str] = {}
    for index, line in enumerate(match.group("body").splitlines(), start=2):
        if not _FIELD_RE.fullmatch(line):
            diagnostics.append(
                _diagnostic(
                    path,
                    index,
                    "frontmatter",
                    "must contain scalar name and description fields only",
                    root,
                )
            )
            continue
        key, value = line.split(":", 1)
        if key in fields:
            diagnostics.append(
                _diagnostic(path, index, "frontmatter", "must not repeat a field", root)
            )
        fields[key] = value.strip()
    if set(fields) != {"name", "description"}:
        diagnostics.append(
            _diagnostic(
                path, 1, "frontmatter", "must have exactly name and description", root
            )
        )
    elif not all(fields.values()):
        diagnostics.append(
            _diagnostic(path, 1, "frontmatter", "values must not be empty", root)
        )
    return fields, text


def _within(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _check_links(path: Path, root: Path, diagnostics: list[str]) -> None:
    text = _read(path, root, diagnostics)
    for match in _LINK_RE.finditer(text):
        target = match.group(1).strip().strip("<>")
        if not target or target.startswith("#") or _URL_RE.fullmatch(target):
            continue
        line = _line_number(text, match.start(1))
        target_path = target.partition("#")[0]
        if not target_path or "://" in target_path:
            continue
        resolved = (path.parent / target_path).resolve()
        if Path(target_path).is_absolute() or not _within(root, resolved):
            diagnostics.append(
                _diagnostic(
                    path, line, "local-link", "must stay inside the repository", root
                )
            )
        elif not resolved.exists():
            diagnostics.append(
                _diagnostic(path, line, "local-link", "target does not exist", root)
            )


def inspect_pointer_file(path: Path, root: Path) -> list[str]:
    """Return portable-pointer diagnostics; exposed for small fixture tests."""
    diagnostics: list[str] = []
    text = _read(path, root, diagnostics)
    for marker in _OLD_MARKERS:
        if marker in text:
            diagnostics.append(
                _diagnostic(
                    path,
                    _line_number(text, text.index(marker)),
                    "legacy-marker",
                    "contains a local-only environment or path marker",
                    root,
                )
            )
    if len(text.encode("utf-8")) > 1_024 or text.count("\n") > 12:
        diagnostics.append(
            _diagnostic(
                path,
                1,
                "pointer-content",
                "retained pointers must not contain copied source bodies",
                root,
            )
        )
    _check_links(path, root, diagnostics)
    return diagnostics


def _check_openai_yaml(path: Path, root: Path, diagnostics: list[str]) -> None:
    text = _read(path, root, diagnostics)
    expected = {
        'display_name: "MCP Tool Design"',
        'short_description: "Tune MCP surfaces with client-aware evidence"',
        'default_prompt: "Use $mcp-tool-design to improve an MCP backend\'s advertised surface and validate it for the target client."',
    }
    for value in sorted(expected):
        if value not in text:
            diagnostics.append(
                _diagnostic(
                    path, 1, "openai-yaml", f"missing required value {value!r}", root
                )
            )


def _check_profile(
    path: Path, corpus_text: str, root: Path, diagnostics: list[str]
) -> None:
    text = _read(path, root, diagnostics)
    profile_name = path.stem.replace("-", " ").title()
    if not text.startswith(f"# {profile_name} profile\n"):
        diagnostics.append(
            _diagnostic(
                path, 1, "profile-heading", "must use its profile heading", root
            )
        )
    date_match = _DATE_RE.search(text)
    if not date_match:
        diagnostics.append(
            _diagnostic(path, 1, "profile-date", "missing verified-on date", root)
        )
    else:
        try:
            date.fromisoformat(date_match.group(1))
        except ValueError:
            diagnostics.append(
                _diagnostic(
                    path,
                    _line_number(text, date_match.start(1)),
                    "profile-date",
                    "must be an ISO date",
                    root,
                )
            )
    if not _CONFIDENCE_RE.search(text):
        diagnostics.append(
            _diagnostic(path, 1, "profile-confidence", "missing confidence", root)
        )
    heading = "## Primary sources"
    if heading not in text:
        diagnostics.append(
            _diagnostic(
                path, 1, "profile-sources", "missing primary-sources heading", root
            )
        )
    urls = _URL_RE.findall(text)
    if not urls:
        diagnostics.append(
            _diagnostic(path, 1, "profile-sources", "needs source URLs", root)
        )
    for url in urls:
        if url not in corpus_text:
            diagnostics.append(
                _diagnostic(
                    path,
                    _line_number(text, text.index(url)),
                    "profile-evidence",
                    "source URL is absent from corpus retention",
                    root,
                )
            )


def _check_corpus(root: Path, diagnostics: list[str]) -> str:
    corpus = root / "corpus"
    manifest = corpus / "RETENTION.md"
    text = _read(manifest, root, diagnostics)
    if not text.startswith("# Corpus retention manifest\n"):
        diagnostics.append(
            _diagnostic(manifest, 1, "corpus-manifest", "missing title", root)
        )
    rows = [
        line
        for line in text.splitlines()
        if line.startswith("| ") and not line.startswith("| Stable ID")
    ]
    for line_number, row in enumerate(text.splitlines(), start=1):
        if not row.startswith("| ") or "---" in row or row.startswith("| Stable ID"):
            continue
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if len(cells) != 7 or not all(cells):
            diagnostics.append(
                _diagnostic(
                    manifest,
                    line_number,
                    "corpus-manifest",
                    "row must have seven populated cells",
                    root,
                )
            )
            continue
        if not _URL_RE.search(cells[2]):
            diagnostics.append(
                _diagnostic(
                    manifest,
                    line_number,
                    "corpus-manifest",
                    "row needs a source URL",
                    root,
                )
            )
        consumers = re.findall(r"`([^`]+)`", cells[6])
        if not consumers:
            diagnostics.append(
                _diagnostic(
                    manifest,
                    line_number,
                    "corpus-consumer",
                    "row needs a consumer path",
                    root,
                )
            )
        for consumer in consumers:
            target = root / consumer
            if consumer == "none":
                continue
            if (
                Path(consumer).is_absolute()
                or not _within(root, target)
                or not target.is_file()
            ):
                diagnostics.append(
                    _diagnostic(
                        manifest,
                        line_number,
                        "corpus-consumer",
                        "consumer path does not exist",
                        root,
                    )
                )
    if not rows:
        diagnostics.append(
            _diagnostic(manifest, 1, "corpus-manifest", "needs evidence rows", root)
        )
    extra_files = [
        path
        for path in corpus.rglob("*")
        if path.is_file() and path != manifest and path.name != ".DS_Store"
    ]
    for path in extra_files:
        diagnostics.extend(inspect_pointer_file(path, root))
    return text


def _check_generic_claims(root: Path, diagnostics: list[str]) -> None:
    base = root.joinpath(*SKILL_ROOT_PARTS)
    paths = [base / "SKILL.md", *(base / "references").glob("*.md")]
    for path in paths:
        text = _read(path, root, diagnostics)
        match = _CLIENT_BEHAVIOR_RE.search(text)
        if match:
            diagnostics.append(
                _diagnostic(
                    path,
                    _line_number(text, match.start()),
                    "generic-client-claim",
                    "client behavioral claims belong only in a client profile",
                    root,
                )
            )
        for marker in _OLD_MARKERS:
            if marker in text:
                diagnostics.append(
                    _diagnostic(
                        path,
                        _line_number(text, text.index(marker)),
                        "legacy-marker",
                        "contains a local-only environment or path marker",
                        root,
                    )
                )


def _tracked_files(root: Path, directory: Path) -> set[str] | None:
    """Return tracked files below *directory*, or ``None`` outside a worktree."""
    relative_directory = directory.relative_to(root).as_posix()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--", relative_directory],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode:
        return None
    return {
        Path(line).relative_to(relative_directory).as_posix()
        for line in result.stdout.splitlines()
        if line
    }


def _is_ignored(root: Path, path: Path) -> bool | None:
    """Report Git ignore status, or ``None`` when no worktree is available."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--quiet", "--", str(path)],
            check=False,
            capture_output=True,
        )
    except OSError:
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _check_research_layout(root: Path, diagnostics: list[str]) -> None:
    """Require tracked research scaffolding while permitting ignored local receipts.

    In a Git worktree, the index is the source of truth: extra ignored and
    untracked receipts are intentionally local.  Archive exports have no index,
    so they must contain only the release scaffold; a normal export cannot carry
    ignored worktree receipts.
    """
    research = root.joinpath(*SKILL_ROOT_PARTS, "research")
    expected = {".gitignore", "ABOUT.md"}
    if not research.is_dir():
        found: set[str] = set()
    else:
        tracked = _tracked_files(root, research)
        if tracked is None:
            found = {
                path.relative_to(research).as_posix()
                for path in research.rglob("*")
                if path.is_file() and path.name != ".DS_Store"
            }
        elif tracked:
            found = tracked
        else:
            # While a new skill is still unstaged, retain its required scaffold
            # but disregard local receipts covered by the research .gitignore.
            found = {
                path.relative_to(research).as_posix()
                for path in research.rglob("*")
                if path.is_file()
                and path.name != ".DS_Store"
                and _is_ignored(root, path) is not True
            }
    if found != expected:
        diagnostics.append(
            _diagnostic(
                research,
                1,
                "research-tree",
                "must track only .gitignore and ABOUT.md",
                root,
            )
        )


def _check_layout(root: Path, diagnostics: list[str]) -> None:
    canonical = root.joinpath(*SKILL_ROOT_PARTS)
    skill = canonical / "SKILL.md"
    fields, text = _frontmatter(skill, root, diagnostics)
    if fields.get("name") != "mcp-tool-design":
        diagnostics.append(
            _diagnostic(skill, 2, "frontmatter", "name must match the skill", root)
        )
    if len(text.splitlines()) > 90:
        diagnostics.append(
            _diagnostic(
                skill, 1, "canonical-length", "canonical skill exceeds 90 lines", root
            )
        )
    _check_research_layout(root, diagnostics)
    for path in canonical.rglob("*.md"):
        _check_links(path, root, diagnostics)
    _check_openai_yaml(canonical / "agents/openai.yaml", root, diagnostics)


def check(root: Path) -> list[str]:
    """Return stable diagnostics for the complete tracked skill contract."""
    diagnostics: list[str] = []
    _check_layout(root, diagnostics)
    corpus_text = _check_corpus(root, diagnostics)
    _check_generic_claims(root, diagnostics)
    clients = root.joinpath(*SKILL_ROOT_PARTS, "references", "clients")
    for profile in sorted(clients.glob("*.md")):
        _check_profile(profile, corpus_text, root, diagnostics)
    return sorted(set(diagnostics))


def main(cwd: Path | None = None) -> int:
    """Run without depending on *cwd*; the argument supports direct tests."""
    del cwd
    root = project_root()
    diagnostics = check(root)
    if diagnostics:
        print("\n".join(diagnostics), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
