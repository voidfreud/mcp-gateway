"""Structured logger, dashboard tail, and request-event coverage."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_gateway import admin_routes_logs, config_loader, logging_setup


def test_async_logger_writes_json_with_diagnostics_and_bounds(tmp_path):
    path = tmp_path / "gateway.log"
    log = logging_setup.configure(
        str(path), level="DEBUG", max_bytes=64 * 1024, backup_count=2
    )
    try:
        log.debug("diagnostic_event", backend="demo", ms=1.25)
        logging_setup.flush()
        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert next(iter(record)) == "timestamp"
        assert record["event"] == "diagnostic_event"
        assert record["level"] == "debug"
        assert record["logger"] == "mcp-gateway"
        assert record["backend"] == "demo"
        assert record["timestamp"]
        assert record["filename"] == Path(__file__).name
        assert (
            record["func_name"]
            == "test_async_logger_writes_json_with_diagnostics_and_bounds"
        )
        status = logging_setup.status()
        assert status["listener_alive"] is True
        assert status["max_bytes"] == 64 * 1024
        assert status["backup_count"] == 2
    finally:
        logging_setup.shutdown()


def test_timestamp_is_first_for_plain_stdlib_records(tmp_path):
    path = tmp_path / "gateway.log"
    logging_setup.configure(str(path))
    try:
        logging.getLogger("third-party").warning("plain framework message")
        logging_setup.flush()
        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert next(iter(record)) == "timestamp"
        assert record["event"] == "stdlib_log"
        assert record["message"] == "plain framework message"
    finally:
        logging_setup.shutdown()


def test_logger_level_filters_events_and_rotates(tmp_path):
    path = tmp_path / "gateway.log"
    log = logging_setup.configure(
        str(path), level="ERROR", max_bytes=64 * 1024, backup_count=2
    )
    try:
        log.info("filtered_event")
        for index in range(40):
            log.error("large_event", index=index, payload="x" * 2500)
        log.error("kept_event", detail="x")
        logging_setup.flush()
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert all(item["level"] == "error" for item in records)
        assert any(item["event"] == "kept_event" for item in records)
        assert (tmp_path / "gateway.log.1").is_file()
    finally:
        logging_setup.shutdown()


def test_log_tail_endpoint_is_bounded_and_filterable(tmp_path):
    path = tmp_path / "gateway.log"
    cfg = config_loader.GatewayConfig.model_validate({"log_file": str(path)})
    log = logging_setup.configure(str(path))

    class Context:
        def load(self):
            return cfg

    def error(message: str, status: int = 400):
        return JSONResponse({"ok": False, "error": message}, status_code=status)

    app = Starlette(routes=admin_routes_logs.log_routes(Context(), error))
    try:
        log.info("visible_event", backend="demo")
        log.warning("warning_event", backend="demo")
        client = TestClient(app)
        response = client.get("/admin/api/logs?limit=1&level=WARNING")
        assert response.status_code == 200
        body = response.json()
        assert [item["event"] for item in body["entries"]] == ["warning_event"]
        assert body["stats"]["max_bytes"] == cfg.log_max_bytes
        assert body["stats"]["listener_alive"] is True
    finally:
        logging_setup.shutdown()


def test_request_middleware_emits_status_and_latency(tmp_path):
    path = tmp_path / "gateway.log"
    log = logging_setup.configure(str(path))

    async def health(_request):
        return PlainTextResponse("ok")

    async def action(_request):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/admin/api/demo", action, methods=["POST"]),
        ],
        middleware=[Middleware(logging_setup.RequestLogMiddleware, log=log)],
    )
    try:
        client = TestClient(app)
        assert client.get("/health").status_code == 200
        assert client.post("/admin/api/demo").status_code == 200
        logging_setup.flush()
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").strip().splitlines()
        ]
        record = next(item for item in records if item.get("event") == "http_request")
        assert record["event"] == "http_request"
        assert record["path"] == "/health"
        assert record["status_code"] == 200
        assert record["ms"] >= 0
        action_record = next(
            item for item in records if item.get("event") == "admin_action"
        )
        assert action_record["action"] == "demo"
        assert action_record["method"] == "POST"
    finally:
        logging_setup.shutdown()
