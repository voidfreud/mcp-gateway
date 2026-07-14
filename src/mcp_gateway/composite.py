"""Composite tools (#14, spec §17.4): synthetic multi-backend tools.

A composite is a config-defined tool the gateway itself serves: one exposed
name/description/param schema, and a list of MEMBER tools (backend + exposed
tool name + arg mapping). A call fans out to the selected members
CONCURRENTLY, each bounded by its own timeout, and merges the results into one
labeled response. A failed/timed-out member reports itself inside the merge
instead of sinking the call; only all-members-failed raises a tool error.

All composites are served together on one FastMCP server mounted at
``/composite/mcp`` (its own runner task in the server lifespan — the existing
per-backend mounts are untouched). Member calls go through the gateway's LIVE
per-backend proxies (the ``registry``), the same in-process path as the admin
mini-inspector: every override/rename applies, and warm vs stateless session
semantics are whatever the member's backend mount already does. Member ``tool``
names are therefore the EXPOSED (post-transform) names.

The dispatch seam for smart routing (#21): member selection is a pluggable
STRATEGY (``select_members``). ``"all"`` — today's only strategy — returns
every member; a router strategy can later pick a per-call SUBSET (it receives
the composite AND the call args) without touching the fan-out/merge machinery.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from typing import Annotated, Any

from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from pydantic import Field

from mcp_gateway.config_loader import (
    ALWAYS_LOAD_META,
    COMPOSITE_ROUTE,
    Composite,
    CompositeMember,
    GatewayConfig,
)

__all__ = [
    "COMPOSITE_ROUTE",
    "STRATEGIES",
    "build_composite_server",
    "build_composite_tool",
    "call_member",
    "member_label",
    "merge_results",
    "run_composite",
    "select_members",
]

_PARAM_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


# ---------------------------------------------------------------------------
# Member selection — the #21 dispatch seam
# ---------------------------------------------------------------------------


def _strategy_all(comp: Composite, _args: dict) -> list[CompositeMember]:
    """Fan out to every member (v1 behaviour)."""
    return list(comp.members)


# strategy name -> (composite, call args) -> members to dispatch to. Smart
# routing (#21) registers here: a router strategy returns a per-call SUBSET.
STRATEGIES: dict[str, Callable[[Composite, dict], list[CompositeMember]]] = {
    "all": _strategy_all,
}


def select_members(comp: Composite, args: dict) -> list[CompositeMember]:
    """The members this call dispatches to, per the composite's strategy."""
    strategy = STRATEGIES.get(comp.strategy)
    if strategy is None:  # unreachable via config (Literal), guards drift
        raise ToolError(f"composite {comp.name!r}: unknown strategy {comp.strategy!r}")
    return strategy(comp, args)


# ---------------------------------------------------------------------------
# Fan-out + merge
# ---------------------------------------------------------------------------


def member_label(m: CompositeMember) -> str:
    return m.label or f"{m.backend}/{m.tool}"


def _member_args(m: CompositeMember, args: dict) -> dict:
    """The member-tool call args: mapped composite params + injected statics.

    An optional composite param Claude omitted is simply not forwarded (the
    member tool's own default applies) — hence the ``in args`` guard.
    """
    call = {mp: args[cp] for mp, cp in m.args.items() if cp in args}
    call.update(m.static_args)
    return call


