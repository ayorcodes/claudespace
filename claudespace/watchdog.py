"""Stall/crash detection for unattended (``--think``/conductor-driven) runs.

``handoff.py``'s nag mechanism (see its module docstring) only fires from a
Stop hook - i.e. only when a pane actually finishes a turn without writing a
completion marker. A pane that never reaches Stop at all - stuck behind a
permission or trust-folder dialog, wedged in a runaway tool loop, or whose
``claude`` process crashed outright - produces no Stop event, so nothing
today detects it. For a supervised run that's fine, a human notices; for an
unattended ``--think``/conductor run left going overnight, a genuinely stuck
pane just silently burns wall-clock until someone happens to look.

This module polls each role pane on an interval and asks the backend
whether it looks stalled (``TerminalBackend.check_pane_stall`` - see AD6 in
the design doc). Both backends use the same full-fidelity screen-content
diff: a pane whose screen is byte-for-byte identical across a full
interval, and whose last non-blank line is *not* claude's ready prompt, is
flagged - iTerm2 reads the screen via its Python API, tmux via
``capture-pane`` (``backends/common.py``'s shared ``screen_signature``/
``stall_decision``).

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
import time
from typing import Any

from claudespace.backends.base import TerminalBackend
from claudespace.pipeline import resolve_root, session_marker_dir

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_STALL_AFTER_SECONDS = 600


def _stall_marker_path(root: str, role: str, instance: str | None) -> str:
    return f"{session_marker_dir(resolve_root(root, instance), instance)}/{role}.stalled"


def _write_stall_marker(root: str, role: str, instance: str | None) -> None:
    marker_dir = session_marker_dir(resolve_root(root, instance), instance)
    os.makedirs(marker_dir, exist_ok=True)
    with open(_stall_marker_path(root, role, instance), "w", encoding="utf-8") as handle:
        handle.write(f"{time.time()}\n")


def _clear_stall_marker(root: str, role: str, instance: str | None) -> None:
    try:
        os.remove(_stall_marker_path(root, role, instance))
    except FileNotFoundError:
        return


async def _check_once(
    backend: TerminalBackend,
    *,
    root: str,
    instance: str | None,
    last_seen: dict[str, Any],
    stall_after_seconds: float,
) -> None:
    """One poll pass: sample every workspace pane via the backend, and
    flag/notify any the backend reports as newly stalled.
    """
    now = time.monotonic()
    async for role, pane in backend.each_pane(marker=root, instance=instance):
        new_state, is_stalled = await backend.check_pane_stall(
            pane,
            role=role,
            previous=last_seen.get(role),
            now=now,
            stall_after_seconds=stall_after_seconds,
        )
        last_seen[role] = new_state

        if not is_stalled:
            _clear_stall_marker(root, role, instance)
            continue

        # D6: notify only on the poll that transitions a pane into stalled,
        # not on every poll it remains stalled - the "notifications persist"
        # symptom the design fixes. The marker's own presence *before* this
        # write is the onset check; still re-written every poll so its mtime
        # reflects the most recent detection.
        already_stalled = os.path.isfile(_stall_marker_path(root, role, instance))

        logger.warning(
            "Pane '%s' in workspace '%s' looks stalled or crashed - see "
            "%s for details",
            role,
            root,
            _stall_marker_path(root, role, instance),
        )
        _write_stall_marker(root, role, instance)

        if already_stalled:
            continue
        await backend.notify(
            title="claudespace: possible stall",
            message=f"'{role}' pane in {root} looks stalled or has crashed.",
            marker=root,
            instance=instance,
        )


async def run_watchdog(
    backend: TerminalBackend,
    *,
    root: str,
    instance: str | None,
    interval_seconds: float,
    stall_after_seconds: float,
) -> None:
    """Poll ``root``'s workspace panes forever, at ``interval_seconds``,
    flagging any pane the backend reports as stalled or crashed.

    Runs until interrupted (Ctrl-C) or the process is killed - there is no
    other exit condition, since a watchdog for an unattended run is meant
    to keep watching for as long as that run might still be going.
    """
    last_seen: dict[str, Any] = {}
    logger.info(
        "Watching '%s' every %ds (flagging %ds of silence)",
        root,
        interval_seconds,
        stall_after_seconds,
    )
    while True:
        try:
            await _check_once(
                backend,
                root=root,
                instance=instance,
                last_seen=last_seen,
                stall_after_seconds=stall_after_seconds,
            )
        except Exception:
            logger.exception("Watchdog poll failed - continuing")
        await asyncio.sleep(interval_seconds)
