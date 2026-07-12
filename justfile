# mcp-gateway tasks. Run `just` to list, `just check` for the full gate.

# Default: show available recipes
default:
    @just --list

# Full local gate: lint + format check + unit/property tests + import smoke.
check: lint test smoke

# Ruff lint + format check
lint:
    uvx ruff check .
    uvx ruff format --check .

# Pure-logic tests (pytest + hypothesis) — no backends needed
test:
    uv run pytest -q

# Import smoke — server/admin/config_loader load cleanly
smoke:
    uv run python -c "import mcp_gateway.server, mcp_gateway.admin, mcp_gateway.config_loader; print('imports OK')"

# End-to-end rename check against the RUNNING daemon (needs live backends)
verify url="http://127.0.0.1:9100":
    uv run verify_rename.py {{url}}

# Restart the launchd daemon
restart:
    launchctl kickstart -k gui/$(id -u)/com.void.mcp-gateway

# Install/sync the LaunchAgent via the ~/.local/opt symlink (#149).
# Re-run after moving the repo. Preview with: ./install.sh --dry-run
install:
    ./install.sh
