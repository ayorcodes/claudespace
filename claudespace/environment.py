"""Preflight checks: iTerm2.app presence, its Python API toggle, and the
``claude`` CLI.

These run at install time (``claudespace doctor``, invoked by ``install.sh``)
and again, cheaply, on every ``claudespace`` invocation. Doing the work at
install time is the point: enabling iTerm2's Python API needs the app to
restart, so a user who first meets that requirement on their first real run
gets bounced out of the tool before it has done anything.

claudespace only runs on macOS - iTerm2 has no Windows/Linux build, and its
scripting API this package depends on is Mac-only.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time

from claudespace import utils
from claudespace.backends import tmux_cli
from claudespace.config import load_tmux_viewer

CMUX_SOCKET_CONTROL_MODE_HELP = (
    "cmux's socket is reachable but refused the connection ('Access denied "
    "- only processes started inside cmux can connect'). This is cmux's "
    "automation.socketControlMode setting, defaulting to 'cmuxOnly' - an "
    "external process (like claudespace) needs it widened. Fix: add "
    '`"automation": {"socketControlMode": "automation"}` to '
    "~/.config/cmux/cmux.json, then run 'cmux reload-config'."
)

logger = logging.getLogger(__name__)

ITERM_APP_PATHS = (
    "/Applications/iTerm.app",
    os.path.expanduser("~/Applications/iTerm.app"),
)
ITERM_BUNDLE_ID = "com.googlecode.iterm2"
API_SERVER_DOMAIN = ITERM_BUNDLE_ID
API_SERVER_KEY = "EnableAPIServer"

# iTerm2 creates this unix socket when its Python API server is actually
# listening. Checking for it is how we tell "the preference is written" (a
# `defaults read`, which says nothing about the running app) apart from "the
# API is genuinely up and connectable" - the distinction that used to force
# an unconditional exit and a manual re-run.
API_SOCKET_PATH = os.path.expanduser(
    "~/Library/Application Support/iTerm2/iterm2-daemon-1.socket"
)

# Keys iTerm2 sets when preferences are loaded from somewhere other than the
# standard domain. `defaults write` against the standard domain is silently
# ignored in that case, which produced an unbreakable "enable the API, then
# re-run" loop with a correct-looking message.
CUSTOM_PREFS_KEYS = ("LoadPrefsFromCustomFolder", "PrefsCustomFolder")


def require_macos() -> None:
    """Exit with a clear error if not running on macOS."""
    if sys.platform != "darwin":
        logger.error(
            "claudespace only works on macOS (it drives iTerm2, which has "
            "no Windows/Linux build)."
        )
        sys.exit(1)


def _defaults_read(key: str) -> str | None:
    result = subprocess.run(
        ["defaults", "read", API_SERVER_DOMAIN, key],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _app_installed(bundle_id: str, app_paths: tuple[str, ...] = ()) -> bool:
    """Whether an app is present anywhere Launch Services knows about.

    Hardcoded ``app_paths`` cover the common cases cheaply; ``mdfind`` by
    bundle ID catches an install somewhere else (a per-user folder, an
    MDM-managed path, a renamed copy) that would otherwise be reported as
    missing - prompting a second, redundant install of an app the user
    already has.
    """
    if any(os.path.isdir(path) for path in app_paths):
        return True
    result = subprocess.run(
        ["mdfind", f"kMDItemCFBundleIdentifier == {bundle_id}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return bool(result.stdout.strip())


def is_iterm_installed() -> bool:
    """Whether iTerm.app is present anywhere Launch Services knows about."""
    return _app_installed(ITERM_BUNDLE_ID, ITERM_APP_PATHS)


def is_ghostty_installed() -> bool:
    """Whether Ghostty.app is present anywhere Launch Services knows about."""
    return _app_installed(utils.GHOSTTY_BUNDLE_ID)


def is_cmux_installed() -> bool:
    """Whether cmux.app is present anywhere Launch Services knows about."""
    return _app_installed(utils.CMUX_BUNDLE_ID)


def is_cmux_reachable(*, timeout: float = 5.0) -> tuple[bool, str | None]:
    """Whether cmux's automation socket actually answers - never inferred
    from a socket-file stat (D4/spike A0: 0600/owner-checked is necessary
    but not sufficient).

    Runs ``cmux ping`` and expects ``PONG``. Returns ``(True, None)`` on
    success; on failure, ``(False, message)`` where ``message`` names the
    exact fix when the failure is the specific 'Access denied' response
    caused by ``automation.socketControlMode`` being left at its default
    ``cmuxOnly`` - the one real surprise the spike found - and a generic
    message otherwise.
    """
    try:
        result = subprocess.run(
            ["cmux", "ping"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return False, f"Could not run 'cmux ping': {exc}"
    if result.returncode == 0 and "PONG" in result.stdout:
        return True, None
    stderr = (result.stderr or "").strip() or (result.stdout or "").strip()
    if "Access denied" in stderr:
        return False, CMUX_SOCKET_CONTROL_MODE_HELP
    return False, stderr or "'cmux ping' failed for an unknown reason"


def is_brew_available() -> bool:
    """Whether Homebrew is usable, checking its standard locations too.

    ``command -v brew`` alone is a false negative in a non-login shell on
    Apple Silicon, where ``/opt/homebrew/bin`` often isn't on PATH - which
    told users with a working Homebrew that they didn't have one.
    """
    if shutil.which("brew"):
        return True
    return any(
        os.path.isfile(path)
        for path in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
    )


def _brew() -> str:
    return shutil.which("brew") or next(
        path
        for path in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
        if os.path.isfile(path)
    )


def install_iterm_via_brew(*, assume_yes: bool = False) -> bool:
    """Install iTerm2 via Homebrew. Returns True on success.

    ``assume_yes`` skips the confirmation prompt - required when running
    non-interactively (from ``install.sh``, or any wrapper), where a bare
    ``input()`` raises ``EOFError`` as an unhandled traceback instead of
    doing anything useful.
    """
    if not is_brew_available():
        logger.error(
            "iTerm2 is not installed and Homebrew is not available. "
            "Install iTerm2 manually from https://iterm2.com and re-run."
        )
        return False

    if not assume_yes:
        if not sys.stdin.isatty():
            logger.error(
                "iTerm2 is not installed. Re-run with 'claudespace doctor "
                "--yes' to install it automatically, or install it from "
                "https://iterm2.com."
            )
            return False
        reply = input("iTerm2 is not installed. Install it now via Homebrew? [y/N] ")
        if reply.strip().lower() not in ("y", "yes"):
            logger.error(
                "iTerm2 is required. Install it from https://iterm2.com and re-run."
            )
            return False

    logger.info("Installing iTerm2 via 'brew install --cask iterm2'...")
    result = subprocess.run([_brew(), "install", "--cask", "iterm2"])
    if result.returncode != 0:
        logger.error(
            "Homebrew install failed. Install iTerm2 manually from https://iterm2.com."
        )
        return False
    return True


def uses_custom_prefs_folder() -> bool:
    """Whether iTerm2 reads its preferences from outside the standard domain."""
    return any(_defaults_read(key) not in (None, "0") for key in CUSTOM_PREFS_KEYS)


def is_python_api_enabled() -> bool:
    """Check iTerm2's 'Enable Python API' preference via `defaults read`."""
    return _defaults_read(API_SERVER_KEY) == "1"


