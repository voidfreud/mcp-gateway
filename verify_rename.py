"""End-to-end verification against the running gateway.

The seed config ships both backends as PASSTHROUGH (no overrides), so this
asserts that every backend tool reaches Claude under its original (prefixed)
name with its original params, and that a real call forwards correctly. (When
you add overrides in config.toml / the admin UI, extend these assertions.)

Usage:  uv run verify_rename.py [http://127.0.0.1:9100/mcp]
Exits non-zero on the first failed assertion.
"""

from __future__ import annotations

import sys

import anyio

from fastmcp import Client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9100/mcp"

checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    checks.append((ok, label))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


async def main() -> int:
    async with Client(URL) as c:
        tools = {t.name: t for t in await c.list_tools()}
    names = set(tools)
    print(f"gateway exposes {len(names)} tools: {sorted(names)}\n")

    # --- gitnexus is PASSTHROUGH (no overrides) — original names reach Claude ---
    check(
        "gitnexus_query" in names,
        "gitnexus passthrough: 'gitnexus_query' present (original)",
    )
    check(
        "gitnexus_list_repos" in names,
        "gitnexus passthrough: 'gitnexus_list_repos' present",
    )
    if "gitnexus_query" in names:
        props = (tools["gitnexus_query"].inputSchema or {}).get("properties", {})
        check(
            "task_context" in props,
            "gitnexus passthrough: original param 'task_context' intact",
        )

    # --- deepwiki is PASSTHROUGH (no overrides) — original names reach Claude ---
    check(
        "deepwiki_ask_question" in names,
        "deepwiki passthrough: 'deepwiki_ask_question' present (original)",
    )
    check(
        "deepwiki_read_wiki_structure" in names,
        "deepwiki passthrough: 'read_wiki_structure' present (not disabled)",
    )
    if "deepwiki_ask_question" in names:
        props = (tools["deepwiki_ask_question"].inputSchema or {}).get("properties", {})
        check(
            "repoName" in props,
            "deepwiki passthrough: original param 'repoName' intact",
        )

    # --- passthrough call with ORIGINAL names proves the gateway forwards calls ---
    print(
        "\npassthrough call: deepwiki_ask_question(repoName=prefecthq/fastmcp, question=...)"
    )
    async with Client(URL) as c:
        res = await c.call_tool(
            "deepwiki_ask_question",
            {
                "repoName": "prefecthq/fastmcp",
                "question": "What transport does the proxy use?",
            },
        )
    text = ""
    for block in res.content:
        text += getattr(block, "text", "")
    ok = len(text.strip()) > 0
    check(ok, "passthrough returned a real backend answer")
    if ok:
        print(f"\n  answer (first 200 chars): {text.strip()[:200]}...")

    failed = [label for ok, label in checks if not ok]
    print(f"\n{'=' * 60}")
    if failed:
        print(f"FAILED {len(failed)}/{len(checks)}:")
        for label in failed:
            print(f"  - {label}")
        return 1
    print(f"ALL {len(checks)} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(anyio.run(main))
