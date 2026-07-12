#!/bin/bash
# install.sh — wire the mcp-gateway LaunchAgent through the stable symlink
# ~/.local/opt/mcp-gateway (issue #149).
#
# The installed plist references ONLY that symlink, never the clone's real
# path, so the LaunchAgent survives a repo move. **After moving the repo,
# re-running this script is the ONLY step needed** — it repoints the symlink,
# refreshes the installed plist, and restarts the daemon.
#
# Idempotent: safe to run repeatedly. `--dry-run` prints the actions without
# performing any of them.
#
# POSIX-safe for macOS's stock /bin/bash 3.2 (no bash-4 features).

set -eu

LABEL="com.void.mcp-gateway"
LINK="$HOME/.local/opt/mcp-gateway"
AGENTS_DIR="$HOME/Library/LaunchAgents"

# Resolve the repo root from this script's own location (works from any cwd,
# including when invoked through the symlink itself).
REPO_ROOT=$(cd "$(dirname "$0")" && pwd -P)

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=1
elif [ $# -gt 0 ]; then
    echo "usage: $0 [--dry-run]" >&2
    exit 2
fi

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] $*"
    else
        echo "+ $*"
        "$@"
    fi
}

echo "repo root: $REPO_ROOT"

# 1. The venv must exist BEFORE (re)loading the agent, or launchd would
#    crash-loop on a missing interpreter.
if [ ! -x "$REPO_ROOT/.venv/bin/python3" ]; then
    echo "error: $REPO_ROOT/.venv/bin/python3 not found." >&2
    echo "       Run 'uv sync' in $REPO_ROOT first, then re-run this script." >&2
    exit 1
fi

# 2. Create/repoint the stable symlink ~/.local/opt/mcp-gateway -> repo root.
#    -n treats an existing symlink as a file (repoints it instead of
#    descending into it), so a stale link from a previous location is fixed.
run mkdir -p "$HOME/.local/opt"
if [ "$(readlink "$LINK" 2>/dev/null || true)" = "$REPO_ROOT" ]; then
    echo "symlink already current: $LINK -> $REPO_ROOT"
else
    run ln -sfn "$REPO_ROOT" "$LINK"
    if [ "$DRY_RUN" -eq 0 ]; then
        echo "symlink: $LINK -> $REPO_ROOT"
    fi
fi

# 3. Install the plist (it references only the symlink, so this copy is
#    identical no matter where the repo lives).
run mkdir -p "$AGENTS_DIR"
run cp "$REPO_ROOT/$LABEL.plist" "$AGENTS_DIR/$LABEL.plist"

# 4. (Re)load: bootout is best-effort (fails harmlessly when not loaded),
#    bootstrap loads the fresh copy, kickstart -k (re)starts the daemon now.
#    bootout is ASYNC — bootstrapping immediately after it races the removal
#    and fails with "Bootstrap failed: 5: Input/output error" (hit live on the
#    first real install). Poll until the old job is actually gone (bounded),
#    and retry the bootstrap a few times as a belt-and-suspenders.
run_launchctl_reload() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] launchctl bootout gui/$UID/$LABEL (ignore failure)"
        echo "[dry-run] wait until gui/$UID/$LABEL is unloaded (max ~10s)"
        echo "[dry-run] launchctl bootstrap gui/$UID $AGENTS_DIR/$LABEL.plist (retried)"
        echo "[dry-run] launchctl kickstart -k gui/$UID/$LABEL"
        return 0
    fi
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    i=0
    while launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -gt 20 ]; then
            echo "warning: old job still loaded after ~10s; bootstrapping anyway" >&2
            break
        fi
        sleep 0.5
    done
    i=0
    until launchctl bootstrap "gui/$UID" "$AGENTS_DIR/$LABEL.plist"; do
        i=$((i + 1))
        if [ "$i" -gt 5 ]; then
            echo "error: bootstrap kept failing — inspect: launchctl print gui/$UID/$LABEL" >&2
            exit 1
        fi
        sleep 1
    done
    launchctl kickstart -k "gui/$UID/$LABEL"
}
run_launchctl_reload

if [ "$DRY_RUN" -eq 1 ]; then
    echo "dry run complete — nothing was changed."
else
    echo "done. Check: curl -s http://127.0.0.1:9100/health"
    echo "(the /health body names the daemon's resolved code path — it should"
    echo " show this repo's real location: $REPO_ROOT)"
fi
