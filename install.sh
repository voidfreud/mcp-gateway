#!/bin/bash
# Compatibility wrapper for checkout users. The installed application owns the
# LaunchAgent lifecycle; this script only installs the checkout as a stable uv
# tool and delegates to the same public service commands.

set -eu

REPO_ROOT=$(cd "$(dirname "$0")" && pwd -P)
TOOL="$HOME/.local/bin/mcp-gateway"
DRY_RUN=0
UNINSTALL=0
PURGE=0

for arg in "$@"; do
    case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --purge)
        PURGE=1
        UNINSTALL=1
        ;;
    *)
        echo "usage: $0 [--dry-run] [--uninstall [--purge]]" >&2
        exit 2
        ;;
    esac
done

if [ "$UNINSTALL" -eq 1 ]; then
    data_flag="--keep-data"
    if [ "$PURGE" -eq 1 ]; then data_flag="--purge-data"; fi
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] $TOOL --uninstall-service $data_flag"
        echo "dry run complete — nothing was changed."
        exit 0
    fi
    if [ -x "$TOOL" ]; then
        exec "$TOOL" --uninstall-service "$data_flag"
    fi
    if command -v uv >/dev/null 2>&1; then
        echo "stable tool shim is absent; using the checkout to remove service residue"
        exec uv run --project "$REPO_ROOT" python -m mcp_gateway \
            --uninstall-service "$data_flag"
    fi
    echo "error: $TOOL is missing and uv is unavailable; install uv, then re-run" >&2
    exit 1
fi

if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] uv tool install --force $REPO_ROOT"
    echo "[dry-run] $TOOL --install-service --restart"
    echo "dry run complete — nothing was changed."
    exit 0
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv is required (https://docs.astral.sh/uv/)" >&2
    exit 1
fi

uv tool install --force "$REPO_ROOT"
if [ ! -x "$TOOL" ]; then
    echo "error: uv did not create the stable shim at $TOOL" >&2
    exit 1
fi
exec "$TOOL" --install-service --restart
