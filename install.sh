#!/bin/sh
# Install claudespace via pipx.
#
#   curl -fsSL https://raw.githubusercontent.com/ayorcodes/claudespace/main/install.sh | sh
#
# claudespace is macOS-only (its supported terminal setups - iTerm2's
# Python API, or tmux + a viewer app - have no Windows/Linux build) - this
# script refuses to run anywhere else.

set -eu

REPO_URL="https://github.com/ayorcodes/claudespace.git"
MIN_PYTHON_MINOR=12
CLEANUP_DIR=""
cleanup() {
    [ -n "$CLEANUP_DIR" ] && rm -rf "$CLEANUP_DIR"
    return 0
}
trap cleanup EXIT INT TERM

die() {
    echo "" >&2
    echo "error: $*" >&2
    exit 1
}

# Refuse early, before cloning anything: there is no point downloading a
# repo onto a machine that can never run it.
if [ "$(uname -s)" != "Darwin" ]; then
    die "claudespace only works on macOS (its supported terminal setups have no Windows/Linux build)."
fi

# When run as `curl ... | sh`, $0 is the shell itself, not this file, so
# dirname "$0" resolves to the caller's cwd rather than the repo root.
# Detect that case (this file won't actually exist at $0) and clone the
# repo into a temp dir instead of trusting the cwd.
if [ -f "$0" ] && [ "$(basename "$0")" = "install.sh" ]; then
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
else
    command -v git >/dev/null 2>&1 ||
        die "git is required to install claudespace via curl | sh."
    CLEANUP_DIR="$(mktemp -d)" || die "could not create a temporary directory."
    echo "Cloning claudespace into a temporary directory..."
    git clone --depth 1 "$REPO_URL" "$CLEANUP_DIR" >&2
    SCRIPT_DIR="$CLEANUP_DIR"
fi

# --- Homebrew -------------------------------------------------------------
# `command -v brew` alone is a false negative in a non-login `sh` on Apple
# Silicon, where /opt/homebrew/bin is not on PATH - which told users with a
# perfectly good Homebrew that they had none.
find_brew() {
    if command -v brew >/dev/null 2>&1; then command -v brew; return 0; fi
    for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
        [ -x "$candidate" ] && echo "$candidate" && return 0
    done
    return 1
}

# --- Python ---------------------------------------------------------------
# claudespace needs >=3.12 (see pyproject.toml). pipx otherwise builds
# against whatever interpreter pipx itself runs under - macOS still ships
# 3.9 - and the failure surfaces as a raw pip "requires a different Python"
# error with no hint about what to do.
find_python() {
    for candidate in \
        python3.14 python3.13 python3.12 \
        /opt/homebrew/bin/python3 /usr/local/bin/python3 python3
    do
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, $MIN_PYTHON_MINOR) else 1)" 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
    if BREW="$(find_brew)"; then
        echo "No Python 3.$MIN_PYTHON_MINOR+ found. Installing one via Homebrew..."
        "$BREW" install python@3.13 || die "could not install Python via Homebrew."
        PYTHON="$(find_python || true)"
    fi
