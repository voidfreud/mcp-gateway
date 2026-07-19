#!/usr/bin/env python3
"""Opt-in black-box acceptance receipts for ADR-0005 Virtual Tools.

The expected Admin API is intentionally defined here while the product surface
is implemented. A missing endpoint is a useful failing receipt, not a reason to
skip the contract.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ("alpha", "beta")
LAST_SCRATCH: list[Path | None] = [None]


class ContractFailure(AssertionError):
    """A failing ADR acceptance receipt."""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(url: str, *, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:  # noqa: S310 - loopback only
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ContractFailure(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc


def _wait_for(predicate, label: str, *, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # noqa: BLE001 - retain the last startup error
            last_error = exc
        time.sleep(0.1)
    suffix = f"; last error: {last_error}" if last_error else ""
    raise ContractFailure(f"timed out waiting for {label}{suffix}")


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


def _write_config(
    path: Path, port: int, fixture_ports: dict[str, int], log_file: Path
) -> None:
    path.write_text(
        f'''host = "127.0.0.1"
port = {port}
log_file = "{log_file}"
baseline_max_age = 3600

[[backends]]
name = "alpha"
transport = "http"
url = "http://127.0.0.1:{fixture_ports["alpha"]}/mcp"
stateless = false

[[backends]]
name = "beta"
transport = "http"
url = "http://127.0.0.1:{fixture_ports["beta"]}/mcp"
stateless = true
''',
        encoding="utf-8",
    )


async def _tools(url: str) -> list[Any]:
    async with Client(url, timeout=8) as client:
        return await client.list_tools()


async def _tools_list_changed(url: str) -> bool | None:
    async with Client(url, timeout=8) as client:
        capabilities = client.initialize_result.capabilities
        return capabilities.tools.listChanged if capabilities.tools else None


async def _call(url: str, name: str, arguments: dict[str, Any]) -> Any:
    async with Client(url, timeout=8) as client:
        return await client.call_tool(name, arguments, raise_on_error=False)


def _text(result: Any) -> str:
    return "\n".join(
        block.text for block in result.content if getattr(block, "text", None)
    )


def _fixture_url(ports: dict[str, int], name: str, suffix: str) -> str:
    return f"http://127.0.0.1:{ports[name]}/_fixture/{suffix}"


def _plan(ports: dict[str, int], name: str, **values: Any) -> None:
    _request(_fixture_url(ports, name, "plan"), method="POST", body=values)


def _reset(ports: dict[str, int]) -> None:
    for name in FIXTURES:
        _request(_fixture_url(ports, name, "reset"), method="POST", body={})


def _events(ports: dict[str, int], name: str) -> list[dict]:
    return _request(_fixture_url(ports, name, "events"))["events"]


def _definition(backend_ids: dict[str, str]) -> dict[str, Any]:
    """Canonical draft payload for the ADR's stable-reference contract."""
    return {
        "name": "fanout_search",
        "description": "Search both fixture sources concurrently.",
        "dispatch": "all",
        "inputs": [
            {"name": "query", "type": "string", "required": True},
            {"name": "limit", "type": "integer", "required": False, "default": 3},
        ],
        "members": [
            {
                "backend_id": backend_ids["alpha"],
                "tool_original": "source_search",
                "args": {"query": "query", "count": "limit"},
                "timeout": 0.75,
            },
            {
                "backend_id": backend_ids["beta"],
                "tool_original": "source_search",
                "args": {"query": "query", "count": "limit"},
                "timeout": 0.75,
            },
        ],
        "max_result_bytes": 131072,
    }


def _keyword_definition(backend_ids: dict[str, str]) -> dict[str, Any]:
    """A deterministic local router with an explicit single-member fallback."""
    definition = _definition(backend_ids)
    definition.update(
        {
            "name": "keyword_search",
            "description": "Route Python requests to alpha and otherwise beta.",
            "dispatch": "keyword",
            "router": {"fallback": "beta"},
        }
    )
    definition["members"][0].update(
        {"label": "alpha", "route_patterns": [r"\bpython\b|\bcode\b"]}
    )
    definition["members"][1].update(
        {"label": "beta", "route_patterns": [r"\bweather\b"]}
    )
    return definition


def _budget_definition(backend_ids: dict[str, str]) -> dict[str, Any]:
    """An all-dispatch tool with the minimum supported aggregate output budget."""
    definition = _definition(backend_ids)
    definition.update(
        {
            "name": "budget_search",
            "description": "Prove aggregate output-budget accounting and markers.",
            "max_result_bytes": 1024,
        }
    )
    return definition


def _require_keys(value: dict, keys: set[str], label: str) -> None:
    missing = keys - value.keys()
    if missing:
        raise ContractFailure(f"{label} missing required fields: {sorted(missing)}")


