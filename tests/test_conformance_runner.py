"""Contracts for the checked-in official conformance Node fixture."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = PROJECT_ROOT / "tests/conformance/run_official.py"
FIXTURE_ROOT = RUNNER_PATH.parent


def _runner_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "official_conformance_runner", RUNNER_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_official_runner_is_the_preinstalled_no_download_binary(tmp_path: Path) -> None:
    runner = _runner_module()

    command = runner._runner_command("ping", "http://127.0.0.1:9010/mcp", tmp_path)

    assert command[:5] == ["npm", "exec", "--no", "--", "conformance"]
    assert "npx" not in command
    assert "--yes" not in command
    assert command[-2:] == ["--output-dir", str(tmp_path / "official-results")]
    assert runner.NODE_FIXTURE == FIXTURE_ROOT


def test_node_fixture_pins_the_conformance_package_with_integrity() -> None:
    runner = _runner_module()
    package = json.loads((FIXTURE_ROOT / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((FIXTURE_ROOT / "package-lock.json").read_text(encoding="utf-8"))
    installed = lock["packages"]["node_modules/@modelcontextprotocol/conformance"]

    assert package["dependencies"] == {"@modelcontextprotocol/conformance": "0.1.16"}
    assert (
        runner.CONFORMANCE_VERSION
        == package["dependencies"]["@modelcontextprotocol/conformance"]
    )
    assert installed["version"] == "0.1.16"
    assert installed["integrity"].startswith("sha512-")


def test_official_runner_documents_every_server_scenario() -> None:
    runner = _runner_module()
    official = set(runner.OFFICIAL_SERVER_SCENARIOS)
    applicable = set(runner.SCENARIOS)
    skipped = set(runner.SKIPPED_SCENARIOS)

    assert len(official) == 32
    assert applicable.isdisjoint(skipped)
    assert applicable | skipped == official
    assert all(runner.SKIPPED_SCENARIOS.values())
    assert {
        "tools-call-image",
        "tools-call-audio",
        "tools-call-embedded-resource",
        "tools-call-mixed-content",
        "json-schema-2020-12",
        "server-sse-multiple-streams",
        "prompts-get-with-image",
        "dns-rebinding-protection",
    } <= applicable


def test_official_runner_exercises_the_persistent_proxy(tmp_path: Path) -> None:
    runner = _runner_module()
    config_path = tmp_path / "config.toml"

    runner._write_config(config_path, port=9010, log_file=tmp_path / "gateway.jsonl")

    assert "stateless = false" in config_path.read_text(encoding="utf-8")
