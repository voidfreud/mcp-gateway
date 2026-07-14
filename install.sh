#!/bin/bash
# install.sh — wire the mcp-gateway LaunchAgent through the stable symlink
# ~/.local/opt/mcp-gateway (issue #149).
#
# The installed plist references ONLY that symlink, never the clone's real
# path, so the LaunchAgent survives a repo move. **After moving the repo,
# re-running this script is the ONLY step needed** — it repoints the symlink,
# refreshes the installed plist, and restarts the daemon.
#
# `--uninstall` reverses everything this script installs: bootout + remove the
# LaunchAgent plist, remove the symlink. User data (config, state/logs/backups)
# is deliberately KEPT unless `--purge` is added. Claude Code registrations are
# never touched — the script prints how to remove them.
#
# Idempotent: safe to run repeatedly (install and uninstall alike). `--dry-run`
# prints the actions without performing any of them and composes with both.
#
# POSIX-safe for macOS's stock /bin/bash 3.2 (no bash-4 features).

set -eu

LABEL="com.void.mcp-gateway"
LINK="$HOME/.local/opt/mcp-gateway"
AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENTS_DIR/$LABEL.plist"
CONFIG_DIR="$HOME/.config/mcp-gateway"
STATE_DIR="$HOME/.local/state/mcp-gateway"

# Resolve the repo root from this script's own location (works from any cwd,
# including when invoked through the symlink itself).
REPO_ROOT=$(cd "$(dirname "$0")" && pwd -P)

DRY_RUN=0
UNINSTALL=0
PURGE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --purge) PURGE=1; UNINSTALL=1 ;;  # --purge implies --uninstall
        *)
            echo "usage: $0 [--dry-run] [--uninstall [--purge]]" >&2
            exit 2
            ;;
    esac
done

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] $*"
    else
        echo "+ $*"
        "$@"
    fi
}