async def call_member(m: CompositeMember, args: dict, registry: dict) -> dict:
    """Call ONE member through its live proxy; never raises.

    Returns ``{member, status, ...}`` where status is ``ok`` (+ ms, text),
    ``timeout``, or ``error`` (+ error text) — the honest per-member outcome
    the merge reports, so one dead member can't sink the composite call.
    """
    label = member_label(m)
    proxy = registry.get(m.backend)
    if proxy is None:
        return {
            "member": label,
            "status": "error",
            "error": f"backend {m.backend!r} is not mounted",
        }
    started = time.perf_counter()
    try:
        async with asyncio.timeout(m.timeout):
            async with Client(proxy) as c:
                res = await c.call_tool_mcp(m.tool, _member_args(m, args))
    except TimeoutError:
        return {
            "member": label,
            "status": "timeout",
            "error": f"no result within {m.timeout:g}s",
        }
    except Exception as exc:  # noqa: BLE001 — the error IS the member result
        return {
            "member": label,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }
    ms = round((time.perf_counter() - started) * 1000, 1)
    text = "\n".join(
        t for t in (getattr(blk, "text", None) for blk in (res.content or [])) if t
    )
    if res.isError:  # call_tool_mcp doesn't raise on tool-level errors
        return {
            "member": label,
            "status": "error",
            "ms": ms,
            "error": text or "tool returned an error",
        }
    return {"member": label, "status": "ok", "ms": ms, "text": text}


def merge_results(results: list[dict]) -> str:
    """v1 merge: labeled concatenation, one section per member, honest status."""
    parts = []
    for r in results:
        head = f"## {r['member']} — {r['status']}"
        if "ms" in r:
            head += f" ({r['ms']} ms)"
        body = r.get("text") if r["status"] == "ok" else r.get("error")
        parts.append(f"{head}\n{body or '(empty result)'}")
    return "\n\n".join(parts)


async def run_composite(comp: Composite, args: dict, registry: dict, log) -> str:
    """Select members (strategy seam), fan out concurrently, merge.

    Raises :class:`ToolError` only when EVERY member failed — a partial
    failure returns the merge with the failures labeled inside it.
    """
    members = select_members(comp, args)
    results = list(
        await asyncio.gather(*(call_member(m, args, registry) for m in members))
    )
    ok = sum(1 for r in results if r["status"] == "ok")
    log.info(
        "composite_call",
        composite=comp.name,
        strategy=comp.strategy,
        members=len(results),
        ok=ok,
    )
    merged = merge_results(results)
    if results and ok == 0:
        raise ToolError(f"all {len(results)} member(s) failed:\n\n{merged}")
    return merged


# ---------------------------------------------------------------------------
# Tool + server build
# ---------------------------------------------------------------------------


def build_composite_tool(comp: Composite, registry: dict, log) -> Tool:
    """Build the FastMCP tool for ONE composite.

    The exposed param schema is synthesized from the config's
    ``CompositeParam`` list by stamping a matching ``__signature__`` onto a
    generic async impl — ``Tool.from_function`` derives the JSON schema from
    the signature (verified against FastMCP 3.4.4).
    """

    async def impl(**kwargs: Any) -> str:
        return await run_composite(comp, kwargs, registry, log)

    sig_params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for p in comp.params:
        base = _PARAM_TYPES[p.type]
        if not p.required and p.default is None:
            # An optional param with no authored default carries default None
            # in the signature — the type must admit null or the emitted
            # schema contradicts its own default.
            base = base | None
        ann = (
            Annotated[base, Field(description=p.description)] if p.description else base
        )
        default = inspect.Parameter.empty if p.required else p.default
        sig_params.append(
            inspect.Parameter(
                p.name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=ann,
                default=default,
            )
        )
        annotations[p.name] = ann
    impl.__signature__ = inspect.Signature(sig_params)  # type: ignore[attr-defined]
    impl.__annotations__ = {**annotations, "return": str}
    impl.__name__ = comp.name
    return Tool.from_function(
        impl,
        name=comp.name,
        description=comp.description,
        meta=dict(ALWAYS_LOAD_META) if comp.always_load else None,
    )


def build_composite_server(cfg: GatewayConfig, registry: dict, log) -> FastMCP:
    """One FastMCP server carrying every ENABLED composite's tool.

    *registry* is the server lifespan's live ``{backend name -> proxy}`` dict,
    shared BY REFERENCE — member calls resolve the proxy at call time, so a
    backend that mounts (or recycles) later is picked up automatically.
    """
    server = FastMCP(name="mcp-gateway-composite")
    for comp in cfg.composites:
        if comp.enabled:
            server.add_tool(build_composite_tool(comp, registry, log))
    return server
