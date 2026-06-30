"""End-to-end verification against the running gateway (per-backend endpoints).

Each backend is exposed on its OWN endpoint (``/<backend>/mcp``) as its own MCP
server, with BARE tool names and its OWN server instructions — its own ~2KB
budget (issue #29). This enumerates backends from ``/admin/api/state``, then for
each endpoint asserts: it is reachable, every enabled tool is exposed under its
effective (bare) name, and its instructions are within the 2KB budget. A
passthrough call on deepwiki (if present) proves calls forward end to end.

Usage:  uv run verify_rename.py [http://127.0.0.1:9100]
Exits non-zero on the first failed assertion.
"""

from __future__ import annotations

import json
import sys
import urllib.request

import anyio

from fastmcp import Client

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9100").rstrip("/")
# Tolerate the old single-endpoint form (…/mcp): strip it back to the base.
if BASE.endswith("/mcp"):
    BASE = BASE[: -len("/mcp")]

checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    checks.append((ok, label))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 (loopback only)
        return json.loads(r.read())


async def main() -> int:
    state = _get_json(f"{BASE}/admin/api/state")
    backends = state["backends"]
    print(f"gateway has {len(backends)} endpoint(s): {[b['name'] for b in backends]}\n")
    check(len(backends) > 0, "at least one backend endpoint")

    deepwiki = None
    for b in backends:
        name = b["name"]
        url = f"{BASE}{b['endpoint']}"
        if name == "deepwiki":
            deepwiki = b
        try:
            async with Client(url) as c:
                exposed = {t.name for t in await c.list_tools()}
                instr = c.initialize_result.instructions or ""
        except Exception as exc:  # noqa: BLE001
            check(False, f"{name}: endpoint {url} reachable ({exc})")
            continue
        check(True, f"{name}: endpoint {url} reachable")

        # Every ENABLED tool is exposed under its effective name, which is BARE
        # (no '<backend>_' prefix) — each backend is its own endpoint now (#29).
        expected = {
            (t.get("name") or t["original"])
            for t in b["tools"]
            if t.get("enabled", True)
        }
        missing = expected - exposed
        check(
            not missing, f"{name}: all enabled tools exposed (bare); missing={missing}"
        )
        check(
            all(not t.startswith(f"{name}_") for t in exposed),
            f"{name}: no '<backend>_' prefix on exposed names: {sorted(exposed)[:4]}",
        )

        # #29: this endpoint carries only its OWN instructions, within the 2KB cap.
        nbytes = len(instr.encode("utf-8"))
        check(nbytes <= 2048, f"{name}: instructions within 2KB budget ({nbytes} B)")

    # Passthrough call (bare name) on deepwiki, if present — proves forwarding.
    if deepwiki is not None:
        print(
            "\npassthrough call: deepwiki ask_question(repoName=prefecthq/fastmcp, …)"
        )
        async with Client(f"{BASE}{deepwiki['endpoint']}") as c:
            res = await c.call_tool(
                "ask_question",
                {
                    "repoName": "prefecthq/fastmcp",
                    "question": "What transport does the proxy use?",
                },
            )
        text = "".join(getattr(block, "text", "") for block in res.content)
        ok = len(text.strip()) > 0
        check(ok, "deepwiki passthrough returned a real backend answer")
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
