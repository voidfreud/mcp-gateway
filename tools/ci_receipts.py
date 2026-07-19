#!/usr/bin/env python3
"""Create the small, non-sensitive receipt bundle uploaded by CI.

Harness scratch directories deliberately keep raw logs, isolated homes,
configuration, and failure details for a developer running locally.  They are
not CI artifacts.  This tool extracts only known suite names, final statuses,
and counts from their machine-readable reports; it never copies report values,
paths, errors, endpoints, or any scratch files.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SCHEMA = "mcp-gateway.ci-receipts/v1"
SUITES = (
    (
        "official-conformance",
        "mcp-gateway-official-conformance-",
        Path("report.json"),
        "scenarios",
    ),
    ("raw-wire", "mcp-gateway-raw-wire-", Path("receipts/report.json"), "checks"),
)
FINAL_STATUSES = frozenset({"passed", "failed"})


def _report_summary(
    suite: str, report_path: Path | None, count_key: str
) -> dict[str, str | int]:
    """Return an allow-listed summary without retaining report content."""
    summary: dict[str, str | int] = {"suite": suite}
    if report_path is None:
        summary["status"] = "not-produced"
        return summary
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        summary["status"] = "unreadable"
        return summary
    if not isinstance(report, Mapping):
        summary["status"] = "unreadable"
        return summary

    status = report.get("status")
    summary["status"] = status if status in FINAL_STATUSES else "incomplete"
    counted = report.get(count_key)
    if isinstance(counted, list):
        summary["check_count"] = len(counted)
    return summary


def _first_report(scratch_root: Path, prefix: str, relative: Path) -> Path | None:
    """Find the one report shape each disposable harness is allowed to expose."""
    candidates = sorted(
        child / relative
        for child in scratch_root.glob(f"{prefix}*")
        if child.is_dir() and (child / relative).is_file()
    )
    return candidates[-1] if candidates else None


def collect(scratch_root: Path) -> dict[str, Any]:
    """Summarize known harness reports using a fixed, sensitive-data-free schema."""
    return {
        "schema": SCHEMA,
        "suites": [
            _report_summary(
                suite, _first_report(scratch_root, prefix, relative), count_key
            )
            for suite, prefix, relative, count_key in SUITES
        ],
    }


def write_bundle(scratch_root: Path, output_dir: Path) -> Path:
    """Write the only file CI is permitted to upload for protocol harnesses."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "receipt.json"
    output.write_text(
        json.dumps(collect(scratch_root), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


def main(argv: Iterable[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    print(write_bundle(args.scratch_root, args.output_dir))


if __name__ == "__main__":
    main()
