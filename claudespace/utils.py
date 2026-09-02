"""Small cross-cutting helpers: logging and shell process checks."""

from __future__ import annotations

import logging
import subprocess
import time


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


def launch_iterm(*, timeout: float = 10.0) -> None:
    """Launch iTerm2.app if it is not already running, and wait for it.

    The iTerm2 Python API can only connect to an already-running instance -
    it has no facility to start the app itself. ``open -b`` launches by
    bundle ID, which is immune to the app's display name/path and avoids
    LaunchServices lookup-by-name races right after install.

    ``open`` returns as soon as the launch request is handed off, not once
    the app is actually up - immediately proceeding to connect to the
    Python API races the app's startup, especially right after a fresh
    install. Poll ``pgrep`` until the process appears (or ``timeout``
    elapses) so callers can rely on ``is_iterm_running()`` being true
    afterwards instead of silently racing it.
    """
    subprocess.run(["open", "-b", "com.googlecode.iterm2"], check=True)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_iterm_running():
            return
        time.sleep(0.2)


GHOSTTY_BUNDLE_ID = "com.mitchellh.ghostty"


def is_ghostty_running() -> bool:
    """Check whether Ghostty.app is currently running. Peer of
    ``is_iterm_running`` for the Ghostty backend's own cold-launch handling
    (see ``cli.py``)."""
    result = subprocess.run(
        ["pgrep", "-x", "Ghostty"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def launch_ghostty(*, timeout: float = 10.0) -> None:
    """Launch Ghostty.app if it is not already running, and wait for it.
    Peer of ``launch_iterm``; see its docstring for why ``open -b`` and a
    poll loop are used instead of racing straight into automation."""
    subprocess.run(["open", "-b", GHOSTTY_BUNDLE_ID], check=True)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_ghostty_running():
            return
        time.sleep(0.2)
