"""Contracts for the sanitized CI protocol receipt bundle."""

from __future__ import annotations

import json
from pathlib import Path

from tools import ci_receipts


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_bundle_allows_only_final_outcomes_and_counts(tmp_path: Path) -> None:
    raw = tmp_path / "mcp-gateway-raw-wire-unsafe"
    official = tmp_path / "mcp-gateway-official-conformance-unsafe"
    _write_json(
        raw / "receipts/report.json",
        {
            "checks": ["fixture check", "a token: should-not-escape"],
            "error": "raw failure details must not escape",
            "scratch": "/private/home/with-sensitive-config",
            "status": "failed",
        },
    )
    _write_json(
        official / "report.json",
        {
            "endpoint": "http://127.0.0.1:50123/conformance/mcp",
            "scenarios": ["ping", "tools-list"],
            "status": "passed",
        },
    )
    (raw / "home/config.toml").parent.mkdir(parents=True)
    (raw / "home/config.toml").write_text("SECRET=never-publish", encoding="utf-8")
    (official / "official-logs/ping.stderr").parent.mkdir(parents=True)
    (official / "official-logs/ping.stderr").write_text("raw stderr", encoding="utf-8")

    output = ci_receipts.write_bundle(tmp_path, tmp_path / "ci-receipts")

    assert output.parent.iterdir()
    assert {path.name for path in output.parent.iterdir()} == {"receipt.json"}
    rendered = output.read_text(encoding="utf-8")
    assert json.loads(rendered) == {
        "schema": "mcp-gateway.ci-receipts/v1",
        "suites": [
            {"check_count": 2, "status": "passed", "suite": "official-conformance"},
            {"check_count": 2, "status": "failed", "suite": "raw-wire"},
        ],
    }
    for forbidden in (
        "127.0.0.1",
        "SECRET=never-publish",
        "raw failure details",
        "should-not-escape",
        "private/home",
    ):
        assert forbidden not in rendered


def test_bundle_marks_missing_and_invalid_reports_without_failure_details(
    tmp_path: Path,
) -> None:
    broken = tmp_path / "mcp-gateway-raw-wire-broken/receipts/report.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("token=not-valid-json", encoding="utf-8")

    bundle = ci_receipts.collect(tmp_path)

    assert bundle == {
        "schema": "mcp-gateway.ci-receipts/v1",
        "suites": [
            {"status": "not-produced", "suite": "official-conformance"},
            {"status": "unreadable", "suite": "raw-wire"},
        ],
    }
