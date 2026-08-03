"""Package metadata shared by the daemon and its Admin surface."""

from __future__ import annotations

import importlib.metadata
import re
from functools import cache
from pathlib import Path


@cache
def gateway_version() -> str:
    """Return the installed version, with a checkout fallback.

    Installed wheels resolve this from package metadata.  A source checkout
    without an editable install falls back to the version in the repository's
    ``pyproject.toml`` so health/admin responses remain useful during local
    development.  The value is cached because it is immutable for a process.
    """
    try:
        return importlib.metadata.version("mcp-local-gateway")
    except importlib.metadata.PackageNotFoundError:
        pass
    try:
        project_root = Path(__file__).resolve().parents[2]
        text = (project_root / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
        if match:
            return match.group(1)
    except OSError:
        pass
    return "unknown"
