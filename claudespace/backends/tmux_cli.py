"""Thin async wrappers over the ``tmux`` CLI (AD3, Components).

Every call here builds an argv list and runs it via
``asyncio.create_subprocess_exec`` - never a shell string - so prompt text,
paths, and role names reaching ``send-keys``/``set-option`` can't be parsed
as flags or injected into a shell (Security Considerations). ``-l --`` is
used wherever tmux would otherwise try to interpret leading ``-`` text as
its own options.

This is the only module that spawns a ``tmux`` subprocess; ``tmux.py``
(``TmuxBackend``) is built entirely out of these primitives so it can be
tested against a fake runner the same way the primitives are tested against
a real ``tmux -f /dev/null`` server (see ``Tests Required``: headless tmux
integration).
"""

from __future__ import annotations

import asyncio
import shutil

from claudespace.backends.base import BackendUnavailableError

TMUX_TIMEOUT_SECONDS = 5.0
NEW_SESSION_TIMEOUT_SECONDS = 15.0
MIN_TMUX_VERSION = (3, 0)

# AD8: every claudespace tmux command runs on this dedicated named socket,
# never the user's default one - the linchpin that lets Increment 2 load
# tmux-resurrect/tmux-continuum without ever touching the user's own
# ~/.tmux.conf or interfering with their everyday tmux usage, and
# retroactively closes Increment 1's "user's tmux config/plugins
# interfering" edge case (there is nothing of the user's to interfere,
# since this server never loads it).
SOCKET_NAME = "claudespace"


