#!/bin/sh
# Install claudespace via pipx.
#
#   curl -fsSL https://raw.githubusercontent.com/ayorcodes/claudespace/main/install.sh | sh
#
# claudespace is macOS-only (it drives iTerm2's Python API, which has no
# Windows/Linux build) - this script refuses to run anywhere else.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "claudespace only works on macOS (it drives iTerm2, which has no Windows/Linux build)." >&2
    exit 1
fi

if ! command -v pipx >/dev/null 2>&1; then
    if ! command -v brew >/dev/null 2>&1; then
        echo "pipx is required and Homebrew was not found to install it." >&2
        echo "Install Homebrew (https://brew.sh) or pipx (https://pipx.pypa.io) and re-run." >&2
        exit 1
    fi
    echo "Installing pipx via Homebrew..."
    brew install pipx
    pipx ensurepath
fi

echo "Installing claudespace from $SCRIPT_DIR..."
pipx install --force "$SCRIPT_DIR"

echo "Registering bundled commands and prompts..."
"$(pipx environment --value PIPX_BIN_DIR)/claudespace:sync-assets"

echo "Done. Run 'claudespace' from any project folder to open a workspace."
echo "(If 'claudespace' isn't found, open a new shell so pipx's PATH changes take effect.)"
