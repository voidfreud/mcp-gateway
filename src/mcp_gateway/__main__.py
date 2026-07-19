"""Allow ``python -m mcp_gateway`` to use the packaged daemon entry point."""

from mcp_gateway.server import main

if __name__ == "__main__":
    main()
