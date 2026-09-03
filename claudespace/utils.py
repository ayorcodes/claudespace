"""Small cross-cutting helpers: logging and shell process checks."""

from __future__ import annotations

import logging
import shutil
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

# Which terminal `launch_viewer` knows how to spawn attaching to a detached
# tmux session (AD5's `[terminal.tmux] viewer`). Ghostty is the default and
# the backend's whole reason for existing; each entry is a one-line lookup
# so another viewer is a small, isolated addition (design's Open Questions).
# Public (not underscore-prefixed) so environment.py's usable-backend
# detection can look up a configured viewer's bundle ID without reaching
# into a private name.
VIEWER_BUNDLE_IDS: dict[str, str] = {
    "ghostty": GHOSTTY_BUNDLE_ID,
    "iterm2": "com.googlecode.iterm2",
}


def is_tmux_available() -> bool:
    """Whether a ``tmux`` binary is on ``PATH``. Peer of ``is_iterm_running``
    for the tmux backend's own preflight (see ``backends/tmux.py``'s
    ``TmuxBackend.run``, which is the actual gate - this is exposed here too
    per the design's Components list, for symmetry with the iTerm2 checks
    cli.py already runs at entry)."""
    return shutil.which("tmux") is not None


def launch_viewer(session: str, *, viewer: str = "ghostty") -> None:
    """Spawn ``viewer`` running ``tmux attach -t <session>`` - how the tmux
    backend makes a detached session visible (AD3/AD5). Unlike
    ``launch_iterm`` (which starts a bare app claudespace then connects an
    API to), this both launches the terminal *and* points it at the right
    tmux session in one step, since a tmux viewer has no separate
    scripting API to drive afterwards - the command line is the whole
    interface.

    Best-effort on the wait: a viewer that fails to launch doesn't corrupt
    anything (Error Handling) - the detached session is untouched and still
    reachable via a manual ``tmux attach -t <session>``, so this only waits
    long enough to make failure visible quickly, and doesn't retry forever.
    """
    bundle_id = VIEWER_BUNDLE_IDS.get(viewer)
    if bundle_id is None:
        raise ValueError(
            f"Unknown tmux viewer '{viewer}'. Known viewers: "
            f"{', '.join(sorted(VIEWER_BUNDLE_IDS))}"
        )
    # `-e` takes the command and its own arguments as separate argv words
    # (like execve), not one shell-style string - passing
    # "tmux attach -t <session>" as a single argument makes the terminal
    # look for a literal binary named that whole string and fail with
    # "No such file or directory".
    #
    # `-L <socket>` matters just as much: every claudespace tmux command
    # runs on the dedicated `claudespace` socket, not the user's default
    # one (AD8) - the session physically lives there. A bare `tmux attach`
    # here (no `-L`) looks on the *default* socket, finds nothing, and
    # fails with "no sessions" even though the real session is right there
    # on the dedicated one - reproduced live: every viewer launch failed
    # this way until this was fixed.
    from claudespace.backends.tmux_cli import SOCKET_NAME

    subprocess.run(
        [
            "open",
            "-b",
            bundle_id,
            "-n",
            "--args",
            "-e",
            "tmux",
            "-L",
            SOCKET_NAME,
            "attach",
            "-t",
            session,
        ],
        check=True,
    )
