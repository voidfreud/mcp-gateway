#!/usr/bin/env python3
"""Controlled loopback MCP backend for the Virtual Tools black-box harness."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import (
    AudioContent,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
    TextResourceContents,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def build_app(name: str, event_file: Path) -> Starlette:
    """Build one fixture MCP app plus a loopback-only control API."""
    state: dict[str, Any] = {
        "delay": 0.0,
        "fail": False,
        "result_mode": "text",
        "events": [],
    }
    mcp = FastMCP(name=f"virtual-tools-fixture-{name}")

    def record(kind: str, **fields: Any) -> None:
        item = {"at": time.time(), "backend": name, "kind": kind, **fields}
        state["events"].append(item)
        with event_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, sort_keys=True) + "\n")

    @mcp.tool(name="source_search")
    async def source_search(query: str, count: int = 3) -> str | ToolResult:
        """Return controlled text or every relevant MCP result-block type."""
        plan = dict(state)
        record("call_started", args={"query": query, "count": count})
        try:
            if plan["delay"]:
                await asyncio.sleep(plan["delay"])
            if plan["fail"]:
                raise ValueError(f"{name} forced failure")
            if plan["result_mode"] == "large":
                return f"{name} large result: " + ("x" * 8192)
            if plan["result_mode"] == "rich":
                return ToolResult(
                    content=[
                        TextContent(type="text", text=f"{name} rich text"),
                        ImageContent(
                            type="image",
                            data="iVBORw0KGgo=",
                            mimeType="image/png",
                        ),
                        AudioContent(
                            type="audio",
                            data="UklGRg==",
                            mimeType="audio/wav",
                        ),
                        EmbeddedResource(
                            type="resource",
                            resource=TextResourceContents(
                                uri=f"fixture://{name}/result",
                                mimeType="text/plain",
                                text=f"{name} embedded resource",
                            ),
                        ),
                        ResourceLink(
                            type="resource_link",
                            uri=f"fixture://{name}/link",
                            name=f"{name} link",
                            description="fixture resource link",
                            mimeType="text/plain",
                        ),
                    ],
                    structured_content={"backend": name, "query": query},
                )
            return f"{name} result query={query!r} count={count}"
        finally:
            record("call_finished")

    async def plan(request: Request) -> JSONResponse:
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "body must be an object"}, 400)
        if "delay" in body and (
            not isinstance(body["delay"], (int, float)) or body["delay"] < 0
        ):
            return JSONResponse({"ok": False, "error": "delay must be >= 0"}, 400)
        if "fail" in body and not isinstance(body["fail"], bool):
            return JSONResponse({"ok": False, "error": "fail must be boolean"}, 400)
        if body.get("result_mode", "text") not in {"text", "rich", "large"}:
            return JSONResponse({"ok": False, "error": "invalid result_mode"}, 400)
        state.update(
            {key: body[key] for key in ("delay", "fail", "result_mode") if key in body}
        )
        record(
            "plan",
            delay=state["delay"],
            fail=state["fail"],
            result_mode=state["result_mode"],
        )
        return JSONResponse(
            {
                "ok": True,
                "plan": {key: state[key] for key in ("delay", "fail", "result_mode")},
            }
        )

    async def events(_request: Request) -> JSONResponse:
        return JSONResponse({"events": state["events"]})

    async def reset(_request: Request) -> JSONResponse:
        state["events"] = []
        record("reset")
        return JSONResponse({"ok": True})

    mcp_app = mcp.http_app(path="/mcp")

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with mcp_app.lifespan(mcp_app):
            yield

    return Starlette(
        routes=[
            Route("/_fixture/plan", plan, methods=["POST"]),
            Route("/_fixture/events", events, methods=["GET"]),
            Route("/_fixture/reset", reset, methods=["POST"]),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--event-file", required=True, type=Path)
    args = parser.parse_args()
    args.ready_file.parent.mkdir(parents=True, exist_ok=True)
    args.event_file.parent.mkdir(parents=True, exist_ok=True)
    _write_json(
        args.ready_file, {"name": args.name, "pid": os.getpid(), "port": args.port}
    )
    uvicorn.run(
        build_app(args.name, args.event_file),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
