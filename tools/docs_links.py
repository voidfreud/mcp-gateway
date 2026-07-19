"""Validate tracked Markdown links that resolve inside this repository."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\((?P<target>[^)\s]+)(?:\s+[^)]*)?\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
PUNCTUATION = re.compile(r"[^\w\- ]")


def tracked_markdown(root: Path) -> list[Path]:
    """Return tracked Markdown files, avoiding generated or untracked input."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git ls-files failed")
    return [root / item for item in result.stdout.split("\0") if item]


def slug(heading: str) -> str:
    """Match GitHub's simple ASCII heading-fragment form used by these docs."""
    text = PUNCTUATION.sub("", heading.casefold())
    return re.sub(r"\s", "-", text.strip())


def fragments(path: Path) -> set[str]:
    """Return heading fragments from one Markdown file."""
    return {
        slug(match.group(1))
        for line in path.read_text(encoding="utf-8").splitlines()
        if (match := HEADING.match(line))
    }


def local_target(source: Path, target: str, root: Path) -> tuple[Path, str] | None:
    """Resolve a repository-relative Markdown target, or ignore an external URL."""
    target = unquote(target.strip("<>"))
    if not target or "://" in target or target.startswith(("mailto:", "tel:")):
        return None
    path_text, separator, fragment = target.partition("#")
    destination = source if not path_text else (source.parent / path_text).resolve()
    try:
        destination.relative_to(root.resolve())
    except ValueError:
        return None
    return destination, fragment if separator else ""


def check_links(root: Path) -> list[str]:
    """Return human-readable errors for bad tracked local Markdown links."""
    errors: list[str] = []
    for source in tracked_markdown(root):
        for match in MARKDOWN_LINK.finditer(source.read_text(encoding="utf-8")):
            target = local_target(source, match.group("target"), root)
            if target is None:
                continue
            destination, fragment = target
            display = source.relative_to(root)
            if not destination.exists():
                errors.append(f"{display}: missing local link {match.group('target')}")
                continue
            if fragment and (
                not destination.is_file() or fragment not in fragments(destination)
            ):
                errors.append(
                    f"{display}: missing fragment #{fragment} in "
                    f"{destination.relative_to(root)}"
                )
    return errors


def main() -> int:
    """Print all failures so one run fixes the complete local-link set."""
    errors = check_links(PROJECT_ROOT)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("tracked Markdown local links OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
