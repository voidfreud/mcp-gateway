"""Allow ``python -m mcp_gateway`` to use the packaged daemon entry point."""

from __future__ import annotations

import sys

from mcp_gateway.metadata import gateway_version


def main() -> None:
    """Dispatch the module entry point with a dependency-free version path."""
    if sys.argv[1:] == ["--version"]:
        print(f"mcp-gateway {gateway_version()}")
        return
    from mcp_gateway.server import main as server_main  # noqa: I001, PLC0415 — lazy version path

    server_main()


if __name__ == "__main__":
    main()
