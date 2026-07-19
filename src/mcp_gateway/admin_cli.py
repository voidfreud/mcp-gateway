"""Shared async subprocess policy for client-registration route modules."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from typing import Any


async def run_cli(
    subprocess_run: Callable[..., Any], argv: list[str], timeout: float
) -> tuple[int, str, str]:
    """Run one client CLI invocation off the event loop and never raise.

    A failed spawn or timeout is represented as ``(-1, "", error)`` so the
    caller can return a normal Admin response rather than breaking the event
    loop or leaking a traceback into the dashboard.
    """
    try:
        result = await asyncio.to_thread(
            subprocess_run,
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout, result.stderr
    except (subprocess.SubprocessError, OSError) as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"
