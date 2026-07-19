#!/usr/bin/env python3
"""Small deterministic MCP backend used by the raw-wire gateway receipt."""

from __future__ import annotations

import argparse
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount


def _write_ready(path: Path, *, name: str, port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"name": name, "pid": os.getpid(), "port": port}),
        encoding="utf-8",
    )


def build_app(name: str, event_file: Path) -> Starlette:
    """Expose three stable tools whose responses identify their backend."""
    event_file.parent.mkdir(parents=True, exist_ok=True)
    mcp = FastMCP(name=f"raw-wire-fixture-{name}")

    def record(tool: str, **fields: object) -> None:
        event = {"at": time.time(), "backend": name, "tool": tool, **fields}
        with event_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    @mcp.tool
    def echo(text: str) -> str:
        """Return the caller's text with a backend-specific prefix."""
        record("echo", text=text)
        return f"{name}:{text}"

    @mcp.tool
    def identity() -> str:
        """Return the fixture backend identity."""
        record("identity")
        return name

    @mcp.tool
    def fail() -> str:
        """Produce a deterministic application-level tool error."""
        record("fail")
        raise ValueError(f"{name} forced failure")

    mcp_app = mcp.http_app(path="/mcp")

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with mcp_app.lifespan(mcp_app):
            yield

    return Starlette(routes=[Mount("/", app=mcp_app)], lifespan=lifespan)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--event-file", required=True, type=Path)
    args = parser.parse_args()
    _write_ready(args.ready_file, name=args.name, port=args.port)
    uvicorn.run(
        build_app(args.name, args.event_file),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
