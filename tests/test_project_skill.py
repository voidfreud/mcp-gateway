"""Offline contracts for the repository's MCP surface-design skill."""

from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SURFACE_PATH = PROJECT_ROOT / ".agents/skills/mcp-tool-design/scripts/surface.py"
CHECK_PATH = PROJECT_ROOT / ".agents/skills/mcp-tool-design/scripts/check_skill.py"


def _module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def surface() -> ModuleType:
    return _module(SURFACE_PATH, "project_skill_surface")


def _write_config(tmp_path: Path, *, disabled: bool = False) -> Path:
    config = tmp_path / "gateway.toml"
    config.write_text(
        "\n".join(
            [
                'host = "127.0.0.1"',
                "",
                "[[backends]]",
                'name = "first"',
                'transport = "http"',
                'url = "https://transport.example/mcp?token=transport-secret"',
                f"enabled = {'false' if disabled else 'true'}",
                'instructions = "Use ${LOCAL_SECRET}; bearer config-secret"',
                "",
                "[[backends.tools]]",
                'original = "search"',
                'name = "find"',
                'description = "Find ${BACKEND_TOKEN} token=tool-secret"',
                "",
                "[[backends.tools.params]]",
                'original = "limit"',
                'description = "Maximum results"',
                "",
                "[[backends.tools.params]]",
                'original = "ghost"',
                'description = "Not in the capture"',
                "",
                "[[backends.resources]]",
                'uri = "https://resource.example/private"',
                'name = "Configured resource"',
                "",
                "[[backends.prompts]]",
                'original = "missing_prompt"',
                'name = "renamed_prompt"',
                "",
                "[[backends]]",
                'name = "second"',
                'transport = "stdio"',
                'command = "never-render-this"',
                'args = ["--secret", "local-secret"]',
                'env = { TOKEN = "local-secret" }',
            ]
        ),
        encoding="utf-8",
    )
    return config


def _write_capture(defaults: Path, backend: str = "first") -> Path:
    defaults.mkdir()
    capture = {
        "backend": backend,
        "captured_at": 1234567890,
        "instructions": "Résumé ✓",
        "server_info": {"name": "server", "version": "1"},
        "capabilities": {},
        "tools": [
            {
                "original": "search",
                "title": "Search",
                "description": "Search https://hidden.example token=capture-secret",
                "params": [
                    {
                        "original": "limit",
                        "description": "Limit",
                        "required": False,
                    }
                ],
            }
        ],
        "resources": [
            {
                "uri": "https://resource.example/private",
                "name": "Reference",
                "description": "Read resource",
            }
        ],
        "resource_templates": [
            {
                "uri": "file:///private/template/{name}",
                "name": "Template",
                "description": "Read template",
            }
        ],
        "prompts": [
            {
                "original": "summarize",
                "name": "Summarize",
                "description": "Summarize a document",
                "args": [
                    {
                        "original": "document",
                        "description": "Document to summarize",
                        "required": True,
                    }
                ],
            }
        ],
    }
    path = defaults / f"{backend}.json"
    path.write_text(json.dumps(capture), encoding="utf-8")
    return path


def _run(surface: ModuleType, args: list[str], capsys: pytest.CaptureFixture[str]):
    assert surface.main(args) in {0, 1, 2}
    return capsys.readouterr()


def _json_output(capsys: pytest.CaptureFixture[str]) -> dict:
    output = capsys.readouterr()
    assert not output.err
    return json.loads(output.out)


