#!/bin/sh
# Committed shim for the npm-distributed claudespace (AD3). Resolves its own
# real location through the npm-created symlink, self-heals a missing venv
# (D5), then execs the matching console script - kept out of node's way on
# the steady-state path so a Stop-hook fire costs no interpreter startup
# beyond the console script itself (D3).
set -eu

resolve_link() {
    link="$1"
    while [ -L "$link" ]; do
        target=$(readlink "$link")
        case "$target" in
            /*) link="$target" ;;
            *) link="$(dirname "$link")/$target" ;;
        esac
    done
    printf '%s\n' "$link"
}

SELF="$(resolve_link "$0")"
BIN_DIR="$(cd "$(dirname "$SELF")" && pwd)"
PKG_ROOT="$(cd "$BIN_DIR/.." && pwd)"

if [ ! -x "$PKG_ROOT/.venv/bin/python" ]; then
    node "$PKG_ROOT/scripts/provision.js"
fi

exec "$PKG_ROOT/.venv/bin/claudespace-tmux-persist" "$@"