class TmuxCommandError(RuntimeError):
    """A non-zero-exit ``tmux`` invocation, carrying its stderr."""

    def __init__(self, message: str, *, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


def is_tmux_available() -> bool:
    """Whether a ``tmux`` binary is on ``PATH`` at all - the cheap first
    check before spawning anything."""
    return shutil.which("tmux") is not None


def _socket_args() -> list[str]:
    """``-L claudespace``, plus ``-f <private conf>`` iff persistence wrote
    one (AD8/AD12) - omitted, not just empty, when persistence is off, so
    an unadorned dedicated-socket server with zero plugins is what a
    ``persist=false`` user actually gets.

    Importing ``tmux_persist`` here (not at module scope) avoids a circular
    import - it imports nothing from this module, but keeping the
    dependency direction one-way (``tmux_persist`` is config/pure-Python,
    this module is the subprocess boundary) is deliberate.
    """
    from claudespace.backends import tmux_persist

    args = ["-L", SOCKET_NAME]
    if tmux_persist.CONF_PATH.is_file():
        args += ["-f", str(tmux_persist.CONF_PATH)]
    return args


async def run(*args: str, timeout: float = TMUX_TIMEOUT_SECONDS) -> str:
    """Run ``tmux <socket args> <args>``, returning stripped stdout.

    Raises ``TmuxCommandError`` on a non-zero exit (with stderr as the
    message) and ``BackendUnavailableError`` on a timeout - the latter
    because a hung tmux invocation is exactly the "hangs with no output and
    no timeout" failure mode ``connect.py`` exists to prevent for iTerm2,
    reproduced here for tmux.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            *_socket_args(),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        raise BackendUnavailableError(
            f"tmux command timed out after {timeout}s: tmux {' '.join(args)}"
        ) from exc
    if proc.returncode != 0:
        raise TmuxCommandError(
            stderr.decode(errors="replace").strip(), returncode=proc.returncode
        )
    return stdout.decode(errors="replace").strip()


async def version() -> str:
    """``tmux -V`` output, e.g. ``"tmux 3.7c"``."""
    return await run("-V")


def parse_version(raw: str) -> tuple[int, ...]:
    """Extract a comparable ``(major, minor)`` tuple from ``tmux -V``
    output. tmux's minor version can carry a trailing letter (``3.7c``) -
    stripped, since it doesn't affect the feature floor this backend needs.
    """
    digits = raw.replace("tmux ", "").split(".")
    parsed: list[int] = []
    for part in digits[:2]:
        num = "".join(ch for ch in part if ch.isdigit())
        parsed.append(int(num) if num else 0)
    return tuple(parsed)


async def has_session(session: str) -> bool:
    try:
        await run("has-session", "-t", session)
        return True
    except TmuxCommandError:
        return False


async def server_running() -> bool:
    """Whether claudespace's dedicated-socket server is already up.

    tmux's default ``exit-empty`` behavior means a server with zero
    sessions has already shut itself down - so "the server is running" and
    "at least one session exists" are the same fact for claudespace's usage
    pattern, and any failure from ``list-sessions`` (including "no server
    running") means "not running." Used to detect whether the *next*
    command is the one that would start a fresh server (and therefore
    trigger continuum's autorestore-on-start check) - see
    ``TmuxBackend._await_autorestore_if_needed``.
    """
    try:
        await run("list-sessions")
        return True
    except TmuxCommandError:
        return False


async def new_session(session: str, *, cwd: str | None = None) -> str:
    """Create a detached session, returning its starting pane's ``#{pane_id}``.

    Gets a longer timeout than other commands: this is the one most likely
    to be the client that starts a fresh server from scratch, which (with
    the private config, Increment 2) also means sourcing resurrect and
    continuum - verified in practice to occasionally take noticeably
    longer than the general ``TMUX_TIMEOUT_SECONDS`` bound on a cold
    start. A later command against the same (by-then-already-running)
    server doesn't pay this cost.
    """
    args = [
        "new-session",
        "-d",
        "-s",
        session,
        "-x",
        "220",
        "-y",
        "50",
        "-P",
        "-F",
        "#{pane_id}",
    ]
    if cwd:
        args += ["-c", cwd]
    return await run(*args, timeout=NEW_SESSION_TIMEOUT_SECONDS)


async def kill_session(session: str) -> None:
    try:
        await run("kill-session", "-t", session)
    except TmuxCommandError:
        pass


async def split_window(
    target: str, *, vertical: bool, session: str
) -> str:
    """Split ``target`` pane, returning the new pane's ``#{pane_id}``.

    ``vertical=True`` (left/right divider, matching ``SplitNode``'s
    convention) is tmux's ``-h`` (horizontal *arrangement* of two panes
    side by side); ``vertical=False`` (top/bottom) is ``-v``.
    """
    flag = "-h" if vertical else "-v"
    return await run(
        "split-window",
        flag,
        "-t",
        target,
        "-P",
        "-F",
        "#{pane_id}",
    )


async def send_keys_literal(target: str, text: str) -> None:
    """Type ``text`` into ``target`` verbatim (no key-table interpretation).

    ``-l`` treats the argument as literal text rather than key names; the
    ``--`` guard stops tmux from parsing a prompt that happens to start with
    ``-`` as an option of its own (Security Considerations).
    """
    await run("send-keys", "-t", target, "-l", "--", text)


async def send_enter(target: str) -> None:
    await run("send-keys", "-t", target, "Enter")


async def capture_pane(target: str) -> str:
    """The pane's visible screen text - the tmux equivalent of iTerm2's
    ``async_get_screen_contents`` (AD6). ``-J`` joins soft-wrapped lines
    back into one logical line, matching iTerm2's own ``_screen_contains``
    join-on-non-hard-eol behavior, so a long prompt that wraps in a narrow
    pane is still found as one contiguous match.
    """
    try:
        return await run("capture-pane", "-p", "-J", "-t", target)
    except TmuxCommandError:
        return ""


async def set_pane_option(target: str, key: str, value: str) -> None:
    await run("set-option", "-p", "-t", target, key, value)


async def set_session_option(session: str, key: str, value: str) -> None:
    try:
        await run("set-option", "-t", session, key, value)
    except TmuxCommandError:
        pass


async def show_pane_option(target: str, key: str) -> str | None:
    try:
        result = await run("show-options", "-p", "-v", "-t", target, key)
    except TmuxCommandError:
        return None
    return result or None


async def pane_dims(target: str) -> tuple[int, int]:
    """``(width, height)`` in character cells - tmux reports real
    dimensions, unlike the raw Ghostty AppleScript surface, so
    largest-sibling selection has full fidelity (Edge Cases)."""
    result = await run(
        "display-message", "-p", "-t", target, "#{pane_width}x#{pane_height}"
    )
    width, _, height = result.partition("x")
    return int(width), int(height)


async def select_pane(target: str) -> None:
    await run("select-pane", "-t", target)


async def select_window(target: str) -> None:
    await run("select-window", "-t", target)


async def list_clients(session: str) -> list[str]:
    try:
        result = await run("list-clients", "-t", session, "-F", "#{client_name}")
    except TmuxCommandError:
        return []
    return [line for line in result.split("\n") if line]


async def list_panes_all(fields: tuple[str, ...]) -> list[dict[str, str]]:
    """Every pane across every tmux session, with the requested ``#{...}``
    fields, as one bulk call - the tmux equivalent of iTerm2's per-session
    variable walk, but O(1) round trips instead of O(panes) (Performance).
    """
    fmt = "\x1f".join(f"#{{{field}}}" for field in fields)
    try:
        result = await run("list-panes", "-a", "-F", fmt)
    except TmuxCommandError:
        return []
    rows = []
    for line in result.split("\n"):
        if not line:
            continue
        values = line.split("\x1f")
        rows.append(dict(zip(fields, values, strict=False)))
    return rows


async def list_panes(session: str, fields: tuple[str, ...]) -> list[dict[str, str]]:
    """Every pane in ``session`` (not the whole server) - used for
    largest-sibling selection on reveal (Edge Cases: Lazy reveal split
    sizing)."""
    fmt = "\x1f".join(f"#{{{field}}}" for field in fields)
    try:
        result = await run("list-panes", "-t", session, "-F", fmt)
    except TmuxCommandError:
        return []
    rows = []
    for line in result.split("\n"):
        if not line:
            continue
        values = line.split("\x1f")
        rows.append(dict(zip(fields, values, strict=False)))
    return rows


async def pane_border_title(target: str, title: str) -> None:
    """Best-effort role-identity label (Edge Cases: Theming) - a pane-local
    title shown in its border when ``pane-border-status``/``-format`` are
    on. Failure is cosmetic only, never worth failing a build over.
    """
    try:
        await run("select-pane", "-t", target, "-T", title)
    except TmuxCommandError:
        pass