def _write_skill_check_fixture(root: Path) -> None:
    """Create a minimal valid skill tree for isolated Git-aware check tests."""
    canonical = root / ".agents/skills/mcp-tool-design"
    research = canonical / "research"
    research.mkdir(parents=True)
    (canonical / "SKILL.md").write_text(
        "---\n"
        "name: mcp-tool-design\n"
        "description: A short valid description.\n"
        "---\n\n"
        "# MCP surface design\n",
        encoding="utf-8",
    )
    (canonical / "agents").mkdir()
    (canonical / "agents/openai.yaml").write_text(
        'display_name: "MCP Tool Design"\n'
        'short_description: "Tune MCP surfaces with client-aware evidence"\n'
        "default_prompt: \"Use $mcp-tool-design to improve an MCP backend's "
        'advertised surface and validate it for the target client."\n',
        encoding="utf-8",
    )
    (research / ".gitignore").write_text(
        "*\n!.gitignore\n!ABOUT.md\n", encoding="utf-8"
    )
    (research / "ABOUT.md").write_text("# Local research receipts\n", encoding="utf-8")
    adapter = root / ".claude/skills/mcp-tool-design"
    adapter.mkdir(parents=True)
    (adapter / "SKILL.md").write_text(
        "---\n"
        "name: mcp-tool-design\n"
        "description: A short valid adapter description.\n"
        "---\n\n"
        "# Claude Code adapter\n",
        encoding="utf-8",
    )
    corpus = root / "corpus"
    corpus.mkdir()
    (corpus / "RETENTION.md").write_text(
        "# Corpus retention manifest\n\n"
        "| Stable ID | Publisher / source | URL | Revision / retrieved | "
        "Usage / license constraint | Purpose | Canonical skill consumer path |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| TEST | Example | https://example.test/source | 1 | URL pointer | "
        "Test evidence | `none` |\n",
        encoding="utf-8",
    )


def _init_git_repository(root: Path) -> None:
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "add", "--all"], check=True, capture_output=True
    )


def test_surface_help_and_missing_config_do_not_create_state(
    surface: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit, match="0"):
        surface.parse_args(["--help"])
    missing = tmp_path / "not-created.toml"
    assert surface.main(["--config", str(missing)]) == 2
    output = capsys.readouterr()
    assert "input error" in output.err
    assert not missing.exists()


def test_surface_is_deterministic_and_preserves_config_order(
    surface: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)
    defaults = tmp_path / "defaults"
    _write_capture(defaults)
    args = [
        "--config",
        str(config),
        "--defaults-dir",
        str(defaults),
        "--format",
        "json",
    ]
    assert surface.main(args) == 0
    first = capsys.readouterr().out
    assert surface.main(args) == 0
    second = capsys.readouterr().out
    assert first == second
    report = json.loads(first)
    assert report["schema_version"] == 1
    assert report["client"] == "generic"
    assert [backend["name"] for backend in report["backends"]] == ["first", "second"]
    assert report["backends"][0]["capture"]["status"] == "present"
    assert report["backends"][1]["capture"]["status"] == "missing"
    assert "captured_at" not in first
    assert str(tmp_path) not in first


