"""Focused tests for the repository-local Markdown link checker."""

from __future__ import annotations

from pathlib import Path

from tools import docs_links


def test_check_links_accepts_relative_file_and_fragment(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "README.md").write_text("[Guide](docs/guide.md#first-step)\n")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# First step\n")
    monkeypatch.setattr(
        docs_links, "tracked_markdown", lambda root: [tmp_path / "README.md"]
    )

    assert docs_links.check_links(tmp_path) == []


def test_check_links_reports_missing_file_and_fragment(
    tmp_path: Path, monkeypatch
) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("[Missing](missing.md)\n[Bad fragment](guide.md#nope)\n")
    (tmp_path / "guide.md").write_text("# Present\n")
    monkeypatch.setattr(docs_links, "tracked_markdown", lambda root: [readme])

    assert docs_links.check_links(tmp_path) == [
        "README.md: missing local link missing.md",
        "README.md: missing fragment #nope in guide.md",
    ]


def test_local_target_ignores_external_and_outside_links(tmp_path: Path) -> None:
    source = tmp_path / "README.md"
    source.write_text("")

    assert docs_links.local_target(source, "https://example.com", tmp_path) is None
    assert docs_links.local_target(source, "../outside.md", tmp_path) is None


def test_slug_preserves_adjacent_heading_separators() -> None:
    assert docs_links.slug("Behavior hooks (`validate` / `post_process`)") == (
        "behavior-hooks-validate--post_process"
    )
