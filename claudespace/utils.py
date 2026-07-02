"""Small cross-cutting helpers: logging and shell process checks."""

from __future__ import annotations

import logging
import subprocess


def setup_logging(verbose: bool) -> None:
    """Configure root logging for the CLI."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def is_iterm_running() -> bool:
    """Check whether iTerm2.app is currently running.

    Uses ``pgrep`` rather than AppleScript/UI automation, per the project's
    preference for the official API wherever possible - this is only a
    process existence check, not app control.
    """
    result = subprocess.run(
        ["pgrep", "-x", "iTerm2"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def launch_iterm() -> None:
    """Launch iTerm2.app if it is not already running.

    The iTerm2 Python API can only connect to an already-running instance -
    it has no facility to start the app itself. ``open -a iTerm`` is the
    standard macOS mechanism for launching an app by name and is not UI
    automation.
    """
    subprocess.run(["open", "-a", "iTerm"], check=True)