def test_surface_effective_overrides_dangling_and_utf8_byte_count(
    surface: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)
    defaults = tmp_path / "defaults"
    _write_capture(defaults)
    assert (
        surface.main(
            [
                "--config",
                str(config),
                "--defaults-dir",
                str(defaults),
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = _json_output(capsys)
    first = report["backends"][0]
    assert first["instructions"] == {
        "source": "override",
        "utf8_bytes": len(b"Use <redacted>; <redacted>"),
        "value": "Use <redacted>; <redacted>",
    }
    tool = first["tools"][0]
    assert tool["name"] == "find"
    assert tool["description"]["value"] == "Find <redacted> <redacted>"
    assert first["resource_templates"][0]["original"] == "Template"
    assert {item["kind"] for item in first["dangling_overrides"]} == {
        "tool-parameter",
        "prompt",
    }
    captured_instruction = surface._text("Résumé ✓", "captured", False)
    assert captured_instruction["utf8_bytes"] == len("Résumé ✓".encode())


def test_surface_filter_disabled_names_only_and_strict_exit(
    surface: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path, disabled=True)
    defaults = tmp_path / "defaults"
    _write_capture(defaults)
    args = [
        "--config",
        str(config),
        "--defaults-dir",
        str(defaults),
        "--backend",
        "first",
        "--names-only",
        "--format",
        "json",
        "--strict",
    ]
    assert surface.main(args) == 1  # a configured missing prompt is dangling
    report = _json_output(capsys)
    assert [backend["name"] for backend in report["backends"]] == ["first"]
    assert report["backends"][0]["enabled"] is False
    assert report["backends"][0]["tools"][0]["enabled"] is False
    assert "description" not in report["backends"][0]["tools"][0]
    assert surface.main(["--config", str(config), "--backend", "unknown"]) == 2
    assert "input error" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ("{not json", "valid UTF-8 JSON"),
        (json.dumps({"backend": "wrong"}), "different backend"),
        (json.dumps({"backend": "first", "tools": {}}), "tools must be a list"),
    ],
)
def test_surface_rejects_malformed_or_mismatched_selected_capture(
    surface: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    contents: str,
    expected: str,
) -> None:
    config = _write_config(tmp_path)
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    (defaults / "first.json").write_text(contents, encoding="utf-8")
    assert surface.main(["--config", str(config), "--defaults-dir", str(defaults)]) == 2
    assert expected in capsys.readouterr().err


def test_surface_redacts_transport_and_secret_material(
    surface: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)
    defaults = tmp_path / "defaults"
    _write_capture(defaults)
    assert (
        surface.main(
            [
                "--config",
                str(config),
                "--defaults-dir",
                str(defaults),
                "--format",
                "json",
            ]
        )
        == 0
    )
    rendered = capsys.readouterr().out
    for forbidden in (
        "transport.example",
        "never-render-this",
        "local-secret",
        "capture-secret",
        "hidden.example",
        "${LOCAL_SECRET}",
        '"url"',
        '"command"',
        '"args"',
        '"env"',
        '"headers"',
    ):
        assert forbidden not in rendered
    assert "<redacted>" in rendered


@pytest.mark.parametrize(
    "secret",
    [
        "access_token = access-token-value",
        'client_secret: "client secret value"',
        "API key = api-key-value",
        "authorization: Bearer authorization-value",
    ],
)
def test_surface_redacts_assigned_credentials_directly(
    surface: ModuleType, secret: str
) -> None:
    assert surface._redact(f"Keep ordinary prose. {secret}; Keep this too.") == (
        "Keep ordinary prose. <redacted>; Keep this too."
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("access_token", "json-access-leak"),
        ("client_secret", "json-client-leak"),
        ("API key", "json-api-leak"),
        ("authorization", "Bearer json-auth-leak"),
    ],
)
def test_surface_redacts_quoted_credential_assignments_directly(
    surface: ModuleType, key: str, value: str
) -> None:
    rendered = surface._redact(
        f'Before {{"{key}": "{value}", "ordinary": "prose"}} after'
    )
    assert value not in rendered
    assert f'"{key}": "<redacted>"' in rendered
    assert '"ordinary": "prose"' in rendered


def test_surface_report_redacts_assigned_credentials_from_captures_and_overrides(
    surface: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)
    override_instructions = (
        "instructions = 'access_token = override-access; API key: override-key; "
        '{"access_token":"json-override-access",'
        '"authorization":"Bearer json-override-auth",'
        '"ordinary":"prose"}; ordinary prose\'\n'
    )
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'instructions = "Use ${LOCAL_SECRET}; bearer config-secret"\n',
            override_instructions,
        ),
        encoding="utf-8",
    )
    defaults = tmp_path / "defaults"
    _write_capture(defaults)
    capture = defaults / "first.json"
    capture_data = json.loads(capture.read_text(encoding="utf-8"))
    capture_data["instructions"] = json.dumps(
        {
            "client_secret": "json-capture-secret",
            "authorization": "Bearer json-capture-auth",
            "ordinary": "prose",
        }
    )
    capture_data["tools"][0]["description"] = (
        '{"access_token":"json-capture-description",'
        '"API key":"json-capture-api","ordinary":"capture prose"}'
    )
    capture.write_text(json.dumps(capture_data), encoding="utf-8")
    assert (
        surface.main(
            [
                "--config",
                str(config),
                "--defaults-dir",
                str(defaults),
                "--format",
                "json",
            ]
        )
        == 0
    )
    override_rendered = capsys.readouterr().out
    config.write_text(
        config.read_text(encoding="utf-8")
        .replace(override_instructions, "")
        .replace(
            'description = "Find ${BACKEND_TOKEN} token=tool-secret"\n',
            "",
        ),
        encoding="utf-8",
    )
    assert (
        surface.main(
            [
                "--config",
                str(config),
                "--defaults-dir",
                str(defaults),
                "--format",
                "json",
            ]
        )
        == 0
    )
    rendered = override_rendered + capsys.readouterr().out
    for value in (
        "override-access",
        "override-key",
        "json-override-access",
        "json-override-auth",
        "json-capture-secret",
        "json-capture-auth",
        "json-capture-description",
        "json-capture-api",
    ):
        assert value not in rendered
    assert "ordinary prose" in rendered
    assert "capture prose" in rendered


