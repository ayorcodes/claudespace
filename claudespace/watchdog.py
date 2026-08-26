"""Stall/crash detection for unattended (``--think``/conductor-driven) runs.

``handoff.py``'s nag mechanism (see its module docstring) only fires from a
Stop hook - i.e. only when a pane actually finishes a turn without writing a
completion marker. A pane that never reaches Stop at all - stuck behind a
permission or trust-folder dialog, wedged in a runaway tool loop, or whose
``claude`` process crashed outright - produces no Stop event, so nothing
today detects it. For a supervised run that's fine, a human notices; for an
unattended ``--think``/conductor run left going overnight, a genuinely stuck
pane just silently burns wall-clock until someone happens to look.

This module polls each role pane's screen contents on an interval. A pane
whose screen is byte-for-byte identical across a full interval, and whose
last non-blank line is *not* claude's ready prompt, is flagged: unchanged
output while claude isn't sitting idle at a prompt means nothing is
happening - not "thinking" (which streams tokens/animates a spinner, so the
screen keeps changing) and not "waiting for the next handoff" (which shows
the ready prompt). A pane whose screen is unchanged *and* shows the ready
prompt is ordinary idle, never flagged.

Run via ``claudespace watchdog --root <dir>`` in its own terminal, or
backgrounded (``nohup ... &``), or driven by an external scheduler - it is
deliberately a separate long-lived process rather than something bolted
onto the Stop-hook path, since a hook only runs in reaction to pane events
and can't notice a pane that stopped producing events at all.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time

import iterm2

from claudespace import iterm as iterm_ops
from claudespace.pipeline import MARKER_DIR

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_STALL_AFTER_SECONDS = 600


def _screen_signature(session_contents: "iterm2.ScreenContents") -> tuple[str, bool]:
    """Return ``(full-screen text, ends-at-ready-prompt)`` for one poll.

    The full text (not a hash) is kept only for the unchanged-comparison
    below; nothing persists it past that. ``ends-at-ready-prompt`` mirrors
    ``_wait_for_claude_prompt``'s own detection of claude's ``❯`` marker, so
    "idle at prompt" is recognized the same way everywhere in the codebase.
    """
    lines = [session_contents.line(i).string for i in range(session_contents.number_of_lines)]
    text = "\n".join(lines)
    ready = any(
        line.strip().startswith(iterm_ops.CLAUDE_PROMPT_MARKER)
        for line in lines
        if line.strip()
    )
    return text, ready


def _notify(title: str, message: str) -> None:
    """Pop a macOS notification via ``osascript`` - no extra dependency
    beyond what ``environment.require_macos`` already guarantees is present.
    Best-effort: a notification failure (e.g. notifications disabled for
    the terminal app) is logged, not fatal to the watchdog loop.
    """
    script = f'display notification {message!r} with title {title!r}'
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
    except Exception:
        logger.warning("Failed to send stall notification (non-fatal)", exc_info=True)


def _stall_marker_path(root: str, role: str) -> str:
    return f"{root.rstrip('/')}/{MARKER_DIR}/{role}.stalled"


async def _check_once(
    app: iterm2.App,
    *,
    root: str,
    instance: str | None,
    last_seen: dict[str, tuple[str, bool, float]],
    stall_after_seconds: float,
) -> None:
    """One poll pass: snapshot every workspace pane, compare against the
    previous snapshot in ``last_seen`` (mutated in place), and flag/notify
    any pane whose screen has been unchanged and non-idle for at least
    ``stall_after_seconds``.
    """
    now = time.monotonic()
    async for session in iterm_ops.each_workspace_session(app, marker=root, instance=instance):
        role = await session.async_get_variable(iterm_ops.ROLE_VAR)
        if not role:
            continue
        contents = await session.async_get_screen_contents()
        text, ready = _screen_signature(contents)

        previous = last_seen.get(role)
        last_seen[role] = (text, ready, now)

        if previous is None:
            continue
        prev_text, _prev_ready, prev_seen_at = previous
        if text != prev_text:
            # Screen moved - progress happened, or it just changed state
            # (e.g. went idle). Either way, not stalled; the marker (if any
            # was left standing from an earlier stall) is stale now.
            _clear_stall_marker(root, role)
            continue
        if ready:
            # Unchanged but idle at the prompt - ordinary idle pane, not a
            # stall.
            continue
        if now - prev_seen_at < stall_after_seconds:
            continue

        logger.warning(
            "Pane '%s' in workspace '%s' has shown no output for over %ds "
            "and isn't idle at the prompt - possible stall (stuck dialog, "
            "runaway tool loop, or crashed process)",
            role,
            root,
            int(now - prev_seen_at),
        )
        _write_stall_marker(root, role)
        _notify(
            "claudespace: possible stall",
            f"'{role}' pane in {root} has produced no output for "
            f"{int(now - prev_seen_at)}s.",
        )
        # Reset the clock so an already-flagged, still-stuck pane doesn't
        # re-notify every single poll - only once per stall_after_seconds
        # of continued silence.
        last_seen[role] = (text, ready, now)


def _write_stall_marker(root: str, role: str) -> None:
    os.makedirs(f"{root.rstrip('/')}/{MARKER_DIR}", exist_ok=True)
    with open(_stall_marker_path(root, role), "w", encoding="utf-8") as handle:
        handle.write(f"{time.time()}\n")


def _clear_stall_marker(root: str, role: str) -> None:
    try:
        os.remove(_stall_marker_path(root, role))
    except FileNotFoundError:
        return


async def run_watchdog(
    connection: iterm2.Connection,
    *,
    root: str,
    instance: str | None,
    interval_seconds: float,
    stall_after_seconds: float,
) -> None:
    """Poll ``root``'s workspace panes forever, at ``interval_seconds``,
    flagging any pane silent and non-idle for ``stall_after_seconds``.

    Runs until interrupted (Ctrl-C) or the process is killed - there is no
    other exit condition, since a watchdog for an unattended run is meant
    to keep watching for as long as that run might still be going.
    """
    app = await iterm2.async_get_app(connection)
    last_seen: dict[str, tuple[str, bool, float]] = {}
    logger.info(
        "Watching '%s' every %ds (flagging %ds of silence)",
        root,
        interval_seconds,
        stall_after_seconds,
    )
    while True:
        try:
            await _check_once(
                app,
                root=root,
                instance=instance,
                last_seen=last_seen,
                stall_after_seconds=stall_after_seconds,
            )
        except Exception:
            logger.exception("Watchdog poll failed - continuing")
        await asyncio.sleep(interval_seconds)
