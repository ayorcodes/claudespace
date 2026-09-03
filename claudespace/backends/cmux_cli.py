"""Thin async wrappers over the ``cmux`` CLI (D1, Components).

Every call here builds an argv list and runs it via
``asyncio.create_subprocess_exec`` - never a shell string - so prompt text,
paths, and role names reaching ``send``/``rename-tab`` can't be parsed as
flags or injected into a shell (Security Considerations). A ``--`` guard is
used wherever cmux would otherwise try to interpret leading ``-`` text as
its own option.

Reads that need structured data (``workspace_list``/``surface_list``) go
through ``cmux rpc <method> <json>`` - the raw JSON-RPC escape hatch - since
the friendly CLI verbs (``workspace list``, ``list-panels``) don't expose
every field the spike inventoried (A7). Everything else uses the friendly
verb cmux itself documents as canonical (``cmux workspace create`` over the
deprecated ``new-workspace`` alias, etc.) - never a raw ``rpc`` call where a
verb exists, so a ``cmux --help`` reader can follow what this module does.

This is the only module that spawns a ``cmux`` subprocess; ``cmux.py``
(``CmuxBackend``) is built entirely out of these primitives so it can be
tested against a fake runner, mirroring ``tmux_cli.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil

from claudespace.backends.base import BackendUnavailableError

CMUX_TIMEOUT_SECONDS = 5.0
WORKSPACE_CREATE_TIMEOUT_SECONDS = 15.0

_OK_REF_RE = re.compile(r"^OK\s+(\S+)")


class CmuxCommandError(RuntimeError):
    """A non-zero-exit ``cmux`` invocation, carrying its stderr (or stdout,
    where cmux prints its ``Error: ...`` line there instead)."""

    def __init__(self, message: str, *, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


def is_cmux_available() -> bool:
    """Whether a ``cmux`` binary is on ``PATH`` at all - the cheap first
    check before spawning anything, mirroring ``tmux_cli.is_tmux_available``.
    """
    return shutil.which("cmux") is not None


async def run(*args: str, timeout: float = CMUX_TIMEOUT_SECONDS) -> str:
    """Run ``cmux <args>``, returning stripped stdout.

    Raises ``CmuxCommandError`` on a non-zero exit (stderr, falling back to
    stdout, as the message - cmux prints its ``Error: ...`` line to whichever
    stream a given subcommand happens to use) and ``BackendUnavailableError``
    on a timeout, mirroring ``tmux_cli.run``'s kill-on-timeout handling (a
    cancelled ``wait_for`` does not kill the underlying process on its own).
    """
    proc = await asyncio.create_subprocess_exec(
        "cmux",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise BackendUnavailableError(
            f"cmux command timed out after {timeout}s: cmux {' '.join(args)}"
        ) from exc
    if proc.returncode != 0:
        message = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
        raise CmuxCommandError(message, returncode=proc.returncode)
    return stdout.decode(errors="replace").strip()


def _parse_ok_ref(output: str, *, index: int = 0) -> str:
    """Pull the ``index``-th whitespace-separated ref out of an ``OK <ref>
    [<ref> ...]`` response line (e.g. ``OK workspace:6`` or ``OK surface:12
    workspace:6``)."""
    parts = output.split()
    if not parts or parts[0] != "OK" or len(parts) <= index + 1:
        raise CmuxCommandError(f"Unexpected cmux response: {output!r}")
    return parts[index + 1]


async def ping() -> str:
    """``cmux ping`` -> ``"PONG"`` once reachable (D4's liveness probe)."""
    return await run("ping")


async def capabilities() -> dict:
    """``cmux capabilities`` JSON, notably ``access_mode`` (D4)."""
    return json.loads(await run("capabilities"))


async def workspace_create(cwd: str) -> str:
    """Create a workspace rooted at ``cwd``, returning its ``workspace:N`` ref.

    ``--focus false`` keeps a freshly built claudespace workspace from
    stealing focus from whatever the user is doing, the cmux equivalent of
    iTerm2/tmux never activating a window they didn't ask to see yet.
    """
    output = await run(
        "workspace", "create", "--cwd", cwd, "--focus", "false",
        timeout=WORKSPACE_CREATE_TIMEOUT_SECONDS,
    )
    return _parse_ok_ref(output)


async def workspace_close(workspace_ref: str) -> None:
    """Best-effort teardown (mirrors ``tmux_cli.kill_session`` - cosmetic,
    never worth failing a caller over)."""
    try:
        await run("workspace", "close", workspace_ref)
    except CmuxCommandError:
        pass


async def workspace_list() -> list[dict]:
    """Every workspace cmux currently knows about, full field set (A7).

    Degrades to ``[]`` on a command error (Error Handling) - same
    "not found" contract as ``tmux_cli.list_panes_all``, so a lookup built
    on top of this never has to special-case a socket hiccup separately
    from "genuinely nothing tagged yet."
    """
    try:
        data = json.loads(await run("rpc", "workspace.list"))
    except CmuxCommandError:
        return []
    return data.get("workspaces", [])


async def surface_list(workspace_id: str) -> list[dict]:
    """Every surface in the workspace identified by ``workspace_id`` (a
    UUID, not a ``workspace:N`` ref - the raw JSON-RPC layer keys on the
    stable id, unlike the friendly CLI verbs). Degrades to ``[]`` on error,
    same as ``workspace_list``."""
    try:
        data = json.loads(await run("rpc", "surface.list", json.dumps({"workspace_id": workspace_id})))
    except CmuxCommandError:
        return []
    return data.get("surfaces", [])


async def new_split(
    direction: str, *, workspace_ref: str, surface_ref: str
) -> str:
    """Split ``surface_ref`` (D-split's confirmed ``--surface`` targeting),
    returning the new pane's ``surface:N`` ref.

    ``direction`` is one of cmux's four cardinal directions - ``vertical``
    (left/right divider, new pane on the right) maps to ``"right"``,
    ``vertical=False`` (top/bottom) to ``"down"``, matching ``split_pane``'s
    contract in ``base.py``.
    """
    output = await run(
        "new-split", direction, "--workspace", workspace_ref, "--surface", surface_ref
    )
    return _parse_ok_ref(output)


async def rename_tab(*, workspace_ref: str, surface_ref: str, title: str) -> None:
    """Set ``surface_ref``'s tab title - the identity field D2 owns.

    Best-effort: a rename failing (e.g. the surface has just closed) is
    cosmetic, mirroring ``tmux_cli.pane_border_title``.
    """
    try:
        await run(
            "rename-tab", "--workspace", workspace_ref, "--surface", surface_ref, "--", title
        )
    except CmuxCommandError:
        pass


async def send_text(*, workspace_ref: str, surface_ref: str, text: str) -> None:
    """Type ``text`` into ``surface_ref`` verbatim - the spike's A10 found
    this is a single atomic write, no chunking needed."""
    await run("send", "--workspace", workspace_ref, "--surface", surface_ref, "--", text)


async def send_key(*, workspace_ref: str, surface_ref: str, key: str) -> None:
    await run("send-key", "--workspace", workspace_ref, "--surface", surface_ref, key)


async def capture_pane(
    *, workspace_ref: str, surface_ref: str, lines: int = 200
) -> str:
    """``surface_ref``'s visible screen text - the cmux equivalent of tmux's
    ``capture-pane -p`` / iTerm2's ``async_get_screen_contents`` (AD6)."""
    try:
        return await run(
            "capture-pane",
            "--workspace", workspace_ref,
            "--surface", surface_ref,
            "--lines", str(lines),
        )
    except CmuxCommandError:
        return ""


async def focus_pane(*, workspace_ref: str, pane_ref: str) -> None:
    """Best-effort pane focus (A9, WANT) - never fatal."""
    try:
        await run("focus-pane", "--workspace", workspace_ref, "--pane", pane_ref)
    except CmuxCommandError:
        pass


async def workspace_select(workspace_ref: str) -> None:
    """Best-effort: bring ``workspace_ref`` to the front - the cmux
    equivalent of iTerm2's ``Window.async_activate``. Never fatal (Edge
    Cases: cmux app quit mid-run - a stale/closed workspace ref shouldn't
    crash a caller that's merely trying to make it visible)."""
    try:
        await run("workspace", "select", workspace_ref)
    except CmuxCommandError:
        pass
