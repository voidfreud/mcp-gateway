# mcp-gateway tasks. Run `just` to list, `just check` for the full gate.

log_path := env_var_or_default("MCP_GATEWAY_LOG_FILE", env_var("HOME") + "/.local/state/mcp-gateway/gateway.log")

# Default: show available recipes
default:
    @just --list

# Full local gate: lint + format check + unit/property tests + import smoke.
check: lint test smoke

# Ruff lint + format check
lint:
    uv run ruff check .
    uv run ruff format --check .

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

# Show recent structured JSON events without needing the dashboard.
# Override the path with MCP_GATEWAY_LOG_FILE when using a non-default config.
logs lines="100":
    @if test -f "{{log_path}}"; then tail -n {{lines}} "{{log_path}}"; else echo "log file not found: {{log_path}}" >&2; exit 1; fi

# Follow the active structured log until Ctrl-C.
logs-follow:
    tail -F "{{log_path}}"

# Pull the merged main branch, sync the locked environment, reload launchd,
# and verify the new daemon. This is deliberately explicit and fail-closed:
# never deploy a feature branch, a dirty checkout, or a half-ready service.
update:
    @if test "$(git branch --show-current)" != "main"; then echo "error: just update must run from the main branch" >&2; exit 1; fi
    @if test -n "$(git status --porcelain=v1)"; then echo "error: just update requires a clean checkout" >&2; git status --short >&2; exit 1; fi
    git pull --ff-only origin main
    uv sync --locked
    ./install.sh
    @curl --fail --silent --show-error http://127.0.0.1:9100/health
    @curl --fail --silent --show-error http://127.0.0.1:9100/ready

# Install/sync the LaunchAgent via the ~/.local/opt symlink (#149).
# Re-run after moving the repo. Preview with: ./install.sh --dry-run
install:
    ./install.sh

# Remove the LaunchAgent, plist, and symlink; keeps config/state (#171).
# Add --purge by hand to also delete those. Preview with --dry-run.
uninstall:
    ./install.sh --uninstall
