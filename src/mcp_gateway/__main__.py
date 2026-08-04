"""Installed entrypoint with a dependency-free version fast path."""

from __future__ import annotations

import sys

from mcp_gateway.metadata import gateway_version

__all__ = ["main"]


def main() -> None:
    """Run the installed command, keeping plain version probes dependency-free."""
    if sys.argv[1:] in (["--version"], ["version"]):
        print(f"mcp-gateway {gateway_version()}")
        return

    from mcp_gateway.cli import main as cli_main  # noqa: PLC0415

    cli_main()


if __name__ == "__main__":
    main()
