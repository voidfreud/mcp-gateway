"""Read-only Admin routes for the bounded structured event log."""

from __future__ import annotations

import asyncio
import json
import queue
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from mcp_gateway import logging_setup


class AdminContext(Protocol):
    """The small Admin context surface needed by log routes."""

    def load(self) -> Any: ...


def log_routes(ctx: AdminContext, error: Callable[..., JSONResponse]) -> list[Route]:
    """Expose a bounded, filtered tail without blocking the event loop."""

    async def logs(request: Request):
        raw_limit = request.query_params.get("limit", "100")
        try:
            limit = int(raw_limit)
        except ValueError:
            return error("limit must be an integer between 1 and 500")
        if not 1 <= limit <= 500:
            return error("limit must be an integer between 1 and 500")

        level = request.query_params.get("level")
        if level:
            level = level.upper()
            if level not in logging_setup.LOG_LEVELS:
                return error(
                    "level must be one of " + ", ".join(logging_setup.LOG_LEVELS)
                )
        event = request.query_params.get("event") or None
        cfg = ctx.load()
        # The listener owns the file descriptor and may still have queued
        # records. Flush/read in a worker thread so neither operation stalls
        # Starlette's event loop.
        await asyncio.to_thread(logging_setup.flush)
        entries = await asyncio.to_thread(
            logging_setup.read_tail,
            cfg.log_file,
            limit=limit,
            level=level,
            event=event,
        )
        stats = await asyncio.to_thread(logging_setup.status)
        stats.update(
            {
                "path": str(Path(cfg.log_file).expanduser()),
                "level": cfg.log_level,
                "max_bytes": cfg.log_max_bytes,
                "backup_count": cfg.log_backup_count,
            }
        )
        return JSONResponse(
            {
                "entries": entries,
                "stats": stats,
                "filters": {"limit": limit, "level": level, "event": event},
            }
        )

    async def log_stream(request: Request):
        level = request.query_params.get("level")
        if level:
            level = level.upper()
            if level not in logging_setup.LOG_LEVELS:
                return error(
                    "level must be one of " + ", ".join(logging_setup.LOG_LEVELS)
                )
        subscriber = logging_setup.subscribe()

        async def events():
            try:
                while True:
                    try:
                        line = await asyncio.to_thread(subscriber.get, True, 1)
                    except queue.Empty:
                        yield b": keepalive\n\n"
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if level and entry.get("level", "").upper() != level:
                        continue
                    yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n".encode()
            finally:
                logging_setup.unsubscribe(subscriber)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return [
        Route("/admin/api/logs", logs, methods=["GET"]),
        Route("/admin/api/logs/stream", log_stream, methods=["GET"]),
    ]
