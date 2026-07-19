"""Structured, bounded, asynchronous logging for the gateway.

The gateway emits structured events from both async request paths and ordinary
library loggers.  A bounded :class:`logging.handlers.QueueHandler` keeps those
paths from waiting on filesystem I/O; a listener thread owns the rotating file
handler.  The queue is deliberately non-blocking.  If an exceptional burst
fills it, events are counted as dropped instead of slowing the event loop.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5
LOG_QUEUE_SIZE = 10_000
FLUSH_TIMEOUT = 5.0
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _timestamp_first(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Keep the timestamp as the first field in every structured line.

    JSON object order is not semantically significant, but putting the time
    first makes shell tails and the dashboard much easier to scan while
    preserving the machine-readable JSON-lines contract.
    """
    timestamp = event_dict.get("timestamp")
    if timestamp is None:
        return event_dict
    ordered: dict[str, Any] = {"timestamp": timestamp}
    for key in ("level", "logger", "event"):
        if key in event_dict and key != "timestamp":
            ordered[key] = event_dict[key]
    ordered.update(
        (key, value) for key, value in event_dict.items() if key not in ordered
    )
    return ordered


class _JsonFormatter(logging.Formatter):
    """Keep third-party stdlib records in the same JSON-lines contract."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        try:
            parsed = json.loads(message)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict) and "event" in parsed:
            return message
        item: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": "stdlib_log",
            "message": message,
            "filename": record.filename,
            "func_name": record.funcName,
            "lineno": record.lineno,
        }
        if record.exc_info:
            item["exception"] = self.formatException(record.exc_info)
        return json.dumps(item, ensure_ascii=False, separators=(",", ":"))


class _FlushMarker:
    """A queue item used to make tests and shutdowns deterministically flush."""

    def __init__(self) -> None:
        self.done = threading.Event()


class _AsyncQueueHandler(QueueHandler):
    """Queue handler whose hot path never waits for the listener thread."""

    def __init__(self, log_queue: queue.Queue[Any]) -> None:
        super().__init__(log_queue)
        self._listener: _AsyncQueueListener | None = None
        self._dropped = 0
        self._drop_lock = threading.Lock()

    @property
    def dropped(self) -> int:
        with self._drop_lock:
            return self._dropped

    def emit(self, record: logging.LogRecord) -> None:
        # QueueHandler.emit normally calls handleError on queue.Full.  That
        # writes to stderr and is itself a synchronous side effect, so the
        # gateway handles overflow explicitly and exposes the count instead.
        try:
            self.queue.put_nowait(self.prepare(record))
        except queue.Full:
            with self._drop_lock:
                self._dropped += 1
        except Exception:  # noqa: BLE001 - logging must never break the app
            self.handleError(record)

    def flush(self) -> None:
        listener = self._listener
        if listener is None or not listener.alive:
            return
        marker = _FlushMarker()
        # Flush is an explicit control operation, never the event-loop hot
        # path.  Waiting here guarantees callers such as the dashboard/tests
        # see all events emitted before their request.
        try:
            self.queue.put(marker, timeout=FLUSH_TIMEOUT)
        except queue.Full:
            return
        marker.done.wait(FLUSH_TIMEOUT)


class _AsyncQueueListener(QueueListener):
    """QueueListener with a flush marker and reliable bounded-queue shutdown."""

    @property
    def alive(self) -> bool:
        thread = getattr(self, "_thread", None)
        return thread is not None and thread.is_alive()

    def handle(self, record: Any) -> None:
        if isinstance(record, _FlushMarker):
            for handler in self.handlers:
                handler.flush()
            record.done.set()
            return
        super().handle(record)
        if self.handlers:
            _publish(self.handlers[0].format(record))

    def enqueue_sentinel(self) -> None:
        # QueueListener's default uses put_nowait; a full queue could then lose
        # the sentinel and leave the listener thread alive during reconfigure.
        if self.alive:
            try:
                self.queue.put(self._sentinel, timeout=FLUSH_TIMEOUT)
            except queue.Full:
                return


@dataclass
class _LoggingRuntime:
    path: Path
    level: str
    max_bytes: int
    backup_count: int
    queue: queue.Queue[Any]
    queue_handler: _AsyncQueueHandler
    file_handler: RotatingFileHandler
    listener: _AsyncQueueListener


_runtime: _LoggingRuntime | None = None
_runtime_lock = threading.RLock()
_subscribers: set[queue.Queue[str]] = set()
_subscribers_lock = threading.Lock()


def subscribe() -> queue.Queue[str]:
    """Subscribe to formatted events emitted after this call."""
    subscriber: queue.Queue[str] = queue.Queue(maxsize=256)
    with _subscribers_lock:
        _subscribers.add(subscriber)
    return subscriber


def unsubscribe(subscriber: queue.Queue[str]) -> None:
    """Stop delivering events to a log-stream subscriber."""
    with _subscribers_lock:
        _subscribers.discard(subscriber)


def _publish(line: str) -> None:
    with _subscribers_lock:
        subscribers = tuple(_subscribers)
    for subscriber in subscribers:
        try:
            subscriber.put_nowait(line)
        except queue.Full:
            # A slow dashboard must never apply backpressure to logging.
            continue


def _level_number(level: str) -> int:
    normalized = level.upper()
    if normalized not in LOG_LEVELS:
        choices = ", ".join(LOG_LEVELS)
        raise ValueError(f"log level must be one of {choices}; got {level!r}")
    return getattr(logging, normalized)


def _stop_runtime() -> None:
    global _runtime  # noqa: PLW0603 - the process owns one replaceable runtime
    current = _runtime
    _runtime = None
    if current is None:
        return
    if current.listener.alive:
        current.queue_handler.flush()
        current.listener.stop()
    current.file_handler.close()


def configure(
    log_file: str,
    *,
    level: str = DEFAULT_LOG_LEVEL,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> structlog.BoundLogger:
    """Configure JSON structlog and return the gateway logger.

    The root logger keeps exactly one queue handler.  Every propagated stdlib
    record (including uvicorn and FastMCP) therefore reaches the same rotating
    JSON-lines file, while the listener thread performs the actual file writes.
    """
    normalized = level.upper()
    numeric_level = _level_number(normalized)
    if isinstance(max_bytes, bool) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if isinstance(backup_count, bool) or backup_count < 1:
        raise ValueError("backup_count must be a positive integer")

    path = Path(log_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    global _runtime  # noqa: PLW0603 - one process-wide logging runtime
    with _runtime_lock:
        _stop_runtime()
        file_handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(_JsonFormatter())

        log_queue: queue.Queue[Any] = queue.Queue(maxsize=LOG_QUEUE_SIZE)
        queue_handler = _AsyncQueueHandler(log_queue)
        listener = _AsyncQueueListener(log_queue, file_handler)
        queue_handler._listener = listener

        root = logging.getLogger()
        for handler in root.handlers:
            handler.close()
        root.handlers = [queue_handler]
        root.setLevel(numeric_level)

        # Keep library chatter at WARNING unless DEBUG was explicitly asked
        # for.  The gateway's own logger follows the configured level exactly.
        library_level = max(numeric_level, logging.WARNING)
        for name in (
            "fastmcp",
            "uvicorn",
            "uvicorn.error",
            "uvicorn.access",
            "mcp",
            "httpx",
            "httpx2",
            "httpcore",
        ):
            logging.getLogger(name).setLevel(library_level)
        app_logger = logging.getLogger("mcp-gateway")
        app_logger.setLevel(numeric_level)
        app_logger.handlers = []
        app_logger.propagate = True

        structlog.configure(
            processors=[
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.CallsiteParameterAdder(
                    parameters=[
                        structlog.processors.CallsiteParameter.FILENAME,
                        structlog.processors.CallsiteParameter.FUNC_NAME,
                        structlog.processors.CallsiteParameter.LINENO,
                    ]
                ),
                _timestamp_first,
                structlog.processors.JSONRenderer(),
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
        )
        listener.start()
        _runtime = _LoggingRuntime(
            path=path,
            level=normalized,
            max_bytes=max_bytes,
            backup_count=backup_count,
            queue=log_queue,
            queue_handler=queue_handler,
            file_handler=file_handler,
            listener=listener,
        )
    return structlog.get_logger("mcp-gateway")


def flush() -> None:
    """Drain queued records into the file, for diagnostics and orderly exit."""
    with _runtime_lock:
        if _runtime is not None:
            _runtime.queue_handler.flush()


def shutdown() -> None:
    """Stop the listener and close the rotating file handler, if configured."""
    with _runtime_lock:
        root = logging.getLogger()
        _stop_runtime()
        root.handlers = []


def status() -> dict[str, Any]:
    """Return safe logger health/retention counters for the Admin UI."""
    with _runtime_lock:
        current = _runtime
        if current is None:
            return {
                "configured": False,
                "listener_alive": False,
                "queue_size": LOG_QUEUE_SIZE,
                "queue_depth": 0,
                "dropped_events": 0,
            }
        return {
            "configured": True,
            "path": str(current.path),
            "level": current.level,
            "max_bytes": current.max_bytes,
            "backup_count": current.backup_count,
            "queue_size": LOG_QUEUE_SIZE,
            "queue_depth": current.queue.qsize(),
            "dropped_events": current.queue_handler.dropped,
            "listener_alive": current.listener.alive,
        }


def read_tail(
    log_file: str,
    *,
    limit: int = 100,
    level: str | None = None,
    event: str | None = None,
) -> list[dict[str, Any]]:
    """Read and filter a bounded tail of the current JSON-lines log file.

    This function is synchronous by design so callers can run it with
    :func:`asyncio.to_thread`; dashboard requests must not read the filesystem
    on the event-loop thread.  Invalid legacy lines remain visible as ``raw``.
    """
    path = Path(log_file).expanduser()
    if not path.is_file():
        return []
    wanted_level = level.upper() if level else None
    recent: deque[dict[str, Any]] = deque(maxlen=limit)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for raw_line in stream:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    item = {"raw": line}
                if not isinstance(item, dict):
                    item = {"raw": line}
                if wanted_level and str(item.get("level", "")).upper() != wanted_level:
                    continue
                if event and item.get("event") != event:
                    continue
                recent.append(item)
    except OSError:
        # Rotation can replace the file between the is_file check and open.
        return []
    return list(recent)


class RequestLogMiddleware:
    """Log every HTTP request with status and latency without reading bodies."""

    def __init__(self, app: Any, log: structlog.BoundLogger) -> None:
        self.app = app
        self.log = log

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status_code: int | None = None

        async def send_logged(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 0))
            await send(message)

        method = str(scope.get("method", "?"))
        path = str(scope.get("path", "?"))
        try:
            await self.app(scope, receive, send_logged)
        except Exception as exc:  # noqa: BLE001 - log, then preserve ASGI error
            self.log.exception(
                "http_request_error",
                method=method,
                path=path,
                status_code=status_code,
                ms=round((time.perf_counter() - started) * 1000, 2),
                error_type=type(exc).__name__,
            )
            raise
        ms = round((time.perf_counter() - started) * 1000, 2)
        fields = {"method": method, "path": path, "status_code": status_code, "ms": ms}
        if status_code is not None and status_code >= 500:
            self.log.warning("http_request", **fields)
        else:
            self.log.info("http_request", **fields)
        if path.startswith("/admin/api/") and method not in {"GET", "HEAD"}:
            self.log.info(
                "admin_action",
                action=path.removeprefix("/admin/api/"),
                method=method,
                path=path,
                status_code=status_code,
                ms=ms,
            )
