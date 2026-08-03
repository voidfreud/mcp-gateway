#!/usr/bin/env python3
"""Measure disposable resident gateway footprint across clean restart cycles.

This never touches the installed LaunchAgent, user config, credentials, or live
backends.  It reports observations rather than enforcing an arbitrary memory
ceiling; reviewers can compare first/last RSS and the sample spread.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http(url: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:  # noqa: S310 - loopback fixture
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"{url} did not become ready: {last_error}")


def _sample(pid: int) -> tuple[int, float]:
    result = subprocess.run(
        ["/bin/ps", "-o", "rss=,pcpu=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=True,
    )
    fields = result.stdout.split()
    if len(fields) != 2:
        raise RuntimeError(
            f"could not parse ps output for pid {pid}: {result.stdout!r}"
        )
    return int(fields[0]) * 1024, float(fields[1])


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)


def _cycle(root: Path, number: int, settle: float) -> dict[str, float | int]:
    home = root / f"home-{number}"
    state = root / f"state-{number}"
    home.mkdir()
    state.mkdir()
    port = _free_port()
    config = root / f"config-{number}.toml"
    config.write_text(
        "\n".join(
            (
                'host = "127.0.0.1"',
                f"port = {port}",
                f'log_file = "{state / "gateway.log"}"',
                "backends = []",
                "",
            )
        ),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "HOME": str(home),
        "MCP_GATEWAY_CONFIG": str(config),
        "PYTHONUNBUFFERED": "1",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "mcp_gateway", "--foreground"],
        cwd=REPO_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        _wait_http(f"http://127.0.0.1:{port}/health")
        _wait_http(f"http://127.0.0.1:{port}/ready")
        time.sleep(settle)
        samples = [_sample(process.pid) for _ in range(3)]
        rss_values = [sample[0] for sample in samples]
        cpu_values = [sample[1] for sample in samples]
        return {
            "cycle": number,
            "pid": process.pid,
            "rss_bytes": int(statistics.median(rss_values)),
            "cpu_percent": statistics.median(cpu_values),
        }
    finally:
        _stop(process)


def run(cycles: int, settle: float) -> dict:
    with tempfile.TemporaryDirectory(prefix="mcp-gateway-resident-receipt-") as raw:
        root = Path(raw)
        observations = [_cycle(root, number, settle) for number in range(1, cycles + 1)]
    rss = [int(item["rss_bytes"]) for item in observations]
    return {
        "kind": "disposable-resident-resource-receipt",
        "cycles": observations,
        "rss_first_to_last_bytes": rss[-1] - rss[0],
        "rss_sample_spread_bytes": max(rss) - min(rss),
        "installed_service_touched": False,
        "live_backends_used": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--settle", type=float, default=1.0)
    args = parser.parse_args()
    if args.cycles < 2:
        parser.error("--cycles must be at least 2")
    if args.settle < 0:
        parser.error("--settle must be non-negative")
    print(json.dumps(run(args.cycles, args.settle), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