def run(keep: bool) -> Path:  # noqa: PLR0915 - one coherent black-box receipt
    scratch = Path(tempfile.mkdtemp(prefix="mcp-gateway-virtual-live-"))
    LAST_SCRATCH[0] = scratch
    for directory in ("fixtures", "logs", "processes", "receipts", "home"):
        (scratch / directory).mkdir()
    environment = {
        **os.environ,
        "HOME": str(scratch / "home"),
        "MCP_GATEWAY_CONFIG": str(scratch / "config.toml"),
        "MCP_GATEWAY_SECRETS": str(scratch / "secrets.env"),
        "MCP_GATEWAY_HOOKS": str(scratch / "hooks"),
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
            process = _spawn(
                [
                    sys.executable,
                    str(REPO_ROOT / "tests/fixtures/virtual_tools_backend.py"),
                    "--name",
                    name,
                    "--port",
                    str(fixture_ports[name]),
                    "--ready-file",
                    str(scratch / "fixtures" / f"{name}.ready.json"),
                    "--event-file",
                    str(scratch / "fixtures" / f"{name}.jsonl"),
                ],
                environment,
                scratch / "processes" / name,
            )
            processes.append(process)
            _wait_for(
                lambda n=name: (scratch / "fixtures" / f"{n}.ready.json").is_file(),
                f"{name} fixture",
            )
            _wait_for(
                lambda n=name: bool(
                    asyncio.run(_tools(f"http://127.0.0.1:{fixture_ports[n]}/mcp"))
                ),
                f"{name} fixture MCP",
            )

        gateway_port = _free_port()
        _write_config(
            scratch / "config.toml",
            gateway_port,
            fixture_ports,
            scratch / "logs/gateway.jsonl",
        )
        gateway = _spawn(
            [sys.executable, "-c", "from mcp_gateway.server import main; main()"],
            environment,
            scratch / "processes/gateway",
        )
        processes.append(gateway)
        base = f"http://127.0.0.1:{gateway_port}"
        _wait_for(
            lambda: urllib.request.urlopen(f"{base}/health", timeout=1).status == 200,
            "gateway health",
        )  # noqa: S310 - loopback only
        _wait_for(lambda: _request(f"{base}/ready")["ready"], "backend readiness")

        virtual_url = f"{base}/virtual/mcp"
        try:
            empty_tools = asyncio.run(_tools(virtual_url))
        except Exception as exc:  # noqa: BLE001 - this is the first ADR receipt
            raise ContractFailure(
                "/virtual/mcp must be mounted and initialize with zero active tools; "
                f"client error: {type(exc).__name__}: {exc}"
            ) from exc
        if empty_tools:
            raise ContractFailure("/virtual/mcp must list zero tools before activation")
        receipt["checks"].append("always-mounted empty virtual endpoint")
        if asyncio.run(_tools_list_changed(virtual_url)) is not False:
            raise ContractFailure(
                "/virtual/mcp must not advertise tools.listChanged until it can "
                "notify every connected downstream session"
            )
        receipt["checks"].append("truthful tools.listChanged capability")

        catalog = _request(f"{base}/admin/api/virtual-catalog")
        backend_ids = {item["name"]: item["id"] for item in catalog["backends"]}
        definition = _definition(backend_ids)
        draft = _request(
            f"{base}/admin/api/virtual-tools", method="POST", body=definition
        )
        _require_keys(draft, {"tool", "lifecycle"}, "create draft response")
        tool_id = draft["tool"]["name"]
        if draft["lifecycle"] != "draft":
            raise ContractFailure(
                f"new virtual tool must be draft, got {draft['lifecycle']!r}"
            )
        listed = _request(f"{base}/admin/api/virtual-tools")
        if tool_id not in [item.get("name") for item in listed.get("tools", [])]:
            raise ContractFailure("draft was not returned by the Virtual Tools list")
        receipt["checks"].append("admin create and draft listing")

        configured = _request(
            f"{base}/admin/api/virtual-tools/{tool_id}",
            method="PUT",
            body=definition,
        )
        _require_keys(configured, {"ok", "tool"}, "configure response")
        resolved = _request(
            f"{base}/admin/api/virtual-tools/{tool_id}/validate", method="POST", body={}
        )
        _require_keys(resolved, {"ok", "members"}, "validate response")
        if not resolved["ok"] or any(
            not member.get("resolved") for member in resolved["members"]
        ):
            raise ContractFailure(f"live references did not resolve: {resolved}")
        receipt["checks"].append("configure plus stable-reference resolution")

        tested = _request(
            f"{base}/admin/api/virtual-tools/{tool_id}/test",
            method="POST",
            body={"arguments": {"query": "draft test", "limit": 2}},
        )
        _require_keys(tested, {"ok", "result"}, "live test response")
        if not tested["ok"]:
            raise ContractFailure(f"draft live test failed: {tested}")
        activated = _request(
            f"{base}/admin/api/virtual-tools/{tool_id}/activate", method="POST", body={}
        )
        if not activated.get("enabled"):
            raise ContractFailure(
                f"activation did not return active state: {activated}"
            )
        receipt["checks"].append("live test then activation")

        names = [tool.name for tool in asyncio.run(_tools(virtual_url))]
        if "fanout_search" not in names:
            raise ContractFailure(f"active virtual tool was not broadcast: {names}")

        _plan(fixture_ports, "alpha", delay=0.4, fail=False, result_mode="text")
        _plan(fixture_ports, "beta", delay=0.4, fail=False, result_mode="text")
        _reset(fixture_ports)
        started = time.monotonic()
        result = asyncio.run(_call(virtual_url, "fanout_search", {"query": "parallel"}))
        elapsed = time.monotonic() - started
        starts = [
            event["at"]
            for name in FIXTURES
            for event in _events(fixture_ports, name)
            if event["kind"] == "call_started"
        ]
        if (
            result.is_error
            or elapsed >= 0.75
            or len(starts) != 2
            or abs(starts[0] - starts[1]) >= 0.2
        ):
            raise ContractFailure(
                f"all-dispatch was not concurrent: {elapsed=:.3f}, {starts=}"
            )
        receipt["checks"].append(f"all dispatch concurrency ({elapsed:.3f}s)")

        _plan(fixture_ports, "alpha", delay=0, fail=False, result_mode="rich")
        direct = asyncio.run(
            _call(f"{base}/alpha/mcp", "source_search", {"query": "rich"})
        )
        direct_kinds = {getattr(block, "type", None) for block in direct.content}
        required_kinds = {"text", "image", "audio", "resource", "resource_link"}
        if (
            direct.is_error
            or not required_kinds <= direct_kinds
            or direct.structured_content is None
        ):
            raise ContractFailure(
                "fixture rich result did not survive the direct proxy endpoint"
            )
        aggregate = asyncio.run(_call(virtual_url, "fanout_search", {"query": "rich"}))
        aggregate_kinds = {getattr(block, "type", None) for block in aggregate.content}
        if (
            aggregate.is_error
            or not required_kinds <= aggregate_kinds
            or aggregate.structured_content is None
        ):
            raise ContractFailure(
                "virtual aggregate silently lost rich MCP content or structured output"
            )
        receipt["checks"].append("rich content and structured output preservation")

        _plan(fixture_ports, "alpha", result_mode="text", fail=False)
        _plan(fixture_ports, "beta", result_mode="text", fail=True)
        partial = asyncio.run(_call(virtual_url, "fanout_search", {"query": "partial"}))
        if partial.is_error or "beta" not in _text(partial).lower():
            raise ContractFailure(
                "partial failure was not represented as a labeled result"
            )
        _plan(fixture_ports, "beta", delay=1.0, fail=False)
        timeout = asyncio.run(_call(virtual_url, "fanout_search", {"query": "timeout"}))
        if timeout.is_error or "timeout" not in _text(timeout).lower():
            raise ContractFailure(
                "member timeout was not represented as a labeled result"
            )
        _plan(fixture_ports, "alpha", fail=True)
        _plan(fixture_ports, "beta", delay=0, fail=True)
        total = asyncio.run(_call(virtual_url, "fanout_search", {"query": "total"}))
        if not total.is_error:
            raise ContractFailure("all-member failure must fail the virtual-tool call")
        receipt["checks"].append("partial, timeout, and total failures")

        _plan(fixture_ports, "alpha", delay=0, fail=False, result_mode="text")
        _plan(fixture_ports, "beta", delay=0, fail=False, result_mode="text")
        keyword = _keyword_definition(backend_ids)
        created = _request(
            f"{base}/admin/api/virtual-tools", method="POST", body=keyword
        )
        if created.get("tool", {}).get("name") != keyword["name"]:
            raise ContractFailure(f"keyword tool was not created: {created}")
        activated = _request(
            f"{base}/admin/api/virtual-tools/{keyword['name']}/activate",
            method="POST",
            body={},
        )
        if not activated.get("enabled"):
            raise ContractFailure(f"keyword tool did not activate: {activated}")

        _reset(fixture_ports)
        started = time.monotonic()
        matched = asyncio.run(
            _call(virtual_url, keyword["name"], {"query": "Python code help"})
        )
        matched_ms = round((time.monotonic() - started) * 1000, 1)
        matched_events = {name: _events(fixture_ports, name) for name in FIXTURES}
        if (
            matched.is_error
            or matched.structured_content.get("selected") != ["alpha"]
            or len(
                [
                    item
                    for item in matched_events["alpha"]
                    if item["kind"] == "call_started"
                ]
            )
            != 1
            or any(item["kind"] == "call_started" for item in matched_events["beta"])
        ):
            raise ContractFailure(
                "keyword match did not select only alpha: "
                f"{matched.structured_content=}, {matched_events=}"
            )

        _reset(fixture_ports)
        started = time.monotonic()
        fallback = asyncio.run(
            _call(virtual_url, keyword["name"], {"query": "unmatched request"})
        )
        fallback_ms = round((time.monotonic() - started) * 1000, 1)
        fallback_events = {name: _events(fixture_ports, name) for name in FIXTURES}
        if (
            fallback.is_error
            or fallback.structured_content.get("selected") != ["beta"]
            or len(
                [
                    item
                    for item in fallback_events["beta"]
                    if item["kind"] == "call_started"
                ]
            )
            != 1
            or any(item["kind"] == "call_started" for item in fallback_events["alpha"])
        ):
            raise ContractFailure(
                "keyword fallback did not select only beta: "
                f"{fallback.structured_content=}, {fallback_events=}"
            )
        receipt["keyword_dispatch"] = {
            "matched_selected": matched.structured_content["selected"],
            "matched_ms": matched_ms,
            "fallback_selected": fallback.structured_content["selected"],
            "fallback_ms": fallback_ms,
        }
        receipt["checks"].append("keyword dispatch plus explicit beta fallback")

        budget = _budget_definition(backend_ids)
        created = _request(
            f"{base}/admin/api/virtual-tools", method="POST", body=budget
        )
        if created.get("tool", {}).get("name") != budget["name"]:
            raise ContractFailure(f"budget tool was not created: {created}")
        activated = _request(
            f"{base}/admin/api/virtual-tools/{budget['name']}/activate",
            method="POST",
            body={},
        )
        if not activated.get("enabled"):
            raise ContractFailure(f"budget tool did not activate: {activated}")
        _plan(fixture_ports, "alpha", result_mode="large", fail=False, delay=0)
        _plan(fixture_ports, "beta", result_mode="text", fail=False, delay=0)
        truncated = asyncio.run(_call(virtual_url, budget["name"], {"query": "budget"}))
        marker = next(
            (
                block.text
                for block in truncated.content
                if getattr(block, "text", "").startswith("[mcp-gateway: output budget")
            ),
            None,
        )
        budget_meta = (truncated.meta or {}).get("mcp-gateway/virtual")
        omitted = truncated.structured_content.get("budget", {}).get("omitted", [])
        if (
            truncated.is_error
            or marker is None
            or "output budget 1024 bytes" not in marker
            or not omitted
            or budget_meta is None
            or budget_meta.get("omitted") != omitted
        ):
            raise ContractFailure(
                "aggregate budget lacked an explicit marker/metadata: "
                f"{marker=}, {budget_meta=}, {omitted=}"
            )
        receipt["aggregate_budget"] = {
            "limit_bytes": 1024,
            "marker": marker,
            "omitted": omitted,
        }
        receipt["checks"].append("aggregate budget truncation marker and metadata")

        renamed = _request(
            f"{base}/admin/api/backend/alpha/rename",
            method="POST",
            body={"value": "alpha_renamed"},
        )
        if not renamed.get("ok"):
            raise ContractFailure(
                f"backend rename failed during stable-reference receipt: {renamed}"
            )
        after_rename = next(
            item
            for item in _request(f"{base}/admin/api/virtual-tools")["tools"]
            if item["name"] == tool_id
        )["resolution"]
        if any(
            not member.get("resolved") for member in after_rename.get("members", [])
        ):
            raise ContractFailure(
                "backend rename broke a stable virtual-tool reference"
            )
        receipt["checks"].append("stable reference survives backend rename")

        receipt["status"] = "passed"
        passed = True
        return scratch
    except Exception as exc:  # noqa: BLE001 - receipt must retain the root cause
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
    parser.add_argument(
        "--keep", action="store_true", help="retain passing receipts too"
    )
    args = parser.parse_args()
    try:
        scratch = run(args.keep)
    except Exception as exc:  # noqa: BLE001 - command-line receipt summary
        print(f"VIRTUAL TOOLS RECEIPTS FAILED: {exc}", file=sys.stderr)
        if LAST_SCRATCH[0] is not None:
            print(f"ARTIFACTS RETAINED: {LAST_SCRATCH[0]}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"VIRTUAL TOOLS RECEIPTS PASSED: {scratch}")


if __name__ == "__main__":
    main()
