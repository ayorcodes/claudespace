"""Preflight checks: iTerm2.app presence, its Python API toggle, and the
``claude`` CLI. Each check is cheap and safe to run on every invocation.

claudespace only runs on macOS - iTerm2 has no Windows/Linux build, and its
scripting API this package depends on is Mac-only.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

ITERM_APP_PATHS = (
    "/Applications/iTerm.app",
    os.path.expanduser("~/Applications/iTerm.app"),
)
API_SERVER_DOMAIN = "com.googlecode.iterm2"
API_SERVER_KEY = "EnableAPIServer"


def require_macos() -> None:
    """Exit with a clear error if not running on macOS.

    iTerm2 (and therefore claudespace) does not exist on Windows or Linux.
    """
    if sys.platform != "darwin":
        logger.error(
            "claudespace only works on macOS (it drives iTerm2, which has "
            "no Windows/Linux build)."
        )
        sys.exit(1)


def is_iterm_installed() -> bool:
    """Check whether iTerm.app is present under a known Applications path."""
    return any(os.path.isdir(path) for path in ITERM_APP_PATHS)


def is_brew_available() -> bool:
    return shutil.which("brew") is not None


def install_iterm_via_brew() -> bool:
    """Prompt to install iTerm2 via Homebrew. Returns True on success."""
    if not is_brew_available():
        logger.error(
            "iTerm2 is not installed and Homebrew is not available. "
            "Install iTerm2 manually from https://iterm2.com and re-run."
        )
        return False

    reply = input("iTerm2 is not installed. Install it now via Homebrew? [y/N] ")
    if reply.strip().lower() not in ("y", "yes"):
        logger.error("iTerm2 is required. Install it from https://iterm2.com and re-run.")
        return False

    logger.info("Installing iTerm2 via 'brew install --cask iterm2'...")
    result = subprocess.run(["brew", "install", "--cask", "iterm2"])
    if result.returncode != 0:
        logger.error("Homebrew install failed. Install iTerm2 manually from https://iterm2.com.")
        return False
    return True


def is_python_api_enabled() -> bool:
    """Check iTerm2's 'Enable Python API' preference via `defaults read`."""
    result = subprocess.run(
        ["defaults", "read", API_SERVER_DOMAIN, API_SERVER_KEY],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "1"


def enable_python_api() -> None:
    """Turn on iTerm2's Python API preference via `defaults write`.

    Takes effect after iTerm2 is (re)started - callers should tell the user
    to restart iTerm2 if it was already running.
    """
    subprocess.run(
        ["defaults", "write", API_SERVER_DOMAIN, API_SERVER_KEY, "-bool", "true"],
        check=True,
    )


def is_claude_installed() -> bool:
    return shutil.which("claude") is not None


def ensure_environment(*, iterm_was_running: bool) -> None:
    """Run all preflight checks, fixing what can be fixed automatically.

    Exits the process with a clear message if a hard requirement (iTerm2,
    the ``claude`` CLI) can't be satisfied.
    """
    require_macos()

    if not is_claude_installed():
        logger.error(
            "The 'claude' CLI was not found on PATH. Install Claude Code "
            "(https://claude.com/claude-code) and re-run."
        )
        sys.exit(1)

    if not is_iterm_installed():
        if not install_iterm_via_brew():
            sys.exit(1)

    if not is_python_api_enabled():
        logger.info("Enabling iTerm2's Python API preference...")
        enable_python_api()
        if iterm_was_running:
            logger.error(
                "iTerm2's Python API was just enabled but requires a "
                "restart to take effect. Quit and reopen iTerm2, then "
                "re-run claudespace."
            )
            sys.exit(1)
