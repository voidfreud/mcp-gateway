"""Code mode (#13, spec §17.3): search / get_schema / execute meta-tools.

An opt-in endpoint (``/meta/mcp``, ``[meta] enabled = true``) that exposes
exactly THREE tools letting an agent script against the gateway's whole
catalog instead of loading every tool into context:

- ``search`` ranks matching tools across every mounted backend (and the
  composite endpoint) by deterministic text match — no LLM, no network beyond
  the in-process ``list_tools`` per backend.
- ``get_schema`` returns ONE tool's full exposed definition — description plus
  the JSON Schema of its parameters — read from what the live proxy actually
  broadcasts, so every rename/override applies.
- ``execute`` calls a tool through the gateway's own per-backend proxy (the
  same in-process path as composites and the admin mini-inspector), returning
  the result verbatim and honest structured errors instead of crashes.

Targets resolve from the server lifespan's live ``registry`` (backend name ->
proxy) at CALL time, shared by reference — a backend that mounts, recycles, or
unmounts later is picked up automatically. The composite server (when mounted)
is included under the ``composite`` name via the shared ``hooks`` dict.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Annotated, Any

from fastmcp import Client, FastMCP
from pydantic import Field

from mcp_gateway.config_loader import COMPOSITE_ROUTE, META_ROUTE

__all__ = [
    "META_ROUTE",
    "build_meta_server",
    "score_tool",
    "targets",
]

# How long one execute() call may run before it errors out (mirrors the admin
# mini-inspector's ceiling).
EXECUTE_TIMEOUT = 60.0

# Listing one backend's tools is in-process for warm backends but may open a
# fresh session for stateless ones; bound it so one hung backend can't stall a
# whole search sweep.
LIST_TIMEOUT = 15.0

_WORD_RE = re.compile(r"[a-z0-9]+")

_INSTRUCTIONS = """\
Code mode for the whole gateway: discover and call any tool on any mounted \
backend without loading every catalog into context. Workflow: `search` finds \
candidate tools by keyword across all backends; `get_schema` fetches one \
tool's full exposed definition (read it before calling anything nontrivial); \
`execute` runs the tool through the gateway's own proxy path, so every \
rename/override applies and the names here are exactly the names search and \
get_schema returned."""


# ---------------------------------------------------------------------------
# Target resolution + scoring
# ---------------------------------------------------------------------------


def targets(registry: dict, hooks: dict) -> dict[str, Any]:
    """The live search/call targets: every mounted backend proxy, plus the
    composite server (under ``composite``) when it is mounted. Resolved fresh
    per call so later mounts/unmounts/recycles are picked up automatically."""
    out = dict(registry)
    comp = hooks.get("composite_server")
    if comp is not None:
        out[COMPOSITE_ROUTE] = comp
    return out


def _param_text(schema: dict | None) -> str:
    """Searchable text of a tool's input schema: property names + descriptions."""
    props = (schema or {}).get("properties") or {}
    parts: list[str] = []
    for name, spec in props.items():
        parts.append(str(name))
        if isinstance(spec, dict) and spec.get("description"):
            parts.append(str(spec["description"]))
    return " ".join(parts).lower()


def score_tool(query: str, tool) -> int:
    """Deterministic relevance of *tool* (an ``mcp.types.Tool``) to *query*.

    Simple weighted substring/word overlap — name matches outrank title, title
    outranks description, description outranks parameter docs. No LLM: the
    same query always ranks the same catalog the same way.
    """
    q = query.lower().strip()
    name = tool.name.lower()
    title = (getattr(tool, "title", None) or "").lower()
    desc = (tool.description or "").lower()
    params = _param_text(getattr(tool, "inputSchema", None))
    s = 0
    if q and q == name:
        s += 100  # exact name match wins outright
    elif q and q in name:
        s += 40  # whole query is a substring of the name
    for w in set(_WORD_RE.findall(q)):
        if w in name:
            s += 20
        if w in title:
            s += 8
        if w in desc:
            s += 4
        if w in params:
            s += 2
    return s


def _summary(tool) -> str:
    """One compact line for a search row: the description's first line."""
    first = (tool.description or "").strip().splitlines()
    line = first[0].strip() if first else ""
    return line if len(line) <= 120 else line[:117] + "..."  # noqa: PLR2004


async def _list_tools(name: str, target, log) -> tuple[str, list]:
    """List one target's EXPOSED tools (post-transform); a down backend
    contributes nothing instead of sinking the sweep."""
    try:
        async with asyncio.timeout(LIST_TIMEOUT):
            async with Client(target) as c:
                return name, await c.list_tools()
    except Exception as exc:  # noqa: BLE001 — a dead backend must not sink search
        log.warning("meta_list_failed", backend=name, error=str(exc))
        return name, []


# ---------------------------------------------------------------------------
# Server build
# ---------------------------------------------------------------------------


