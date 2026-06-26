"""End-to-end verification: connect to the running gateway over HTTP and assert
every broadcast-text rewrite from config.toml was actually applied, then make a
real passthrough call to prove reverse-mapping works.

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
    check("gitnexus_query" in names, "gitnexus passthrough: 'gitnexus_query' present (original)")
    check("gitnexus_list_repos" in names, "gitnexus passthrough: 'gitnexus_list_repos' present")
    if "gitnexus_query" in names:
        props = (tools["gitnexus_query"].inputSchema or {}).get("properties", {})
        check("task_context" in props, "gitnexus passthrough: original param 'task_context' intact")

    # --- deepwiki ask_question -> wiki_ask (the remaining demo override) ---
    check("wiki_ask" in names, "renamed tool 'wiki_ask' present")
    check(
        "deepwiki_ask_question" not in names, "original 'deepwiki_ask_question' hidden"
    )
    if "wiki_ask" in names:
        props = (tools["wiki_ask"].inputSchema or {}).get("properties", {})
        check(
            "repo" in props and "repoName" not in props,
            "param 'repoName' renamed to 'repo'",
        )

    # --- disabled tool dropped ---
    check(
        "deepwiki_read_wiki_structure" not in names,
        "disabled tool 'read_wiki_structure' dropped from listing",
    )

    # --- passthrough: call renamed tool with renamed param (reverse-mapping) ---
    print("\npassthrough call: wiki_ask(repo=prefecthq/fastmcp, question=...)")
    async with Client(URL) as c:
        res = await c.call_tool(
            "wiki_ask",
            {
                "repo": "prefecthq/fastmcp",
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