fi
[ -n "$PYTHON" ] || die "claudespace needs Python 3.$MIN_PYTHON_MINOR or newer.
Install it (https://www.python.org/downloads/macos/ or 'brew install python@3.13') and re-run."
echo "Using $PYTHON ($("$PYTHON" --version 2>&1))"

# --- pipx -----------------------------------------------------------------
if ! command -v pipx >/dev/null 2>&1; then
    if BREW="$(find_brew)"; then
        echo "Installing pipx via Homebrew..."
        "$BREW" install pipx || die "'brew install pipx' failed. Install pipx manually (https://pipx.pypa.io) and re-run."
    else
        echo "pipx not found; installing it with $PYTHON..."
        "$PYTHON" -m pip install --user --quiet pipx ||
            die "could not install pipx.
Install Homebrew (https://brew.sh) or pipx (https://pipx.pypa.io) manually and re-run."
        USER_BIN="$("$PYTHON" -c 'import site, os; print(os.path.join(site.USER_BASE, "bin"))' 2>/dev/null)"
        [ -n "$USER_BIN" ] || USER_BIN="$HOME/.local/bin"
        PATH="$USER_BIN:$PATH"
        export PATH
    fi
fi
command -v pipx >/dev/null 2>&1 || die "pipx still isn't on PATH after installing it."

# `pipx ensurepath` appends to a shell rc file. If that file does not end in
# a newline, the appended export is concatenated onto whatever the last line
# was, destroying both. Make sure every rc file it might touch ends cleanly
# first. (This has bitten real users - it silently breaks unrelated PATH
# entries, and the damage is only visible by reading the file.)
for rc in "$HOME/.zshrc" "$HOME/.zprofile" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
    [ -f "$rc" ] || continue
    [ -s "$rc" ] || continue
    if [ -n "$(tail -c 1 "$rc")" ]; then
        echo "" >> "$rc"
        echo "Added a missing trailing newline to $rc (pipx would otherwise corrupt its last line)."
    fi
done

# Always run this, not just after a fresh pipx install: a pre-existing pipx
# may never have had `ensurepath` run, which is exactly what leaves
# PIPX_BIN_DIR off PATH even though this script's own install succeeds.
#
# Output is discarded, including stderr: when PATH is already set up pipx
# prints a yellow "try again with the '--force' flag" advisory there and
# exits non-zero, which reads like a failure mid-install even though nothing
# is wrong. The PATH check near the end of this script is what actually
# verifies the outcome, so this call's own report adds nothing.
pipx ensurepath >/dev/null 2>&1 || true

echo "Installing claudespace from $SCRIPT_DIR..."
# Uninstall first rather than `pipx install --force`. Two reasons, both
# verified against pipx's actual behaviour:
#   - `--python` is silently IGNORED when `--force` is passed and a venv
#     already exists, so a re-install would keep whatever (possibly too old)
#     interpreter the previous install used.
#   - `--force` reuses the existing venv, so console scripts that a newer
#     version no longer declares are left behind in PIPX_BIN_DIR forever.
pipx uninstall claudespace >/dev/null 2>&1 || true
pipx install --python "$PYTHON" "$SCRIPT_DIR"

BIN_DIR="$(pipx environment --value PIPX_BIN_DIR)"

echo "Registering bundled commands and prompts..."
"$BIN_DIR/claudespace-sync-assets"

# --- one-time environment setup ------------------------------------------
# A usable terminal setup (iTerm2 + its Python API, or tmux + a viewer),
# and the claude CLI, used to be checked only on the first real
# `claudespace` run, which meant a new user was bounced out of the tool two
# or three times before it did anything. Do it here instead. --no-launch
# keeps the installer from opening iTerm2.
echo "Checking for a supported terminal setup..."
"$BIN_DIR/claudespace" doctor --yes --no-launch || DOCTOR_FAILED=1

# --- verify PATH ----------------------------------------------------------
# `pipx ensurepath` edits an rc file; it cannot change the shell that is
# running right now. Check a fresh login shell so we report what the user
# will actually get, rather than guessing.
if ! zsh -lic 'command -v claudespace >/dev/null 2>&1' >/dev/null 2>&1; then
    echo ""
    echo "warning: 'claudespace' is not on PATH in a new login shell." >&2
    echo "Add this to your shell profile and restart your terminal:" >&2
    echo "    export PATH=\"$BIN_DIR:\$PATH\"" >&2
fi

echo ""
if [ "${DOCTOR_FAILED:-0}" = "1" ]; then
    echo "claudespace is installed, but setup is incomplete - see the messages above."
    echo "Fix those, then run 'claudespace doctor' to re-check."
else
    echo "Done. Run 'claudespace' from any project folder to open a workspace."
fi
echo "(If 'claudespace' isn't found, open a new shell so pipx's PATH changes take effect.)"