def test_surface_does_not_write_connect_or_spawn(
    surface: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_config(tmp_path)
    defaults = tmp_path / "defaults"
    _write_capture(defaults)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the offline inspector attempted an external action")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    assert (
        surface.main(
            [
                "--config",
                str(config),
                "--defaults-dir",
                str(defaults),
                "--format",
                "text",
            ]
        )
        == 0
    )
    assert "Backend: first" in capsys.readouterr().out
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_skill_self_check_passes_from_an_unrelated_cwd(tmp_path: Path) -> None:
    check = _module(CHECK_PATH, "project_skill_check")
    assert check.main(cwd=tmp_path) == 0


def test_skill_self_check_rejects_nested_adapter_file(tmp_path: Path) -> None:
    check = _module(CHECK_PATH, "project_skill_check_nested_adapter")
    _write_skill_check_fixture(tmp_path)
    nested = tmp_path / ".claude/skills/mcp-tool-design/anything/SKILL.md"
    nested.parent.mkdir()
    nested.write_text("not an adapter\n", encoding="utf-8")
    _init_git_repository(tmp_path)
    diagnostics = check.check(tmp_path)
    assert any(": claude-adapter:" in diagnostic for diagnostic in diagnostics)


def test_skill_self_check_allows_ignored_local_research_receipt(tmp_path: Path) -> None:
    check = _module(CHECK_PATH, "project_skill_check_local_receipt")
    _write_skill_check_fixture(tmp_path)
    _init_git_repository(tmp_path)
    receipt = tmp_path / ".agents/skills/mcp-tool-design/research/local-receipt.md"
    receipt.write_text("machine-local receipt\n", encoding="utf-8")
    assert check.check(tmp_path) == []


def test_skill_self_check_rejects_tracked_research_file(tmp_path: Path) -> None:
    check = _module(CHECK_PATH, "project_skill_check_tracked_receipt")
    _write_skill_check_fixture(tmp_path)
    receipt = tmp_path / ".agents/skills/mcp-tool-design/research/tracked-receipt.md"
    receipt.write_text("must not ship\n", encoding="utf-8")
    _init_git_repository(tmp_path)
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "-f", str(receipt)],
        check=True,
        capture_output=True,
    )
    diagnostics = check.check(tmp_path)
    assert any(": research-tree:" in diagnostic for diagnostic in diagnostics)


@pytest.mark.parametrize(
    ("contents", "rule"),
    [
        ("[broken](missing.md)\n", "local-link"),
        ("${CLAUDE_SKILL_DIR}\n", "legacy-marker"),
        ("https://example.test/" + "copied body\n" * 20, "pointer-content"),
    ],
)
def test_skill_self_check_reports_focused_fixture_errors(
    tmp_path: Path, contents: str, rule: str
) -> None:
    check = _module(CHECK_PATH, f"project_skill_check_{rule}")
    fixture = tmp_path / "fixture.md"
    fixture.write_text(contents, encoding="utf-8")
    diagnostics = check.inspect_pointer_file(fixture, tmp_path)
    assert any(f": {rule}:" in diagnostic for diagnostic in diagnostics)
