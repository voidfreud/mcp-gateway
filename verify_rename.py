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
import urllib.error
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
    # Scheme guard (#86): urlopen would otherwise honour file:// and other
    # handlers; this verifier only ever talks HTTP(S) to the gateway.
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing non-http(s) url: {url!r}")
    with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 (http(s) only)
        return json.loads(r.read())


def _http_status(url: str) -> int | None:
    """The HTTP status of a bare GET (no auth header), or None if unreachable.
    Used to probe whether the gateway enforces a bearer token (#158)."""
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing non-http(s) url: {url!r}")
    try:
        with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 (http(s) only)
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001 — unreachable -> inconclusive
        return None


def _summary() -> int:
    failed = [label for ok, label in checks if not ok]
    print(f"\n{'=' * 60}")
    if failed:
        print(f"FAILED {len(failed)}/{len(checks)}:")
        for label in failed:
            print(f"  - {label}")
        return 1
    print(f"ALL {len(checks)} CHECKS PASSED")
    return 0


async def main() -> int:  # noqa: PLR0912, PLR0915 — linear end-to-end receipts; splitting scatters the flow
    # A down daemon must report a clean FAIL, not an unhandled traceback (#86).
    try:
        state = _get_json(f"{BASE}/admin/api/state")
    except Exception as exc:  # noqa: BLE001
        check(False, f"gateway reachable at {BASE} ({exc})")
        return _summary()
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
            reachable, err = True, ""
        except Exception as exc:  # noqa: BLE001
            reachable, err = False, str(exc)

        # A backend switched off at the BACKEND level (#38) is NOT mounted (#78):
        # its endpoint is absent (404 / connection refused), not reachable-nil.
        if not b.get("enabled", True):
            check(
                not reachable,
                f"{name}: disabled backend endpoint is absent (unreachable)",
            )
            continue

        if not reachable:
            check(False, f"{name}: endpoint {url} reachable ({err})")
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
    # Call by the EFFECTIVE (possibly renamed) broadcast name, like every other
    # check — a hardcoded original breaks the probe the moment the tool is
    # renamed in the admin (which is the product's whole point).
    if deepwiki is not None and not deepwiki.get("enabled", True):
        deepwiki = None  # backend switched off (#38) -> nothing to call
    ask = None  # bound before the conditional so the check below is unambiguous
    if deepwiki is not None:
        ask = next(
            (
                t.get("name") or t["original"]
                for t in deepwiki["tools"]
                if t["original"] == "ask_question" and t.get("enabled", True)
            ),
            None,
        )
    if deepwiki is not None and ask is not None:
        print(f"\npassthrough call: deepwiki {ask}(repoName=prefecthq/fastmcp, …)")
        async with Client(f"{BASE}{deepwiki['endpoint']}") as c:
            res = await c.call_tool(
                ask,
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

    # --- Receipt 1 (#158): /admin/api/status shows every enabled backend ok ---
    print("\nreceipts:")
    try:
        status = _get_json(f"{BASE}/admin/api/status")["backends"]
    except Exception as exc:  # noqa: BLE001
        check(False, f"status: /admin/api/status reachable ({exc})")
        status = {}
    for b in backends:
        if not b.get("enabled", True):
            continue
        st = status.get(b["name"], {})
        check(
            st.get("state") == "ok",
            f"status: {b['name']} probes ok (got {st.get('state', 'missing')!r})",
        )

    # --- Receipt 2 (#158): a hidden-param injection is applied end to end -------
    # If any enabled tool hides a param with an injected default, the param must
    # be ABSENT from the tool's broadcast inputSchema (Claude never sees it, the
    # gateway injects the fixed value on every call). Skip cleanly when none.
    hidden = None
    for b in backends:
        if not b.get("enabled", True):
            continue
        for t in b["tools"]:
            if not t.get("enabled", True):
                continue
            for p in t.get("params", []):
                if p.get("hide") and p.get("default") is not None:
                    hidden = (b, t, p)
                    break
            if hidden:
                break
        if hidden:
            break
    if hidden is None:
        print("  SKIP  hidden-param: no backend configures a hidden injected param")
    else:
        b, t, p = hidden
        tool_name = t.get("name") or t["original"]
        param_name = p.get("name") or p["original"]
        try:
            async with Client(f"{BASE}{b['endpoint']}") as c:
                spec = next(
                    (x for x in await c.list_tools() if x.name == tool_name), None
                )
            props = (spec.inputSchema or {}).get("properties", {}) if spec else {}
            check(
                spec is not None and param_name not in props,
                f"hidden-param: {b['name']}/{tool_name} hides {param_name!r} "
                f"(absent from broadcast schema, default injected)",
            )
        except Exception as exc:  # noqa: BLE001
            check(False, f"hidden-param: round-trip on {b['name']} failed ({exc})")

    # --- Receipt 3 (#158): bearer 401 when a token is enforced -----------------
    # Probe an /admin/api/* path with NO auth header. 401 => the gateway enforces
    # a bearer token (the receipt); 200 => open, nothing to assert (skip cleanly).
    code = _http_status(f"{BASE}/admin/api/status")
    if code == 401:
        check(True, "bearer: unauthenticated /admin/api/* returns 401 (token enforced)")
    elif code == 200:
        print("  SKIP  bearer: gateway is open (no token configured)")
    else:
        print(f"  SKIP  bearer: probe inconclusive (status {code})")

    return _summary()


if __name__ == "__main__":
    sys.exit(anyio.run(main))