# ---------------------------------------------------------------------------
# --uninstall: the exact reverse of the install below. Removes ONLY what
# install created (LaunchAgent + plist + symlink); config and state are user
# data and stay put unless --purge. Idempotent: with nothing installed it
# says so and exits 0.
# ---------------------------------------------------------------------------
if [ "$UNINSTALL" -eq 1 ]; then
    LOADED=0
    if command -v launchctl >/dev/null 2>&1 \
            && launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
        LOADED=1
    fi

    if [ "$LOADED" -eq 0 ] && [ ! -f "$PLIST" ] && [ ! -e "$LINK" ] \
            && [ ! -L "$LINK" ] && [ "$PURGE" -eq 0 ]; then
        echo "nothing installed — no LaunchAgent, plist, or symlink found."
        echo "already uninstalled (or never installed); nothing to do."
        exit 0
    fi

    # 1. Unload the LaunchAgent. bootout is ASYNC (same race the installer
    #    handles) — wait until the job is actually gone, bounded.
    if [ "$LOADED" -eq 1 ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "[dry-run] launchctl bootout gui/$UID/$LABEL"
            echo "[dry-run] wait until gui/$UID/$LABEL is unloaded (max ~10s)"
        else
            launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
            i=0
            while launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; do
                i=$((i + 1))
                if [ "$i" -gt 20 ]; then
                    echo "warning: job still loaded after ~10s; continuing" >&2
                    break
                fi
                sleep 0.5
            done
            echo "removed: LaunchAgent gui/$UID/$LABEL (booted out)"
        fi
    else
        echo "LaunchAgent not loaded — skipping bootout."
    fi

    # 2. The installed plist.
    if [ -f "$PLIST" ]; then
        run rm "$PLIST"
        if [ "$DRY_RUN" -eq 0 ]; then
            echo "removed: $PLIST"
        fi
    else
        echo "no installed plist at $PLIST — skipping."
    fi

    # 3. The stable symlink (-L also catches a broken link).
    if [ -L "$LINK" ] || [ -e "$LINK" ]; then
        run rm "$LINK"
        if [ "$DRY_RUN" -eq 0 ]; then
            echo "removed: $LINK"
        fi
    else
        echo "no symlink at $LINK — skipping."
    fi

    # 4. --purge: delete config + state after an explicit confirm. This is
    #    irreversible — the config backups live under the state dir.
    if [ "$PURGE" -eq 1 ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "[dry-run] rm -rf $CONFIG_DIR"
            echo "[dry-run] rm -rf $STATE_DIR"
        elif [ ! -e "$CONFIG_DIR" ] && [ ! -e "$STATE_DIR" ]; then
            echo "purge: no config or state directories found — nothing to delete."
        else
            echo ""
            echo "--purge will PERMANENTLY delete (backups live under state):"
            if [ -e "$CONFIG_DIR" ]; then echo "  $CONFIG_DIR"; fi
            if [ -e "$STATE_DIR" ]; then echo "  $STATE_DIR"; fi
            printf "type 'yes' to confirm: "
            read -r answer
            if [ "$answer" = "yes" ]; then
                rm -rf "$CONFIG_DIR" "$STATE_DIR"
                echo "removed: $CONFIG_DIR"
                echo "removed: $STATE_DIR"
            else
                echo "purge skipped (not confirmed) — config and state kept."
            fi
        fi
    fi

    # 5. Honest summary: what was deliberately left behind.
    echo ""
    if [ "$PURGE" -eq 0 ]; then
        echo "kept (delete by hand if you want them gone, or re-run with --purge):"
        echo "  config:              $CONFIG_DIR (and ./config.toml in the clone, if any)"
        echo "  state/logs/backups:  $STATE_DIR"
    fi
    echo "kept: the clone itself at $REPO_ROOT"
    echo "kept: Claude Code registrations (cannot be removed safely from here) —"
    echo "      remove each with: claude mcp remove gateway-<name>"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "dry run complete — nothing was changed."
    else
        echo "uninstall complete."
    fi
    exit 0
fi

echo "repo root: $REPO_ROOT"

# 1. The venv (and its mcp-gateway console script) must exist BEFORE
#    (re)loading the agent, or launchd would crash-loop on a missing binary.
#    Bootstrap it automatically when uv is available — out-of-the-box install.
if [ ! -x "$REPO_ROOT/.venv/bin/mcp-gateway" ]; then
    if command -v uv >/dev/null 2>&1; then
        echo "no venv yet — running 'uv sync' to create it"
        run uv sync --project "$REPO_ROOT"
    else
        echo "error: $REPO_ROOT/.venv/bin/mcp-gateway not found and 'uv' is not installed." >&2
        echo "       Install uv (https://docs.astral.sh/uv/), then re-run this script." >&2
        exit 1
    fi
fi
if [ "$DRY_RUN" -eq 0 ] && [ ! -x "$REPO_ROOT/.venv/bin/mcp-gateway" ]; then
    echo "error: uv sync did not produce .venv/bin/mcp-gateway — inspect the output above." >&2
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

# 3. Render the plist template for THIS user (the repo carries no personal
#    paths) and install it. The rendered file references only the symlink, so
#    the copy is identical no matter where the repo lives.
TEMPLATE="$REPO_ROOT/deploy/$LABEL.plist.template"
if [ ! -f "$TEMPLATE" ]; then
    echo "error: plist template missing: $TEMPLATE" >&2
    exit 1
fi
run mkdir -p "$AGENTS_DIR"
if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] render $TEMPLATE (@@HOME@@ -> $HOME) -> $AGENTS_DIR/$LABEL.plist"
else
    sed "s|@@HOME@@|$HOME|g" "$TEMPLATE" > "$AGENTS_DIR/$LABEL.plist"
    echo "rendered plist -> $AGENTS_DIR/$LABEL.plist"
    if command -v plutil >/dev/null 2>&1; then
        plutil -lint -s "$AGENTS_DIR/$LABEL.plist" || {
            echo "error: rendered plist failed plutil lint" >&2; exit 1; }
    fi
fi

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
