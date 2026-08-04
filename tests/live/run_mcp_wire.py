#!/usr/bin/env python3
"""Socket-level MCP Streamable HTTP receipt for the gateway's public mounts.

This is intentionally a raw HTTP/JSON-RPC client, rather than FastMCP's
``Client``.  It verifies the gateway's actual HTTP, session and response
contracts without sharing the SDK code that implements them.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from mcp.types import LATEST_PROTOCOL_VERSION

from mcp_gateway import __version__
from mcp_gateway import config_loader as cl

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ("alpha", "beta", "blackhole")
TOKEN = "raw-wire-test-token"
LAST_SCRATCH: list[Path | None] = [None]


class ContractFailure(AssertionError):
    """A failed public-wire contract receipt."""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _spawn(command: list[str], env: dict[str, str], output: Path) -> subprocess.Popen:
    return subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=output.with_suffix(".stdout").open("wb"),
        stderr=output.with_suffix(".stderr").open("wb"),
        start_new_session=True,
    )


def _stop(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
    for process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)


def _wait_for(predicate, label: str, *, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # noqa: BLE001 - retain useful startup evidence
            last_error = exc
        time.sleep(0.1)
    suffix = f"; last error: {last_error}" if last_error else ""
    raise ContractFailure(f"timed out waiting for {label}{suffix}")


def _response_message(response: httpx.Response, request_id: int) -> dict[str, Any]:
    """Return a matching JSON-RPC message from JSON or SSE response content."""
    content_type = response.headers.get("content-type", "").lower()
    if content_type.startswith("application/json"):
        payload = response.json()
        messages = payload if isinstance(payload, list) else [payload]
    elif content_type.startswith("text/event-stream"):
        messages = []
        data: list[str] = []
        for line in response.text.splitlines():
            if not line:
                if data:
                    messages.append(json.loads("\n".join(data)))
                    data = []
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if data:
            messages.append(json.loads("\n".join(data)))
    else:
        raise ContractFailure(
            "expected an MCP JSON or SSE response, got "
            f"{content_type!r}: {response.text!r}"
        )
    for message in messages:
        if message.get("id") == request_id:
            return message
    raise ContractFailure(
        f"response did not contain JSON-RPC id {request_id}: {messages!r}"
    )


def _headers(
    *,
    session_id: str | None = None,
    protocol_version: str | None = None,
    origin: str | None = None,
    bearer: bool = True,
    accept: str = "application/json, text/event-stream",
    content_type: str = "application/json",
) -> dict[str, str]:
    headers = {"Accept": accept, "Content-Type": content_type}
    if bearer:
        headers["Authorization"] = f"Bearer {TOKEN}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    if protocol_version:
        headers["Mcp-Protocol-Version"] = protocol_version
    if origin:
        headers["Origin"] = origin
    return headers


def _post(
    client: httpx.Client,
    url: str,
    payload: dict[str, Any],
    **header_options: Any,
) -> httpx.Response:
    return client.post(
        url, content=json.dumps(payload), headers=_headers(**header_options)
    )


def _initialize(
    client: httpx.Client,
    url: str,
    request_id: int,
    protocol_version: str,
    *,
    origin: str | None = None,
) -> tuple[str, dict[str, Any]]:
    response = _post(
        client,
        url,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "mcp-gateway-raw-wire", "version": "1"},
            },
        },
        origin=origin,
    )
    if response.status_code != 200:
        raise ContractFailure(
            f"initialize {url} -> {response.status_code}: {response.text}"
        )
    session_id = response.headers.get("mcp-session-id")
    if not session_id or any(
        ord(char) < 0x21 or ord(char) > 0x7E for char in session_id
    ):
        raise ContractFailure(
            f"initialize did not mint a visible-ASCII session id: {session_id!r}"
        )
    message = _response_message(response, request_id)
    if message.get("jsonrpc") != "2.0" or "result" not in message:
        raise ContractFailure(
            f"initialize did not return a JSON-RPC result: {message!r}"
        )
    return session_id, message["result"]


def _assert_initialize_metadata(
    result: dict[str, Any], endpoint_name: str, *, tools_only: bool
) -> None:
    expected_info = {"name": endpoint_name, "version": __version__}
    if result.get("serverInfo") != expected_info:
        raise ContractFailure(
            f"{endpoint_name} reported wrong serverInfo: {result.get('serverInfo')!r}"
        )
    expected_capabilities = {"tools": {"listChanged": False}}
    if not tools_only:
        expected_capabilities.update(
            {
                "logging": {},
                "prompts": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            }
        )
    if result.get("capabilities") != expected_capabilities:
        raise ContractFailure(
            f"{endpoint_name} advertised unexpected capabilities: "
            f"{result.get('capabilities')!r}"
        )


def _initialized(
    client: httpx.Client, url: str, session_id: str, protocol_version: str
) -> None:
    response = _post(
        client,
        url,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        session_id=session_id,
        protocol_version=protocol_version,
    )
    if response.status_code != 202 or response.content:
        raise ContractFailure(
            "initialized notification must return 202 with an empty body, got "
            f"{response.status_code} {response.text!r}"
        )


def _rpc(
    client: httpx.Client,
    url: str,
    request_id: int,
    method: str,
    params: dict[str, Any],
    session_id: str,
    protocol_version: str,
) -> dict[str, Any]:
    response = _post(
        client,
        url,
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        session_id=session_id,
        protocol_version=protocol_version,
    )
    if response.status_code != 200:
        raise ContractFailure(
            f"{method} -> HTTP {response.status_code}: {response.text}"
        )
    message = _response_message(response, request_id)
    if message.get("jsonrpc") != "2.0":
        raise ContractFailure(f"{method} did not preserve JSON-RPC 2.0: {message!r}")
    return message


def _assert_rpc_error(
    client: httpx.Client,
    url: str,
    session_id: str,
    protocol_version: str,
    request_id: int,
    method: str,
    params: dict[str, Any],
    expected_code: int,
) -> None:
    message = _rpc(
        client,
        url,
        request_id,
        method,
        params,
        session_id,
        protocol_version,
    )
    code = (message.get("error") or {}).get("code")
    if code != expected_code:
        raise ContractFailure(
            f"{method} returned JSON-RPC code {code}, expected {expected_code}: "
            f"{message!r}"
        )


def _write_config(path: Path, port: int, fixture_ports: dict[str, int]) -> None:
    cfg = cl.GatewayConfig(
        host="127.0.0.1",
        port=port,
        log_file=str(path.parent / "gateway.log"),
        baseline_max_age=3600,
        bearer_token="${RAW_WIRE_GATEWAY_TOKEN}",
        backends=[
            cl.Backend(
                id="raw-wire-alpha",
                name="alpha",
                transport="http",
                url=f"http://127.0.0.1:{fixture_ports['alpha']}/mcp",
                stateless=False,
                request_timeout=0.25,
            ),
            cl.Backend(
                id="raw-wire-beta",
                name="beta",
                transport="http",
                url=f"http://127.0.0.1:{fixture_ports['beta']}/mcp",
                stateless=True,
            ),
            cl.Backend(
                id="raw-wire-blackhole",
                name="blackhole",
                transport="http",
                url=f"http://127.0.0.1:{fixture_ports['blackhole']}/mcp",
                stateless=False,
                init_timeout=0.25,
                request_timeout=0.25,
            ),
        ],
        virtual_tools=[
            cl.VirtualTool(
                name="virtual_echo",
                description="Echo through the alpha fixture.",
                enabled=True,
                inputs=[cl.VirtualInput(name="text")],
                members=[
                    cl.VirtualMember(
                        backend_id="raw-wire-alpha",
                        tool_original="echo",
                        args={"text": "text"},
                    )
                ],
            )
        ],
    )
    cl.save(cfg, str(path))
    # Keep startup baseline capture from masking the mount-handshake timeout
    # under test with its separate capture timeout.
    defaults_dir = path.parent / "home/.local/state/mcp-gateway/defaults"
    defaults_dir.mkdir(parents=True, exist_ok=True)
    (defaults_dir / "blackhole.json").write_text(
        json.dumps(
            {
                "backend": "blackhole",
                "captured_at": time.time(),
                "instructions": None,
                "server_info": None,
                "capabilities": None,
                "tools": [],
                "resources": [],
                "resource_templates": [],
                "prompts": [],
            }
        ),
        encoding="utf-8",
    )


def _assert_catalog(
    client: httpx.Client,
    url: str,
    session_id: str,
    protocol_version: str,
    request_id: int,
    expected: set[str],
) -> None:
    listed = _rpc(
        client, url, request_id, "tools/list", {}, session_id, protocol_version
    )
    result = listed.get("result")
    names = {tool["name"] for tool in (result or {}).get("tools", [])}
    if names != expected:
        raise ContractFailure(
            f"{url} catalog mismatch: expected {expected}, got {names}"
        )
    if result.get("nextCursor") is not None:
        raise ContractFailure(
            f"{url} unexpectedly paginated its small deterministic catalog: {result!r}"
        )


def _assert_echo(
    client: httpx.Client,
    url: str,
    session_id: str,
    protocol_version: str,
    request_id: int,
    tool: str,
    expected_text: str,
) -> None:
    message = _rpc(
        client,
        url,
        request_id,
        "tools/call",
        {"name": tool, "arguments": {"text": "hello"}},
        session_id,
        protocol_version,
    )
    result = message.get("result") or {}
    text = "\n".join(item.get("text", "") for item in result.get("content", []))
    if result.get("isError") is True or expected_text not in text:
        raise ContractFailure(f"{tool} returned the wrong MCP result: {result!r}")


def _assert_application_error(
    client: httpx.Client,
    url: str,
    session_id: str,
    protocol_version: str,
    request_id: int,
) -> None:
    message = _rpc(
        client,
        url,
        request_id,
        "tools/call",
        {"name": "does_not_exist", "arguments": {}},
        session_id,
        protocol_version,
    )
    result = message.get("result") or {}
    if result.get("isError") is not True or not result.get("content"):
        raise ContractFailure(
            f"unknown tool was not an MCP tool-result error: {message!r}"
        )


def _assert_request_timeout(
    client: httpx.Client,
    url: str,
    session_id: str,
    protocol_version: str,
    request_id: int,
) -> None:
    started = time.monotonic()
    message = _rpc(
        client,
        url,
        request_id,
        "tools/call",
        {"name": "hang", "arguments": {}},
        session_id,
        protocol_version,
    )
    elapsed = time.monotonic() - started
    result = message.get("result") or {}
    if result.get("isError") is not True or not (0.15 <= elapsed < 3):
        raise ContractFailure(
            "hung backend call did not honor its request timeout: "
            f"elapsed={elapsed:.3f}s result={result!r}"
        )


def _assert_transport_errors(client: httpx.Client, endpoint: str) -> None:
    malformed = client.post(
        endpoint,
        content=b"{",
        headers=_headers(),
    )
    if malformed.status_code != 400:
        raise ContractFailure(
            f"malformed JSON must be HTTP 400, got {malformed.status_code}"
        )
    wrong_type = client.post(
        endpoint,
        content=json.dumps({"jsonrpc": "2.0", "id": 99, "method": "initialize"}),
        headers=_headers(content_type="text/plain"),
    )
    # FastMCP's proxy layer normalizes this to 400, while the underlying
    # transport can emit 415. Both are protocol-level rejection, so pin the
    # observable supported behaviour without assuming a private implementation.
    if wrong_type.status_code not in {400, 415}:
        raise ContractFailure(
            "wrong content type must be rejected with HTTP 400 or 415, got "
            f"{wrong_type.status_code}"
        )


def run(keep: bool) -> Path:  # noqa: PLR0915 - one coherent black-box receipt
    scratch = Path(tempfile.mkdtemp(prefix="mcp-gateway-raw-wire-"))
    LAST_SCRATCH[0] = scratch
    for directory in ("fixtures", "logs", "processes", "receipts", "home", "hooks"):
        (scratch / directory).mkdir()
    environment = {
        **os.environ,
        "HOME": str(scratch / "home"),
        "MCP_GATEWAY_CONFIG": str(scratch / "config.toml"),
        "MCP_GATEWAY_SECRETS": str(scratch / "secrets.env"),
        "MCP_GATEWAY_HOOKS": str(scratch / "hooks"),
        "RAW_WIRE_GATEWAY_TOKEN": TOKEN,
        "PYTHONPATH": str(REPO_ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
        "PYTHONUNBUFFERED": "1",
    }
    receipt: dict[str, Any] = {
        "checks": [],
        "scratch": str(scratch),
        "status": "running",
    }
    processes: list[subprocess.Popen] = []
    passed = False
    try:
        fixture_ports = {name: _free_port() for name in FIXTURES}
        for name in FIXTURES:
            command = [
                sys.executable,
                str(REPO_ROOT / "tests/fixtures/raw_wire_backend.py"),
                "--name",
                name,
                "--port",
                str(fixture_ports[name]),
                "--ready-file",
                str(scratch / "fixtures" / f"{name}.ready.json"),
                "--event-file",
                str(scratch / "fixtures" / f"{name}.jsonl"),
            ]
            if name == "blackhole":
                command.append("--hang-initialize")
            process = _spawn(
                command,
                environment,
                scratch / "processes" / name,
            )
            processes.append(process)
            _wait_for(
                lambda n=name: (scratch / "fixtures" / f"{n}.ready.json").is_file(),
                f"{name} fixture",
            )
            _wait_for(
                lambda p=fixture_ports[name]: (
                    httpx.get(f"http://127.0.0.1:{p}/health", timeout=0.5).status_code
                    == 200
                ),
                f"{name} fixture HTTP listener",
            )

        gateway_port = _free_port()
        _write_config(scratch / "config.toml", gateway_port, fixture_ports)
        gateway = _spawn(
            [sys.executable, "-c", "from mcp_gateway.server import main; main()"],
            environment,
            scratch / "processes" / "gateway",
        )
        processes.append(gateway)
        base = f"http://127.0.0.1:{gateway_port}"
        with httpx.Client(timeout=8) as client:
            _wait_for(
                lambda: client.get(f"{base}/health").status_code == 200,
                "gateway health",
            )

            def expected_gateway_state() -> bool:
                response = client.get(f"{base}/ready")
                if response.status_code != 503:
                    return False
                state = response.json()
                return state.get("mounted") == ["alpha", "beta"] and state.get(
                    "missing"
                ) == ["blackhole"]

            _wait_for(expected_gateway_state, "gateway degraded readiness")
            _wait_for(
                lambda: (
                    (scratch / "gateway.log").is_file()
                    and '"event": "backend_mount_failed"'
                    in (scratch / "gateway.log").read_text(encoding="utf-8")
                    and '"backend": "blackhole"'
                    in (scratch / "gateway.log").read_text(encoding="utf-8")
                ),
                "bounded blackhole initialization failure",
            )
            receipt["checks"].append(
                "backend initialization timeout and degraded readiness"
            )

            alpha = f"{base}/alpha/mcp"
            beta = f"{base}/beta/mcp"
            virtual = f"{base}/virtual/mcp"

            missing_auth = client.post(
                alpha, content=b"{}", headers=_headers(bearer=False)
            )
            if (
                missing_auth.status_code != 401
                or missing_auth.headers.get("www-authenticate") != "Bearer"
            ):
                raise ContractFailure(
                    "bearer-protected MCP endpoint did not challenge "
                    "unauthenticated request"
                )
            foreign_origin = client.post(
                alpha,
                content=b"{}",
                headers=_headers(bearer=False, origin="http://evil.example"),
            )
            if foreign_origin.status_code != 403:
                raise ContractFailure(
                    "foreign Origin did not win over bearer auth with HTTP 403"
                )
            receipt["checks"].append(
                "bearer challenge and origin-before-auth rejection"
            )

            root = client.post(f"{base}/mcp", content=b"{}", headers=_headers())
            if root.status_code != 404:
                raise ContractFailure(
                    f"aggregate /mcp must not exist, got HTTP {root.status_code}"
                )
            receipt["checks"].append("no aggregate endpoint")

            alpha_session, alpha_init = _initialize(
                client, alpha, 1, LATEST_PROTOCOL_VERSION, origin=base
            )
            if alpha_init.get("protocolVersion") != LATEST_PROTOCOL_VERSION:
                raise ContractFailure(f"latest protocol was not echoed: {alpha_init!r}")
            _assert_initialize_metadata(
                alpha_init, "mcp-gateway-alpha", tools_only=False
            )
            _initialized(client, alpha, alpha_session, LATEST_PROTOCOL_VERSION)
            _assert_catalog(
                client,
                alpha,
                alpha_session,
                LATEST_PROTOCOL_VERSION,
                2,
                {"echo", "identity", "fail", "hang"},
            )
            _assert_echo(
                client,
                alpha,
                alpha_session,
                LATEST_PROTOCOL_VERSION,
                3,
                "echo",
                "alpha:hello",
            )
            _assert_application_error(
                client, alpha, alpha_session, LATEST_PROTOCOL_VERSION, 4
            )
            _assert_request_timeout(
                client, alpha, alpha_session, LATEST_PROTOCOL_VERSION, 5
            )
            receipt["checks"].append("backend tool-call request timeout")
            receipt["checks"].append(
                "alpha raw initialize initialized list call and tool error"
            )
            _assert_rpc_error(
                client,
                alpha,
                alpha_session,
                LATEST_PROTOCOL_VERSION,
                6,
                "prompts/get",
                {"name": "does-not-exist", "arguments": {}},
                -32602,
            )
            _assert_rpc_error(
                client,
                alpha,
                alpha_session,
                LATEST_PROTOCOL_VERSION,
                7,
                "prompts/get",
                {"name": "required_prompt", "arguments": {}},
                -32602,
            )
            _assert_rpc_error(
                client,
                alpha,
                alpha_session,
                LATEST_PROTOCOL_VERSION,
                8,
                "prompts/get",
                {"name": "broken_prompt", "arguments": {}},
                -32603,
            )
            receipt["checks"].append("standard prompt JSON-RPC error codes")

            missing_session = _post(
                client,
                alpha,
                {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}},
                protocol_version=LATEST_PROTOCOL_VERSION,
            )
            if missing_session.status_code != 400:
                raise ContractFailure(
                    "missing session id must be HTTP 400, got "
                    f"{missing_session.status_code}"
                )

            unknown = _post(
                client,
                alpha,
                {"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}},
                session_id="does-not-exist",
                protocol_version=LATEST_PROTOCOL_VERSION,
            )
            if unknown.status_code != 404:
                raise ContractFailure(
                    f"unknown session must be HTTP 404, got {unknown.status_code}"
                )
            terminated = client.delete(
                alpha,
                headers=_headers(
                    session_id=alpha_session,
                    protocol_version=LATEST_PROTOCOL_VERSION,
                ),
            )
            if terminated.status_code != 200:
                raise ContractFailure(
                    f"session DELETE must be HTTP 200, got {terminated.status_code}"
                )
            after_delete = _post(
                client,
                alpha,
                {"jsonrpc": "2.0", "id": 11, "method": "tools/list", "params": {}},
                session_id=alpha_session,
                protocol_version=LATEST_PROTOCOL_VERSION,
            )
            if after_delete.status_code != 404:
                raise ContractFailure(
                    "terminated session must be rejected with HTTP 404, got "
                    f"{after_delete.status_code}"
                )
            receipt["checks"].append(
                "missing invalid and deleted sessions follow HTTP contracts"
            )

            beta_session, beta_init = _initialize(client, beta, 10, "2025-03-26")
            if beta_init.get("protocolVersion") != "2025-03-26":
                raise ContractFailure(
                    f"legacy protocol did not negotiate exactly: {beta_init!r}"
                )
            _assert_initialize_metadata(beta_init, "mcp-gateway-beta", tools_only=False)
            _initialized(client, beta, beta_session, "2025-03-26")
            _assert_catalog(
                client,
                beta,
                beta_session,
                "2025-03-26",
                11,
                {"echo", "identity", "fail", "hang"},
            )
            _assert_echo(
                client, beta, beta_session, "2025-03-26", 12, "echo", "beta:hello"
            )
            receipt["checks"].append(
                "independent stateless-backend endpoint and legacy protocol"
            )

            virtual_session, virtual_init = _initialize(
                client, virtual, 20, LATEST_PROTOCOL_VERSION
            )
            _assert_initialize_metadata(
                virtual_init, "mcp-gateway-virtual", tools_only=True
            )
            _initialized(client, virtual, virtual_session, LATEST_PROTOCOL_VERSION)
            _assert_catalog(
                client,
                virtual,
                virtual_session,
                LATEST_PROTOCOL_VERSION,
                21,
                {"virtual_echo"},
            )
            _assert_echo(
                client,
                virtual,
                virtual_session,
                LATEST_PROTOCOL_VERSION,
                22,
                "virtual_echo",
                "alpha:hello",
            )
            receipt["checks"].append("independent virtual endpoint and routed call")

            _assert_transport_errors(client, alpha)
            receipt["checks"].append("raw malformed-json and content-type errors")

        receipt["status"] = "passed"
        passed = True
        return scratch
    except Exception as exc:  # noqa: BLE001 - receipt keeps root failure evidence
        receipt["status"] = "failed"
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        (scratch / "receipts/report.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8"
        )
        _stop(list(reversed(processes)))
        if passed and not keep:
            shutil.rmtree(scratch)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="retain passing artifacts")
    args = parser.parse_args()
    try:
        scratch = run(args.keep)
    except Exception as exc:  # noqa: BLE001 - command-line receipt summary
        print(f"MCP RAW-WIRE RECEIPTS FAILED: {exc}", file=sys.stderr)
        if LAST_SCRATCH[0] is not None:
            print(f"ARTIFACTS RETAINED: {LAST_SCRATCH[0]}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"MCP RAW-WIRE RECEIPTS PASSED: {scratch}")


if __name__ == "__main__":
    main()
