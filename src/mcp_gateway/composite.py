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
STRATEGY (``select_members``). ``"all"`` fans out to every member; ``"keyword"``
matches per-member ``route_patterns`` regexes against the call's arg text
(free, instant); ``"llm"`` asks a cheap OpenRouter model to pick a member
subset. Routing is BEST-EFFORT by design: a router outage, timeout, or garbage
reply falls back to the configured ``router.fallback`` (default: all members) —
it must never break the call.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from pydantic import Field

from mcp_gateway.config_loader import (
    ALWAYS_LOAD_META,
    COMPOSITE_ROUTE,
    Composite,
    CompositeMember,
    CompositeRouter,
    GatewayConfig,
    expand_env,
)

__all__ = [
    "COMPOSITE_ROUTE",
    "OPENROUTER_URL",
    "STRATEGIES",
    "RouteContext",
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
# Member selection — the #21 dispatch seam (smart routing)
# ---------------------------------------------------------------------------

# OpenRouter's OpenAI-compatible chat endpoint — the "llm" strategy's router.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


@dataclass
class RouteContext:
    """Boot-resolved inputs a strategy may need beyond (composite, args).

    ``api_key`` is the RESOLVED OpenRouter key (``${ENV}`` expanded once in
    :func:`build_composite_tool`, like ``bearer_token`` — never per call).
    """

    api_key: str | None = None
    log: Any = None


def _fallback_members(comp: Composite) -> list[CompositeMember]:
    """Where a call goes when routing decides nothing (or the router fails):
    ``router.fallback`` — every member (``"all"``, the default) or the one
    member whose label matches (validated to exist at config load)."""
    fb = comp.router.fallback if comp.router else "all"
    if fb == "all":
        return list(comp.members)
    return [m for m in comp.members if member_label(m) == fb]


def _route_text(args: dict) -> str:
    """The text routing looks at: every supplied arg value, stringified."""
    return " ".join(str(v) for v in args.values())


def _strategy_all(
    comp: Composite, _args: dict, _ctx: RouteContext
) -> list[CompositeMember]:
    """Fan out to every member (v1 behaviour, the default)."""
    return list(comp.members)


def _strategy_keyword(
    comp: Composite, args: dict, ctx: RouteContext
) -> list[CompositeMember]:
    """Free, instant heuristic: a member is selected when ANY of its
    ``route_patterns`` regexes matches the call's arg text (case-insensitive
    search). No member matched -> the configured fallback."""
    text = _route_text(args)
    hits = [
        m
        for m in comp.members
        if any(re.search(p, text, re.IGNORECASE) for p in m.route_patterns)
    ]
    if hits:
        return hits
    if ctx.log:
        ctx.log.info(
            "composite_route_fallback",
            composite=comp.name,
            strategy="keyword",
            reason="no route_pattern matched",
        )
    return _fallback_members(comp)


def _router_prompt(comp: Composite, args: dict) -> str:
    """The routing question: member labels + their routing conditions, any
    composite-level policy text, and the call args. Answer contract: a bare
    JSON array of member labels."""
    lines = [
        f"You route calls for the tool {comp.name!r}: {comp.description}",
        "",
        "Members (label: when to route to it):",
    ]
    for m in comp.members:
        cond = m.route_description or "(no routing description)"
        lines.append(f'- "{member_label(m)}": {cond}')
    if comp.router and comp.router.conditions:
        lines += ["", comp.router.conditions]
    lines += [
        "",
        "Call arguments (JSON):",
        json.dumps(args, default=str),
        "",
        "Reply with ONLY a JSON array of the member labels that should "
        'receive this call, e.g. ["label-a", "label-b"]. Pick at least one.',
    ]
    return "\n".join(lines)


async def _post_router(router: CompositeRouter, api_key: str, prompt: str) -> str:
    """One routing request to OpenRouter; returns the message content.

    Factored out so tests mock the HTTP layer here — never call OpenRouter
    for real in tests. Raises on any HTTP/shape problem; the caller's
    fallback handles it.
    """
    async with httpx.AsyncClient(timeout=router.timeout) as client:
        resp = await client.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": router.model,
                "temperature": 0,
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _parse_router_reply(content: str) -> list[str]:
    """Strict small parse: the reply must carry ONE JSON array of strings
    (surrounding prose/code fences tolerated; anything else raises)."""
    start, end = content.find("["), content.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("no JSON array in router reply")
    labels = json.loads(content[start : end + 1])
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise ValueError("router reply is not a JSON array of strings")
    return labels


async def _strategy_llm(
    comp: Composite, args: dict, ctx: RouteContext
) -> list[CompositeMember]:
    """OpenRouter-backed router: a cheap model reads the call args and each
    member's routing condition, replies with a JSON array of member labels.

    Best-effort BY CONTRACT: timeout, HTTP error, garbage reply, or an
    unknown-label reply all fall back to ``router.fallback`` — a router
    outage must never break the composite call.
    """
    router = comp.router
    assert router is not None and ctx.api_key  # noqa: S101 — enforced at config load + boot
    try:
        prompt = _router_prompt(comp, args)
        async with asyncio.timeout(router.timeout):
            content = await _post_router(router, ctx.api_key, prompt)
        labels = set(_parse_router_reply(content))
        chosen = [m for m in comp.members if member_label(m) in labels]
        if not chosen:
            raise ValueError(f"router chose no known member: {sorted(labels)}")
    except Exception as exc:  # noqa: BLE001 — ANY router failure falls back
        if ctx.log:
            ctx.log.warning(
                "composite_route_fallback",
                composite=comp.name,
                strategy="llm",
                error=f"{type(exc).__name__}: {exc}",
            )
        return _fallback_members(comp)
    if ctx.log:
        ctx.log.info(
            "composite_route",
            composite=comp.name,
            strategy="llm",
            chosen=[member_label(m) for m in chosen],
        )
    return chosen


# strategy name -> (composite, call args, context) -> members to dispatch to
# (or an awaitable of them). Registering here is the whole plug-in surface.
STRATEGIES: dict[
    str,
    Callable[
        [Composite, dict, RouteContext],
        list[CompositeMember] | Awaitable[list[CompositeMember]],
    ],
] = {
    "all": _strategy_all,
    "keyword": _strategy_keyword,
    "llm": _strategy_llm,
}


async def select_members(
    comp: Composite, args: dict, ctx: RouteContext | None = None
) -> list[CompositeMember]:
    """The members this call dispatches to, per the composite's strategy."""
    strategy = STRATEGIES.get(comp.strategy)
    if strategy is None:  # unreachable via config (Literal), guards drift
        raise ToolError(f"composite {comp.name!r}: unknown strategy {comp.strategy!r}")
    selected = strategy(comp, args, ctx or RouteContext())
    if inspect.isawaitable(selected):
        selected = await selected
    return selected


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


async def run_composite(
    comp: Composite, args: dict, registry: dict, log, api_key: str | None = None
) -> str:
    """Select members (strategy seam), fan out concurrently, merge.

    Raises :class:`ToolError` only when EVERY member failed — a partial
    failure returns the merge with the failures labeled inside it.
    """
    members = await select_members(comp, args, RouteContext(api_key=api_key, log=log))
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

    # #21: the "llm" strategy's OpenRouter key is a ${ENV} reference resolved
    # ONCE here — at boot (or admin rebuild), like bearer_token. A missing
    # secret fails the mount loudly instead of failing every routed call.
    api_key = (
        expand_env(comp.router.api_key)
        if comp.strategy == "llm" and comp.router and comp.router.api_key
        else None
    )

    async def impl(**kwargs: Any) -> str:
        return await run_composite(comp, kwargs, registry, log, api_key=api_key)

    sig_params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for p in comp.params:
        base = _PARAM_TYPES[p.type]
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
        if not comp.enabled:
            continue
        # Per-composite isolation (mirrors the per-backend rule, #61): one
        # broken composite — typically an "llm" router whose ${ENV} secret no
        # longer resolves — must not sink its siblings or the whole mount.
        try:
            server.add_tool(build_composite_tool(comp, registry, log))
        except Exception as exc:
            log.error("composite_build_failed", composite=comp.name, error=str(exc))
    return server
