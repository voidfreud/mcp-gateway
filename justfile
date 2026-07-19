# mcp-gateway tasks. Run `just` to list, `just check` for the full gate.

log_path := env_var_or_default("MCP_GATEWAY_LOG_FILE", env_var("HOME") + "/.local/state/mcp-gateway/gateway.log")

# Default: show available recipes
default:
    @just --list

# Full local gate: lint + format check + unit/property tests + import smoke +
# one offline release-contract build. The release recipe is intentionally last:
# tests inspect the contract through synthetic fixtures rather than nesting
# another package build inside pytest.
check: lint test smoke release-contract

# Build, inspect, install-check, SBOM, and checksum the local release assets.
# This is strictly local verification: it never tags, publishes, or deploys.
release-contract:
    uv run python tools/release_contract.py build --out-dir dist

# Ruff lint + format check
lint:
    uv run ruff check .
    uv run ruff format --check .

# Pure-logic tests (pytest + hypothesis) — no backends needed
test:
    uv run pytest -q

# Repository-only checks for material that must never enter a release review.
# The complete test suite already includes these; this recipe is the fast,
# explicit pre-review check for local-only paths and deployable templates.
hygiene:
    uv run pytest -q tests/test_release_hygiene.py

# Deterministic, offline validation of tracked Markdown relative paths and
# fragments. External URL reachability remains advisory and manual.
docs-check:
    uv run python tools/docs_links.py

# Advisory type-baseline report. It deliberately preserves ty's real exit
# status in the output while returning success so it does not impersonate a
# required gate before the existing type debt is remediated.
types:
    @uv run ty check; status=$?; if test "$status" -ne 0; then echo "advisory: ty reported existing diagnostics; see the tracked type-debt Issue before promoting this to CI" >&2; fi; exit 0

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
    @curl --fail --silent --show-error --retry 10 --retry-delay 1 --retry-connrefused --retry-max-time 30 http://127.0.0.1:9100/health
    @curl --fail --silent --show-error --retry 10 --retry-delay 1 --retry-connrefused --retry-max-time 30 http://127.0.0.1:9100/ready

# Install/sync the LaunchAgent via the ~/.local/opt symlink (#149).
# Re-run after moving the repo. Preview with: ./install.sh --dry-run
install:
    ./install.sh

# Remove the LaunchAgent, plist, and symlink; keeps config/state (#171).
# Add --purge by hand to also delete those. Preview with --dry-run.
uninstall:
    ./install.sh --uninstall