def is_api_listening() -> bool:
    """Whether iTerm2's API server socket exists, i.e. the API is really up."""
    return os.path.exists(API_SOCKET_PATH)


def enable_python_api() -> bool:
    """Turn on iTerm2's Python API preference. Returns False if it didn't take.

    Takes effect only once iTerm2 (re)starts. Returns ``False`` rather than
    raising so a failed write becomes a message the caller can act on
    instead of a bare ``CalledProcessError`` traceback.
    """
    result = subprocess.run(
        ["defaults", "write", API_SERVER_DOMAIN, API_SERVER_KEY, "-bool", "true"],
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        logger.error(
            "Could not enable iTerm2's Python API preference: %s",
            (result.stderr or "").strip(),
        )
        return False
    return True


def is_claude_installed() -> bool:
    return shutil.which("claude") is not None


def wait_for_api(*, timeout: float = 20.0) -> bool:
    """Poll until iTerm2's API socket appears, up to ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_api_listening():
            return True
        time.sleep(0.25)
    return is_api_listening()


def _ensure_api_enabled(*, iterm_was_running: bool, launch: bool) -> bool:
    """Enable the Python API and confirm it is actually listening.

    Previously this wrote the preference and then exited unconditionally,
    telling the user to re-run - so the first ``claudespace`` on any machine
    where the API was off always failed by design, even when iTerm2 wasn't
    running and could simply have been started. Now the only case that still
    needs the user is the one that genuinely does: iTerm2 was already running
    when the preference changed, so it has to be restarted.
    """
    if is_api_listening():
        return True

    if not is_python_api_enabled():
        if uses_custom_prefs_folder():
            logger.error(
                "iTerm2 loads its preferences from a custom folder, so "
                "claudespace cannot enable the Python API for you. Turn it "
                "on manually: iTerm2 > Settings > General > Magic > "
                "'Enable Python API', then re-run."
            )
            return False
        logger.info("Enabling iTerm2's Python API preference...")
        if not enable_python_api():
            return False

    if iterm_was_running:
        logger.error(
            "iTerm2's Python API was just enabled but needs a restart to "
            "take effect. Quit and reopen iTerm2, then re-run claudespace."
        )
        return False

    if not launch:
        # Install-time path: the preference is set and iTerm2 isn't running,
        # so the next real run will start it with the API already on. No
        # reason to launch the app during an install.
        logger.info(
            "iTerm2's Python API is enabled. It will start listening the "
            "next time iTerm2 launches."
        )
        return True

    # iTerm2 isn't running: start it and wait for the API rather than
    # bouncing the user out to run the same command again.
    logger.info("Starting iTerm2 to bring its Python API up...")
    utils.launch_iterm()
    if not wait_for_api():
        logger.error(
            "iTerm2 started but its Python API never came up. Quit and "
            "reopen iTerm2, then re-run claudespace."
        )
        return False
    return True


def _viewer_installed(viewer: str) -> bool:
    """Whether the tmux backend's configured viewer app is present.

    An unknown viewer name returns ``False`` (conservative): it already
    makes ``utils.launch_viewer`` raise, so the tmux backend wouldn't
    function with it regardless of detection.
    """
    bundle_id = utils.VIEWER_BUNDLE_IDS.get(viewer)
    if bundle_id is None:
        return False
    if viewer == "iterm2":
        return is_iterm_installed()
    return _app_installed(bundle_id)


def detect_usable_backends() -> list[str]:
    """Which supported terminal setups are actually usable right now.

    Backend-agnostic: this is doctor/install's shared source of truth for
    "is *any* supported setup usable," trusting no config value as evidence
    of presence - every verdict is backed by a filesystem/``mdfind``/``which``
    probe.
    """
    usable = []
    if is_iterm_installed():
        usable.append("iterm2")
    if tmux_cli.is_tmux_available():
        viewer = load_tmux_viewer()
        if _viewer_installed(viewer):
            usable.append("tmux")
    if is_cmux_installed() and is_cmux_reachable()[0]:
        usable.append("cmux")
    return usable


def run_doctor_checks(
    *, iterm_was_running: bool, assume_yes: bool = False, launch: bool = True
) -> bool:
    """Backend-agnostic doctor/install entry point: is any supported terminal
    setup usable, only installing iTerm2 as a fallback if not.

    Unlike ``check_environment`` (kept for the iTerm2-specific real-run and
    watchdog paths, which must still install iTerm2 when it's the backend
    the user explicitly chose), this never assumes iTerm2 is the target -
    it reports what it found and only falls back to installing iTerm2 when
    nothing usable exists.
    """
    require_macos()
    ok = True

    if not is_claude_installed():
        logger.error(
            "The 'claude' CLI was not found on PATH. Install Claude Code "
            "(https://claude.com/claude-code) and re-run."
        )
        ok = False

    usable = detect_usable_backends()
    if not usable:
        logger.warning(
            "No supported terminal setup found (iTerm2, or tmux + its "
            "viewer)."
        )
        if not install_iterm_via_brew(assume_yes=assume_yes):
            return False
    else:
        logger.info("Found usable terminal setup(s): %s", ", ".join(usable))

    if is_iterm_installed():
        if not _ensure_api_enabled(iterm_was_running=iterm_was_running, launch=launch):
            ok = False

    return ok


def check_environment(
    *, iterm_was_running: bool, assume_yes: bool = False, launch: bool = True
) -> bool:
    """Run every preflight check, fixing what can be fixed. Returns success.

    Unlike the old ``ensure_environment``, this never calls ``sys.exit`` -
    callers decide what a failure means, which is what lets the same checks
    run at install time (where a missing ``claude`` login is a warning) and
    at launch time (where it is fatal).
    """
    require_macos()
    ok = True

    if not is_claude_installed():
        logger.error(
            "The 'claude' CLI was not found on PATH. Install Claude Code "
            "(https://claude.com/claude-code) and re-run."
        )
        ok = False

    if not is_iterm_installed():
        if not install_iterm_via_brew(assume_yes=assume_yes):
            return False

    if not _ensure_api_enabled(iterm_was_running=iterm_was_running, launch=launch):
        ok = False

    return ok


def ensure_environment(*, iterm_was_running: bool) -> None:
    """Launch-time wrapper: run the checks, exit non-zero if any failed."""
    if not check_environment(iterm_was_running=iterm_was_running):
        sys.exit(1)
