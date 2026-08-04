"""First-class gateway-owned Virtual Tools served at ``/virtual/mcp``.

Definitions bind to stable backend IDs plus original tool/parameter identities.
Every call resolves those bindings through the current overrides, dispatches via
the live per-backend proxies, and returns a fidelity-preserving aggregate.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from fastmcp import Client, FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.providers.proxy import FastMCPProxy
from fastmcp.tools import Tool, ToolResult
from mcp.types import TextContent
from pydantic import PrivateAttr

from mcp_gateway import __version__
from mcp_gateway import config_loader as cl

VIRTUAL_ROUTE = "virtual"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_OMISSION_DETAIL_LIMIT = 2

# This is intentionally an envelope schema rather than a schema for every
# upstream result.  Virtual members can proxy arbitrary MCP servers, so their
# individual structured results and execution metadata cannot be constrained
# safely by the gateway.  The stable top-level fields let clients rely on the
# virtual-tool contract while ``additionalProperties`` preserves backend
# fidelity as the envelope evolves.
VIRTUAL_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "virtual_tool",
        "dispatch",
        "selected",
        "selected_omitted",
        "members",
        "budget",
    ],
    "properties": {
        "virtual_tool": {"type": "string"},
        "dispatch": {"type": "string"},
        "selected": {"type": "array", "items": {"type": "string"}},
        "selected_omitted": {"type": "integer", "minimum": 0},
        "members": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
        "budget": {"type": "object", "additionalProperties": True},
    },
    "additionalProperties": True,
}

_JSON_TYPES: dict[str, str | list[str]] = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
}


def stable_backend_id(backend: cl.Backend) -> str:
    """Return the persisted ID or the deterministic legacy migration value."""
    return backend.id or str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"mcp-gateway/backend/{backend.name}")
    )


def ensure_backend_ids(cfg: cl.GatewayConfig) -> None:
    """Materialize IDs before the first stable reference is persisted."""
    for backend in cfg.backends:
        if backend.id is None:
            backend.id = stable_backend_id(backend)


def member_label(member: cl.VirtualMember) -> str:
    return member.label or f"{member.backend_id}/{member.tool_original}"


def _backend(cfg: cl.GatewayConfig, backend_id: str) -> cl.Backend | None:
    return next((item for item in cfg.backends if item.id == backend_id), None)


def resolve_member(
    member: cl.VirtualMember, cfg: cl.GatewayConfig
) -> tuple[cl.Backend, str, dict[str, str]]:
    """Resolve stable original identities to the current exposed names."""
    backend = _backend(cfg, member.backend_id)
    if backend is None:
        raise cl.ConfigError(f"unknown backend id {member.backend_id!r}")
    if not backend.enabled:
        raise cl.ConfigError(f"backend {backend.name!r} is disabled")
    override = next(
        (tool for tool in backend.tools if tool.original == member.tool_original), None
    )
    if override is not None and not override.enabled:
        raise cl.ConfigError(f"tool {backend.name}/{member.tool_original} is disabled")
    tool_name = override.name if override and override.name else member.tool_original
    param_names: dict[str, str] = {}
    if override is not None:
        for param in override.params:
            if param.hide and (
                param.original in member.args or param.original in member.static_args
            ):
                raise cl.ConfigError(
                    f"member {backend.name}/{member.tool_original} references hidden "
                    f"parameter {param.original!r}"
                )
            param_names[param.original] = param.name or param.original
    for original in set(member.args) | set(member.static_args):
        param_names.setdefault(original, original)
    return backend, tool_name, param_names


async def resolve_tool(
    tool: cl.VirtualTool,
    cfg: cl.GatewayConfig,
    registry: Mapping[str, FastMCPProxy],
) -> dict:
    """Return a live resolution receipt; no member call is made."""
    members = []
    errors: list[str] = []
    for member in tool.members:
        label = member_label(member)
        try:
            backend, effective_tool, param_names = resolve_member(member, cfg)
            proxy = registry.get(backend.name)
            if proxy is None:
                raise cl.ConfigError(f"backend {backend.name!r} is not mounted")
            async with asyncio.timeout(member.timeout):
                async with Client(proxy) as client:
                    listed = await client.list_tools()
            live = next((item for item in listed if item.name == effective_tool), None)
            if live is None:
                raise cl.ConfigError(
                    f"source tool {backend.name}/{member.tool_original} no longer "
                    f"resolves to a live exposed tool ({effective_tool!r})"
                )
            properties = (live.inputSchema or {}).get("properties", {})
            missing = [
                value for value in param_names.values() if value not in properties
            ]
            if missing:
                raise cl.ConfigError(
                    f"source parameter(s) no longer resolve: {sorted(missing)}"
                )
            members.append(
                {
                    "label": label,
                    "resolved": True,
                    "backend_id": member.backend_id,
                    "backend": backend.name,
                    "backend_effective": backend.display_name or backend.name,
                    "tool_original": member.tool_original,
                    "tool_effective": effective_tool,
                    "params_effective": param_names,
                }
            )
        except Exception as exc:  # noqa: BLE001 - resolution receipt
            error = f"{label}: {type(exc).__name__}: {exc}"
            errors.append(error)
            members.append(
                {
                    "label": label,
                    "resolved": False,
                    "backend_id": member.backend_id,
                    "tool_original": member.tool_original,
                    "error": str(exc),
                }
            )
    return {"ok": not errors, "members": members, "errors": errors}


def _fallback(tool: cl.VirtualTool) -> list[cl.VirtualMember]:
    fallback = tool.router.fallback if tool.router else "all"
    if fallback == "all":
        return list(tool.members)
    return [member for member in tool.members if member_label(member) == fallback]


def _route_text(tool: cl.VirtualTool, arguments: dict) -> str:
    """Bound local regex work and the caller data sent to an LLM router."""
    return " ".join(str(value) for value in arguments.values())[
        : tool.routing_input_max_chars
    ]


async def _llm_members(
    tool: cl.VirtualTool, arguments: dict, log
) -> list[cl.VirtualMember]:
    router = tool.router
    assert router is not None and router.api_key  # validated by config model
    lines = [
        f"Route a call for {tool.name!r}: {tool.description}",
        "Members (label: condition):",
    ]
    for member in tool.members:
        lines.append(
            f'- "{member_label(member)}": '
            f"{member.route_description or '(no condition supplied)'}"
        )
    if router.conditions:
        lines.append(router.conditions)
    lines.extend(
        [
            f"Routing input (bounded): {_route_text(tool, arguments)}",
            "Reply only with a JSON array of one or more member labels.",
        ]
    )
    try:
        key = cl.expand_env(router.api_key)
        async with httpx.AsyncClient(timeout=router.timeout) as client:
            response = await client.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": router.model,
                    "temperature": 0,
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": "\n".join(lines)}],
                },
            )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        start, end = content.find("["), content.rfind("]")
        labels = json.loads(content[start : end + 1])
        if not isinstance(labels, list) or not all(
            isinstance(value, str) for value in labels
        ):
            raise ValueError("router response is not a string array")
        wanted = set(labels)
        chosen = [member for member in tool.members if member_label(member) in wanted]
        if not chosen:
            raise ValueError("router selected no known member")
        return chosen
    except Exception as exc:  # noqa: BLE001 - specified local fallback
        log.warning(
            "virtual_route_fallback",
            virtual_tool=tool.name,
            dispatch="llm",
            error=f"{type(exc).__name__}: {exc}",
        )
        return _fallback(tool)


async def select_members(
    tool: cl.VirtualTool, arguments: dict, log
) -> list[cl.VirtualMember]:
    if tool.dispatch == "all":
        return list(tool.members)
    if tool.dispatch == "keyword":
        text = _route_text(tool, arguments)
        selected = [
            member
            for member in tool.members
            if any(
                re.search(pattern, text, re.IGNORECASE)
                for pattern in member.route_patterns
            )
        ]
        return selected or _fallback(tool)
    return await _llm_members(tool, arguments, log)


def _validate_arguments(tool: cl.VirtualTool, arguments: dict) -> dict:
    declared = {item.name: item for item in tool.inputs}
    unknown = set(arguments) - set(declared)
    if unknown:
        raise ValueError(f"unknown input(s): {sorted(unknown)}")
    values = dict(arguments)
    for item in tool.inputs:
        if item.name not in values:
            if item.required:
                raise ValueError(f"missing required input {item.name!r}")
            if item.default is not None:
                values[item.name] = item.default
            continue
        value = values[item.name]
        valid = {
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, int | float) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
        }[item.type]
        if not valid:
            raise ValueError(
                f"input {item.name!r} must be {item.type}, got {type(value).__name__}"
            )
    return values


async def call_member(
    member: cl.VirtualMember,
    arguments: dict,
    cfg: cl.GatewayConfig,
    registry: Mapping[str, FastMCPProxy],
) -> dict:
    label = member_label(member)
    started = time.perf_counter()
    try:
        backend, tool_name, param_names = resolve_member(member, cfg)
        proxy = registry.get(backend.name)
        if proxy is None:
            raise cl.ConfigError(f"backend {backend.name!r} is not mounted")
        call_args = {
            param_names[original]: arguments[source]
            for original, source in member.args.items()
            if source in arguments
        }
        call_args.update(
            {
                param_names[original]: value
                for original, value in member.static_args.items()
            }
        )
        async with asyncio.timeout(member.timeout):
            async with Client(proxy) as client:
                result = await client.call_tool_mcp(tool_name, call_args)
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        if result.isError:
            text = "\n".join(
                getattr(block, "text", "") for block in result.content or []
            ).strip()
            return {
                "member": label,
                "status": "error",
                "ms": elapsed,
                "error": text or "tool returned an error",
                "content": list(result.content or []),
                "structured": result.structuredContent,
                "meta": result.meta,
            }
        return {
            "member": label,
            "status": "ok",
            "ms": elapsed,
            "content": list(result.content or []),
            "structured": result.structuredContent,
            "meta": result.meta,
        }
    except TimeoutError:
        return {
            "member": label,
            "status": "timeout",
            "error": f"no result within {member.timeout:g}s",
        }
    except Exception as exc:  # noqa: BLE001 - member isolation is the contract
        return {
            "member": label,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _json_size(value: Any) -> int:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return len(
        json.dumps(
            value, ensure_ascii=False, default=str, separators=(",", ":")
        ).encode("utf-8")
    )


def _clip(value: Any, limit: int = 128) -> str:
    """A deterministic summary for the bounded aggregate envelope."""
    text = str(value)
    return text if len(text) <= limit else f"{text[: max(0, limit - 1)]}…"


def _aggregate_result(  # noqa: PLR0913, PLR0917 - exact wire-result accounting inputs
    tool: cl.VirtualTool,
    content: list,
    members: list[dict],
    selected: list[str],
    omitted: list[dict],
    omitted_count: int,
    omitted_bytes: int,
    accounted_bytes: int,
) -> ToolResult:
    """Build the exact wire result whose serialized size the caller checks."""
    budget = {
        "limit_bytes": tool.max_result_bytes,
        "accounted_bytes": accounted_bytes,
        "omitted": omitted,
        "omitted_count": omitted_count,
        "omitted_bytes": omitted_bytes,
    }
    visible_content = list(content)
    if omitted_count:
        visible_content.append(
            TextContent(
                type="text",
                text=(
                    f"[mcp-gateway: output budget {tool.max_result_bytes} bytes; "
                    f"omitted {omitted_count} item(s)]"
                ),
            )
        )
    visible_selected = [_clip(label, 96) for label in selected[:8]]
    envelope = {
        "virtual_tool": _clip(tool.name, 64),
        "dispatch": tool.dispatch,
        "selected": visible_selected,
        "selected_omitted": max(0, len(selected) - len(visible_selected)),
        "members": members,
        "budget": budget,
    }
    return ToolResult(
        content=visible_content,
        structured_content=envelope,
        meta={"mcp-gateway/virtual": budget},
    )


def aggregate_results(  # noqa: PLR0912, PLR0915 - one ordered budget-accounting pass
    tool: cl.VirtualTool, outcomes: list[dict], selected: list[str]
) -> ToolResult:
    """Preserve results while strictly bounding the final serialized MCP value."""
    successes = sum(item["status"] == "ok" for item in outcomes)
    is_error = successes == 0 or (
        tool.failure_policy == "strict" and successes != len(outcomes)
    )
    content: list = []
    structured_members: list[dict] = []
    used = 0
    omitted: list[dict] = []
    omitted_count = 0
    omitted_bytes = 0

    def result() -> ToolResult:
        current = _aggregate_result(
            tool,
            content,
            structured_members,
            selected,
            omitted,
            omitted_count,
            omitted_bytes,
            used,
        )
        current.is_error = is_error
        return current

    def fits_with_first_omission() -> bool:
        if omitted_count:
            return _json_size(result()) <= tool.max_result_bytes
        reserve = [{"member": "x" * 32, "kind": "content", "index": 0, "bytes": 0}]
        trial = _aggregate_result(
            tool,
            content,
            structured_members,
            selected,
            reserve,
            1,
            0,
            used,
        )
        return _json_size(trial) <= tool.max_result_bytes

    def omit(member: str, kind: str, size: int, index: int | None = None) -> None:
        nonlocal omitted_count, omitted_bytes
        omitted_count += 1
        omitted_bytes += size
        if len(omitted) < _OMISSION_DETAIL_LIMIT:
            detail = {"member": _clip(member, 32), "kind": kind, "bytes": size}
            if index is not None:
                detail["index"] = index
            omitted.append(detail)

    for outcome in outcomes:
        status = outcome["status"]
        member = _clip(outcome["member"], 96)
        header = TextContent(
            type="text",
            text=(
                f"## {member} — {status}"
                + (f" ({outcome['ms']} ms)" if "ms" in outcome else "")
                + (f"\n{_clip(outcome['error'])}" if outcome.get("error") else "")
            ),
        )
        record = {"member": member, "status": status}
        if "ms" in outcome:
            record["ms"] = outcome["ms"]
        if outcome.get("error"):
            record["error"] = _clip(outcome["error"])
        structured_members.append(record)
        if not fits_with_first_omission():
            structured_members.pop()
            omit(member, "member", _json_size(record))

        for index, block in enumerate([header, *outcome.get("content", [])]):
            size = _json_size(block)
            content.append(block)
            used += size
            if fits_with_first_omission():
                continue
            content.pop()
            used -= size
            omit(member, "content", size, index)

        if outcome.get("structured") is not None:
            structured_size = _json_size(outcome["structured"])
            if record not in structured_members:
                omit(member, "structured", structured_size)
            else:
                record["result"] = outcome["structured"]
                if fits_with_first_omission():
                    used += structured_size
                else:
                    record.pop("result")
                    omit(member, "structured", structured_size)

        # Upstream ``_meta`` remains inside its member's result record so two
        # backends cannot collide.  It is subject to the same exact wire-size
        # accounting as content and structured results.
        if outcome.get("meta") is not None:
            metadata_size = _json_size(outcome["meta"])
            if record not in structured_members:
                omit(member, "meta", metadata_size)
            else:
                record["meta"] = outcome["meta"]
                if fits_with_first_omission():
                    used += metadata_size
                else:
                    record.pop("meta")
                    omit(member, "meta", metadata_size)

    while _json_size(result()) > tool.max_result_bytes and structured_members:
        dropped = structured_members.pop()
        omit(dropped.get("member", "unknown"), "member", _json_size(dropped))
    while _json_size(result()) > tool.max_result_bytes and content:
        dropped = content.pop()
        omit("aggregate", "content", _json_size(dropped))
    final = result()
    if _json_size(final) > tool.max_result_bytes:
        # >=1KiB is model-enforced; this compact fallback keeps the marker and
        # bounded omission metadata instead of returning a silent empty success.
        omitted_count += 1
        final = _aggregate_result(
            tool,
            [],
            [],
            [],
            [{"kind": item["kind"]} for item in omitted[:_OMISSION_DETAIL_LIMIT]],
            omitted_count,
            omitted_bytes,
            0,
        )
        final.is_error = is_error
    if _json_size(final) > tool.max_result_bytes:
        final = _aggregate_result(tool, [], [], [], [], omitted_count, omitted_bytes, 0)
        final.is_error = is_error
    if _json_size(final) > tool.max_result_bytes:
        raise cl.ConfigError("virtual-tool aggregate metadata exceeds output budget")
    return final


async def run_virtual(
    tool: cl.VirtualTool,
    arguments: dict,
    cfg: cl.GatewayConfig,
    registry: Mapping[str, FastMCPProxy],
    log,
) -> ToolResult:
    values = _validate_arguments(tool, arguments)
    members = await select_members(tool, values, log)
    outcomes = list(
        await asyncio.gather(
            *(call_member(member, values, cfg, registry) for member in members)
        )
    )
    log.info(
        "virtual_tool_call",
        virtual_tool=tool.name,
        dispatch=tool.dispatch,
        selected=[member_label(member) for member in members],
        ok=sum(item["status"] == "ok" for item in outcomes),
    )
    return aggregate_results(
        tool, outcomes, [member_label(member) for member in members]
    )


def input_schema(tool: cl.VirtualTool) -> dict:
    properties: dict[str, dict] = {}
    required = []
    for item in tool.inputs:
        schema: dict = {"type": _JSON_TYPES[item.type]}
        if item.description:
            schema["description"] = item.description
        if item.default is not None:
            schema["default"] = item.default
        properties[item.name] = schema
        if item.required:
            required.append(item.name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


class _VirtualRuntimeTool(Tool):
    """A Tool whose public JSON names are independent of Python identifiers."""

    _definition: cl.VirtualTool = PrivateAttr()
    _cfg_source: cl.GatewayConfig | Callable[[], cl.GatewayConfig] = PrivateAttr()
    _registry: Mapping[str, FastMCPProxy] = PrivateAttr()
    _log: Any = PrivateAttr()
    _status_store: dict | None = PrivateAttr()

    def __init__(
        self,
        definition: cl.VirtualTool,
        cfg_source: cl.GatewayConfig | Callable[[], cl.GatewayConfig],
        registry: Mapping[str, FastMCPProxy],
        log,
        status_store: dict | None = None,
    ) -> None:
        super().__init__(
            name=definition.name,
            description=definition.description,
            parameters=input_schema(definition),
            output_schema=VIRTUAL_OUTPUT_SCHEMA,
            meta=(dict(cl.ALWAYS_LOAD_META) if definition.always_load else None),
        )
        self._definition = definition
        self._cfg_source = cfg_source
        self._registry = registry
        self._log = log
        self._status_store = status_store

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        cfg = self._cfg_source() if callable(self._cfg_source) else self._cfg_source
        current = next(
            (item for item in cfg.virtual_tools if item.name == self._definition.name),
            self._definition,
        )
        started = time.perf_counter()
        try:
            result = await run_virtual(
                current, arguments, cfg, self._registry, self._log
            )
        except Exception as exc:
            if self._status_store is not None:
                self._status_store.setdefault(current.name, {})["last_dispatch"] = {
                    "ok": False,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                }
            raise
        if self._status_store is not None:
            self._status_store.setdefault(current.name, {})["last_dispatch"] = {
                "ok": not result.is_error,
                "status": "passed" if not result.is_error else "failed",
                "ms": round((time.perf_counter() - started) * 1000, 1),
                "selected": result.structured_content.get("selected", []),
            }
        return result


def build_virtual_tool(
    tool: cl.VirtualTool,
    cfg_source: cl.GatewayConfig | Callable[[], cl.GatewayConfig],
    registry: Mapping[str, FastMCPProxy],
    log,
    status_store: dict | None = None,
) -> Tool:
    """Build without translating public input names into Python identifiers."""
    return _VirtualRuntimeTool(tool, cfg_source, registry, log, status_store)


def replace_tools(  # noqa: PLR0913, PLR0917 - explicit lifecycle dependencies
    server: FastMCP,
    cfg: cl.GatewayConfig,
    cfg_source: cl.GatewayConfig | Callable[[], cl.GatewayConfig],
    registry: Mapping[str, FastMCPProxy],
    log,
    status_store: dict | None = None,
) -> None:
    """Stage every component before one provider-map swap can mutate live tools."""
    staged = FastMCP(
        name=f"{server.name}-staging", list_page_size=cl.DOWNSTREAM_TOOLS_PAGE_SIZE
    )
    for item in [
        build_virtual_tool(tool, cfg_source, registry, log, status_store)
        for tool in cfg.virtual_tools
        if tool.enabled
    ]:
        staged.add_tool(item)
    server.local_provider._components = staged.local_provider._components  # noqa: SLF001


def build_virtual_server(  # noqa: PLR0913, PLR0917 - explicit lifecycle and auth dependencies
    cfg: cl.GatewayConfig,
    cfg_source: cl.GatewayConfig | Callable[[], cl.GatewayConfig],
    registry: Mapping[str, FastMCPProxy],
    log,
    status_store: dict | None = None,
    auth: AuthProvider | None = None,
) -> FastMCP:
    server = FastMCP(
        name="mcp-gateway-virtual",
        version=__version__,
        dereference_schemas=False,
        list_page_size=cl.DOWNSTREAM_TOOLS_PAGE_SIZE,
        auth=auth,
    )
    replace_tools(server, cfg, cfg_source, registry, log, status_store)
    return server
