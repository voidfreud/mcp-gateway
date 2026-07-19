#!/usr/bin/env python3
"""Run a narrow official MCP conformance smoke suite through mcp-gateway.

This launcher is deliberately **not** a complete MCP certification claim.  The
official runner's full suite requires a backend with specially named tools and
fixed responses (images, audio, sampling, elicitation, and more).  Here a
hermetic FastMCP stdio fixture exercises the gateway's actual per-backend
Streamable HTTP endpoint with the generic official checks that apply now:

* server initialization and lifecycle;
* ping;
* valid tool-list shape; and
* DNS-rebinding protection.

Run manually (it downloads the pinned official Node package on first use):

    uv run python tests/conformance/run_official.py

Passing artifacts are removed by default.  Failures always retain their
scratch directory, including gateway, fixture, and official-runner logs.
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
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name("official_fixture.py")
CONFORMANCE_VERSION = "0.1.16"
SPEC_VERSION = "2025-11-25"
SCENARIOS = (
    "server-initialize",
    "ping",
    "tools-list",
    "dns-rebinding-protection",
)
LAST_SCRATCH: list[Path | None] = [None]


class SmokeFailure(AssertionError):
    """One observable official-conformance smoke failure."""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(predicate: Any, label: str, *, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # noqa: BLE001 - surface the useful startup error
            last_error = exc
        time.sleep(0.1)
    suffix = f"; last error: {last_error}" if last_error else ""
    raise SmokeFailure(f"timed out waiting for {label}{suffix}")


def _spawn(
    command: list[str], environment: dict[str, str], output: Path
) -> subprocess.Popen:
    output.parent.mkdir(parents=True, exist_ok=True)
    with (
        output.with_suffix(".stdout").open("wb") as stdout,
        output.with_suffix(".stderr").open("wb") as stderr,
    ):
        return subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
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


def _write_config(path: Path, *, port: int, log_file: Path) -> None:
    """Create an isolated gateway config with one in-repo stdio backend."""
    path.write_text(
        "\n".join(
            (
                'host = "127.0.0.1"',
                f"port = {port}",
                f"log_file = {json.dumps(str(log_file))}",
                "baseline_max_age = 0",
                "",
                "[[backends]]",
                'name = "conformance"',
                'transport = "stdio"',
                f"command = {json.dumps(sys.executable)}",
                f"args = [{json.dumps(str(FIXTURE))}]",
                "stateless = false",
                "",
            )
        ),
        encoding="utf-8",
    )


def _gateway_is_healthy(base_url: str) -> bool:
    with urllib.request.urlopen(f"{base_url}/health", timeout=1) as response:  # noqa: S310 - loopback only
        return response.status == 200


def _gateway_is_ready(base_url: str) -> bool:
    with urllib.request.urlopen(f"{base_url}/ready", timeout=1) as response:  # noqa: S310 - loopback only
        payload = json.loads(response.read())
    return payload["ready"] is True and payload["mounted"] == ["conformance"]


def _run_scenario(
    scenario: str, endpoint: str, environment: dict[str, str], scratch: Path
) -> None:
    """Run exactly one stable official scenario and retain its complete output."""
    result = subprocess.run(
        [
            "npx",
            "--yes",
            f"@modelcontextprotocol/conformance@{CONFORMANCE_VERSION}",
            "server",
            "--url",
            endpoint,
            "--scenario",
            scenario,
            "--spec-version",
            SPEC_VERSION,
            "--output-dir",
            str(scratch / "official-results"),
        ],
        cwd=REPO_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=90,
    )
    (scratch / "official-logs").mkdir(exist_ok=True)
    (scratch / "official-logs" / f"{scenario}.stdout").write_bytes(result.stdout)
    (scratch / "official-logs" / f"{scenario}.stderr").write_bytes(result.stderr)
    if result.returncode != 0:
        raise SmokeFailure(
            f"official conformance scenario {scenario!r} failed with exit "
            f"{result.returncode}; inspect {scratch / 'official-logs'}"
        )


def run(keep: bool) -> Path:
    """Execute the official smoke subset in a private process/config namespace."""
    scratch = Path(tempfile.mkdtemp(prefix="mcp-gateway-official-conformance-"))
    LAST_SCRATCH[0] = scratch
    for directory in ("home", "logs", "processes", "official-logs", "official-results"):
        (scratch / directory).mkdir()

    environment = {
        **os.environ,
        "HOME": str(scratch / "home"),
        "XDG_CONFIG_HOME": str(scratch / "home" / ".config"),
        "XDG_STATE_HOME": str(scratch / "home" / ".state"),
        "MCP_GATEWAY_CONFIG": str(scratch / "config.toml"),
        "MCP_GATEWAY_SECRETS": str(scratch / "secrets.env"),
        "MCP_GATEWAY_HOOKS": str(scratch / "hooks"),
        "PYTHONPATH": str(REPO_ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
        "PYTHONUNBUFFERED": "1",
    }
    receipt: dict[str, Any] = {
        "conformance_version": CONFORMANCE_VERSION,
        "endpoint": None,
        "kind": "official MCP conformance smoke subset, not full certification",
        "scenarios": list(SCENARIOS),
        "status": "running",
    }
    processes: list[subprocess.Popen] = []
    passed = False
    try:
        port = _free_port()
        _write_config(
            scratch / "config.toml",
            port=port,
            log_file=scratch / "logs/gateway.jsonl",
        )
        gateway = _spawn(
            [sys.executable, "-c", "from mcp_gateway.server import main; main()"],
            environment,
            scratch / "processes/gateway",
        )
        processes.append(gateway)

        base_url = f"http://127.0.0.1:{port}"
        endpoint = f"{base_url}/conformance/mcp"
        receipt["endpoint"] = endpoint
        _wait_for(lambda: _gateway_is_healthy(base_url), "gateway health")
        _wait_for(lambda: _gateway_is_ready(base_url), "stdio backend readiness")

        for scenario in SCENARIOS:
            _run_scenario(scenario, endpoint, environment, scratch)

        receipt["status"] = "passed"
        passed = True
        return scratch
    except Exception as exc:  # noqa: BLE001 - receipt must retain root cause
        receipt["status"] = "failed"
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        (scratch / "report.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8"
        )
        _stop(list(reversed(processes)))
        if passed and not keep:
            shutil.rmtree(scratch)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="retain passing receipts")
    args = parser.parse_args()
    try:
        scratch = run(args.keep)
    except Exception as exc:  # noqa: BLE001 - compact CLI failure receipt
        print(f"OFFICIAL MCP SMOKE FAILED: {exc}", file=sys.stderr)
        if LAST_SCRATCH[0] is not None:
            print(f"ARTIFACTS RETAINED: {LAST_SCRATCH[0]}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"OFFICIAL MCP SMOKE PASSED (subset only; not full certification): {scratch}")


if __name__ == "__main__":
    main()
