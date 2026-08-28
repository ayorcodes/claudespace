"""Connect to iTerm2's Python API with a bound and an actionable error.

``iterm2.run_until_complete(coro, retry=True)`` never gives up. On a 401 it
loops on ``authenticate()`` forever; on ``ConnectionRefusedError``/``OSError``
it sleeps and retries forever. Both present as claudespace hanging with no
output and no timeout - the two worst failures the tool had, because the
user cannot tell them apart from slow work.

Passing ``retry=False`` instead surfaces real exceptions and lets the
library print its own diagnostic, but gives up on the first attempt, which
loses the one case retrying legitimately covers: iTerm2 is still starting
and its socket isn't up yet. So this module retries on its own terms - a
bounded number of attempts - and translates each terminal failure into a
message naming the actual fix.
"""

from __future__ import annotations

import logging
import sys
import time

from claudespace import environment

logger = logging.getLogger(__name__)

CONNECT_ATTEMPTS = 8
CONNECT_BACKOFF_SECONDS = 0.75

# macOS shows the Automation consent dialog ("... wants to control iTerm2")
# the first time a script drives iTerm2 via AppleScript, which is how the
# iterm2 library fetches its auth cookie. A denial is sticky and can only be
# reversed in System Settings, and the library asks iTerm2 to suppress its
# own in-app permission UI, so a denied grant surfaces only as an opaque 401.
TCC_HELP = (
    "iTerm2 refused the connection (authentication failed).\n"
    "This is almost always the macOS Automation permission: the first time "
    "claudespace drives iTerm2, macOS asks whether your terminal may "
    "control it, and a denial sticks.\n"
    "Fix it in System Settings > Privacy & Security > Automation > "
    "<your terminal app> and enable 'iTerm2', then re-run claudespace."
)

REFUSED_HELP = (
    "Could not connect to iTerm2's Python API.\n"
    "  - Make sure iTerm2 is running.\n"
    "  - Make sure its Python API is enabled: iTerm2 > Settings > General > "
    "Magic > 'Enable Python API'.\n"
    "  - If you just enabled it, quit and reopen iTerm2 so it takes effect.\n"
    "Run 'claudespace doctor' to check and repair all of the above."
)


def _is_auth_failure(exc: BaseException) -> bool:
    """Whether ``exc`` is the websocket 401 the library raises on auth failure.

    Matched structurally rather than by importing ``websockets`` ourselves:
    the exception type has moved between websockets releases, and the
    attribute is what the library itself branches on.
    """
    return getattr(exc, "status_code", None) == 401


def run(coro, *, attempts: int = CONNECT_ATTEMPTS) -> None:
    """Run ``coro`` against iTerm2, retrying a bounded number of times.

    Exits the process with a specific message rather than hanging or
    surfacing a bare traceback. ``coro`` is the same
    ``async def (connection) -> None`` shape ``iterm2.run_until_complete``
    takes.
    """
    # Imported here, not at module scope, so a broken venv (a Python
    # upgrade that orphaned it, a bad websockets/protobuf resolution)
    # surfaces as a message naming the fix rather than an ImportError
    # traceback before `claudespace --help` can even print.
    try:
        import iterm2
    except ImportError as exc:
        logger.error(
            "claudespace's 'iterm2' dependency failed to import (%s).\n"
            "Its virtualenv is probably broken - often after a Python "
            "upgrade. Repair it with: pipx reinstall claudespace",
            exc,
        )
        sys.exit(1)

    last: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            iterm2.run_until_complete(coro, retry=False)
            return
        except KeyboardInterrupt:
            raise
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            last = exc
            if _is_auth_failure(exc):
                # Not a race: a denied Automation grant will not become
                # allowed by trying again.
                logger.error("%s", TCC_HELP)
                sys.exit(1)
            if not isinstance(exc, (ConnectionRefusedError, OSError)):
                raise
            if attempt < attempts:
                logger.debug(
                    "iTerm2 API not ready (attempt %d/%d): %s", attempt, attempts, exc
                )
                time.sleep(CONNECT_BACKOFF_SECONDS)

    logger.error("%s", REFUSED_HELP)
    if not environment.is_api_listening():
        logger.error(
            "(iTerm2's API socket at %s does not exist, so the API server "
            "is not running.)",
            environment.API_SOCKET_PATH,
        )
    logger.debug("Last connection error: %r", last)
    sys.exit(1)