def build_meta_server(registry: dict, hooks: dict, log) -> FastMCP:
    """The ``/meta/mcp`` FastMCP server carrying the three code-mode tools.

    *registry* and *hooks* are the server lifespan's live dicts, shared BY
    REFERENCE — every call resolves its targets fresh, so the meta endpoint
    always reflects what is mounted right now.
    """
    server = FastMCP(name="mcp-gateway-meta", instructions=_INSTRUCTIONS)

    @server.tool
    async def search(
        query: Annotated[
            str,
            Field(
                description="Keywords naming what the tool should do, e.g. "
                "'search web' or 'pull request review'. Case-insensitive; an "
                "exact or substring match on the tool NAME ranks highest, then "
                "title, description, and parameter docs."
            ),
        ],
        backend: Annotated[
            str | None,
            Field(
                description="Restrict to one backend (its gateway name, e.g. "
                "'deepwiki', or 'composite' for composite tools). Omit to "
                "search every mounted backend."
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                description="Maximum rows returned (best matches first).",
                ge=1,
                le=100,
            ),
        ] = 10,
    ) -> dict:
        """Find tools across every mounted gateway backend by keyword.

        Deterministic text ranking (substring/word overlap — not semantic; if
        a query misses, retry with a synonym). Returns compact rows
        ``{backend, tool, summary}`` ordered best-first; feed a row's
        ``backend`` + ``tool`` straight into ``get_schema`` or ``execute``.
        Also returns ``searched`` (``{backend: tool count}``) so an empty
        result is diagnosable. An unknown ``backend`` filter returns
        ``{error, mounted}`` instead of failing.
        """
        cat = targets(registry, hooks)
        if backend is not None:
            if backend not in cat:
                return {
                    "error": f"backend {backend!r} is not mounted",
                    "mounted": sorted(cat),
                }
            cat = {backend: cat[backend]}
        listed = await asyncio.gather(
            *(_list_tools(n, t, log) for n, t in sorted(cat.items()))
        )
        scored: list[tuple[int, str, str, dict]] = []
        for bname, tools in listed:
            for tool in tools:
                s = score_tool(query, tool)
                if s > 0:
                    row = {
                        "backend": bname,
                        "tool": tool.name,
                        "summary": _summary(tool),
                    }
                    scored.append((s, bname, tool.name, row))
        scored.sort(key=lambda t: (-t[0], t[1], t[2]))
        log.info("meta_search", query=query, backend=backend, hits=len(scored))
        return {
            "matches": [row for _, _, _, row in scored[:limit]],
            "searched": {bname: len(tools) for bname, tools in listed},
        }

    @server.tool
    async def get_schema(
        backend: Annotated[
            str,
            Field(description="The tool's backend, exactly as `search` returned it."),
        ],
        tool: Annotated[
            str,
            Field(
                description="The EXPOSED tool name, exactly as `search` "
                "returned it (post-rename — not the backend's internal name)."
            ),
        ],
    ) -> dict:
        """The full exposed definition of one tool: description plus the JSON
        Schema of its parameters, exactly as the gateway broadcasts it (every
        rename and override applied). Read this before an ``execute`` call
        with nontrivial arguments — ``input_schema.required`` names the
        parameters you must pass. An unknown backend or tool returns
        ``{error, ...}`` with the valid alternatives instead of failing.
        """
        cat = targets(registry, hooks)
        target = cat.get(backend)
        if target is None:
            return {
                "error": f"backend {backend!r} is not mounted",
                "mounted": sorted(cat),
            }
        _, tools = await _list_tools(backend, target, log)
        found = next((t for t in tools if t.name == tool), None)
        if found is None:
            return {
                "error": f"no tool {tool!r} on backend {backend!r}",
                "available": sorted(t.name for t in tools),
            }
        return {
            "backend": backend,
            "name": found.name,
            "title": getattr(found, "title", None),
            "description": found.description,
            "input_schema": found.inputSchema,
            "output_schema": getattr(found, "outputSchema", None),
        }

    @server.tool
    async def execute(
        backend: Annotated[
            str,
            Field(description="The tool's backend, exactly as `search` returned it."),
        ],
        tool: Annotated[
            str,
            Field(
                description="The EXPOSED tool name, exactly as `search`/"
                "`get_schema` returned it."
            ),
        ],
        arguments: Annotated[
            dict,
            Field(
                description="The call arguments as an object matching "
                "`get_schema`'s input_schema. Pass {} for a no-argument tool."
            ),
        ],
    ) -> dict:
        """Run one tool through the gateway's own proxy — the same in-process
        path a direct call on the backend's endpoint takes, so every override,
        rename, and injected default applies. Returns ``{ok, is_error, ms,
        content, structured}`` with the tool's result verbatim: a tool-level
        failure comes back as ``is_error: true`` with the message in
        ``content``; an unknown backend/tool or transport failure returns
        ``{ok: false, error}`` — never a crash. Use ``search`` +
        ``get_schema`` first for exact names and required arguments.
        """
        cat = targets(registry, hooks)
        target = cat.get(backend)
        if target is None:
            return {
                "ok": False,
                "error": f"backend {backend!r} is not mounted",
                "mounted": sorted(cat),
            }
        started = time.perf_counter()
        try:
            async with asyncio.timeout(EXECUTE_TIMEOUT):
                async with Client(target) as c:
                    res = await c.call_tool_mcp(tool, arguments)
        except TimeoutError:
            return {
                "ok": False,
                "error": f"no result within {EXECUTE_TIMEOUT:g}s",
            }
        except Exception as exc:  # noqa: BLE001 — the error IS the result
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ms = round((time.perf_counter() - started) * 1000, 1)
        log.info(
            "meta_execute",
            backend=backend,
            tool=tool,
            ms=ms,
            is_error=bool(res.isError),
        )
        return {
            "ok": True,
            "is_error": bool(res.isError),
            "ms": ms,
            "content": [
                blk.model_dump(mode="json", exclude_none=True)
                for blk in (res.content or [])
            ],
            "structured": res.structuredContent,
        }

    return server
