"""Enforce the file-size limits from PRINCIPLES.md as a ratchet.

Every source file must stay under its limit. Files that were already over
the limit when the rule arrived are listed in the baseline with the size
they had then: they may shrink but never grow, and once under the limit
they leave the baseline. Run from the project root; exits nonzero on any
violation and prints one line per offending file.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIMITS = {"src": 400, "tests": 400}
SUFFIXES = {".py", ".html", ".js", ".css"}

# Files over the limit on 2026-09-02 and the size they had then. Lower a
# number whenever its file shrinks; delete the line when it fits the limit.
BASELINE = {
    "src/mcp_gateway/admin.html": 2751,
    "src/mcp_gateway/config_loader.py": 2532,
    "src/mcp_gateway/admin.py": 2517,
    "src/mcp_gateway/cli_surface.py": 1507,
    "src/mcp_gateway/server.py": 1393,
    "src/mcp_gateway/cli_backend.py": 1065,
    "src/mcp_gateway/service.py": 900,
    "src/mcp_gateway/cli.py": 821,
    "src/mcp_gateway/cli_virtual.py": 790,
    "src/mcp_gateway/virtual_tools.py": 723,
    "src/mcp_gateway/cli_settings.py": 584,
    "src/mcp_gateway/admin_routes_backend.py": 492,
    "src/mcp_gateway/logging_setup.py": 448,
    "src/mcp_gateway/cli_common.py": 438,
    "src/mcp_gateway/admin_routes_virtual.py": 424,
    "tests/test_cli.py": 4234,
    "tests/test_admin.py": 3068,
    "tests/test_config_loader.py": 2979,
    "tests/test_server.py": 2811,
    "tests/live/run_mcp_wire.py": 810,
    "tests/test_service.py": 794,
    "tests/test_virtual_tools.py": 755,
    "tests/live/run_virtual_tools.py": 620,
    "tests/test_project_skill.py": 574,
    "tests/test_release_contract.py": 467,
    "tests/test_hooks.py": 401,
}


def line_count(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def violations() -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for top, limit in LIMITS.items():
        for path in sorted((ROOT / top).rglob("*")):
            if path.suffix not in SUFFIXES or "node_modules" in path.parts:
                continue
            rel = path.relative_to(ROOT).as_posix()
            seen.add(rel)
            lines = line_count(path)
            allowed = BASELINE.get(rel)
            if allowed is None:
                if lines > limit:
                    found.append(f"{rel}: {lines} lines, limit {limit}")
            elif lines > allowed:
                found.append(f"{rel}: {lines} lines, grew past its baseline {allowed}")
            elif lines <= limit:
                found.append(f"{rel}: {lines} lines fits the limit; drop its baseline")
    for rel in BASELINE:
        if rel not in seen:
            found.append(f"{rel}: gone; drop its baseline")
    return found


def main() -> int:
    problems = violations()
    for line in problems:
        print(line)
    if problems:
        return 1
    print("file sizes within limits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
