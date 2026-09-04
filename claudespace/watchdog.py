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

That stall check deliberately treats a pane sitting idle at claude's ready
prompt as healthy - which leaves a second, symmetric gap it can't see: a
role that *finished* a turn but wrote no handoff marker (easy to lose track
of at the end of a long implementation turn) is now parked at the prompt
with the pipeline silently stalled behind it, and neither the stall check
(idle-at-prompt is "healthy") nor the Stop-hook nag (its one-shot reminder
was already spent earlier in the turn) catches it. So each poll also runs a
complementary idle-completion check (``common.idle_completion_decision`` +
``handoff.has_unhanded_forward_work``): a pane idle at the ready prompt, its
screen unchanged for the idle window, whose role still owes a forward
handoff and has no marker to trigger it, is flagged as a silent completion.
Because this reads the live screen, it's a true backstop - independent of
whatever the Stop hook's sentinel already did.

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
from claudespace.backends.common import idle_completion_decision
from claudespace.handoff import has_unhanded_forward_work
from claudespace.pipeline import session_marker_dir

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_STALL_AFTER_SECONDS = 600
# How long a pane must sit idle at the ready prompt with no handoff marker
# before the watchdog flags it as a silent completion. Defaults to the stall
# window when not given its own value (see ``run_watchdog``).
DEFAULT_IDLE_AFTER_SECONDS = DEFAULT_STALL_AFTER_SECONDS


def _marker_path(root: str, role: str, instance: str | None, suffix: str) -> str:
    return (
        f"{session_marker_dir(root, instance)}/{role}.{suffix}"
    )


def _write_marker(root: str, role: str, instance: str | None, suffix: str) -> None:
    marker_dir = session_marker_dir(root, instance)
    os.makedirs(marker_dir, exist_ok=True)
    with open(_marker_path(root, role, instance, suffix), "w", encoding="utf-8") as handle:
        handle.write(f"{time.time()}\n")


def _clear_marker(root: str, role: str, instance: str | None, suffix: str) -> None:
    try:
        os.remove(_marker_path(root, role, instance, suffix))
    except FileNotFoundError:
        return


def _stall_marker_path(root: str, role: str, instance: str | None) -> str:
    return _marker_path(root, role, instance, "stalled")


def _write_stall_marker(root: str, role: str, instance: str | None) -> None:
    _write_marker(root, role, instance, "stalled")


def _clear_stall_marker(root: str, role: str, instance: str | None) -> None:
    _clear_marker(root, role, instance, "stalled")


def _idle_marker_path(root: str, role: str, instance: str | None) -> str:
    return _marker_path(root, role, instance, "silent")


def _write_idle_marker(root: str, role: str, instance: str | None) -> None:
    _write_marker(root, role, instance, "silent")


def _clear_idle_marker(root: str, role: str, instance: str | None) -> None:
    _clear_marker(root, role, instance, "silent")


async def _handle_stall(
    backend: TerminalBackend, *, root: str, instance: str | None, role: str
) -> None:
    """Flag ``role`` as stalled/crashed and notify on the stall's onset."""
    # D6: notify only on the poll that transitions a pane into stalled, not
    # on every poll it remains stalled - the "notifications persist" symptom
    # the design fixes. The marker's own presence *before* this write is the
    # onset check; still re-written every poll so its mtime reflects the most
    # recent detection.
    already_stalled = os.path.isfile(_stall_marker_path(root, role, instance))
    logger.warning(
        "Pane '%s' in workspace '%s' looks stalled or crashed - see %s for details",
        role,
        root,
        _stall_marker_path(root, role, instance),
    )
    _write_stall_marker(root, role, instance)
    if already_stalled:
        return
    await backend.notify(
        title="claudespace: possible stall",
        message=f"'{role}' pane in {root} looks stalled or has crashed.",
        marker=root,
        instance=instance,
    )


async def _handle_idle_completion(
    backend: TerminalBackend, *, root: str, instance: str | None, role: str
) -> None:
    """Flag ``role`` as a silent completion and notify on its onset.

    Same onset-dedup idiom as ``_handle_stall``: the marker's presence before
    this write is the onset check, so the notification fires once when the
    pane first crosses into "finished, idle, nothing handed off" rather than
    on every poll it stays there.
    """
    already_flagged = os.path.isfile(_idle_marker_path(root, role, instance))
    logger.warning(
        "Pane '%s' in workspace '%s' finished and is idle at the prompt with "
        "no handoff marker - the pipeline is stalled behind it. See %s",
        role,
        root,
        _idle_marker_path(root, role, instance),
    )
    _write_idle_marker(root, role, instance)
    if already_flagged:
        return
    await backend.notify(
        title="claudespace: no handoff",
        message=(
            f"'{role}' pane in {root} finished but handed nothing off - "
            "it may have skipped its completion marker."
        ),
        marker=root,
        instance=instance,
    )


async def _check_once(
    backend: TerminalBackend,
    *,
    root: str,
    instance: str | None,
    last_seen: dict[str, Any],
    last_idle: dict[str, Any],
    stall_after_seconds: float,
    idle_after_seconds: float,
) -> None:
    """One poll pass: sample every workspace pane via the backend, and
    flag/notify any pane that either looks stalled/crashed (unchanged,
    non-idle output) or has silently completed (idle at the ready prompt with
    a forward handoff still owed and no marker written). The two are mutually
    exclusive - a stall requires the pane *not* be at the ready prompt, a
    silent completion requires it *is* - so at most one fires per pane.
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

        if is_stalled:
            # A wedged pane can't also be a silent completion; leave the idle
            # clock untouched and skip the idle check this poll.
            await _handle_stall(backend, root=root, instance=instance, role=role)
            continue
        _clear_stall_marker(root, role, instance)

        # Fix 1: the stall check above deliberately treats an idle-at-prompt
        # pane as healthy, so it can never catch a role that finished a turn,
        # wrote no handoff marker, and is now parked at the prompt with the
        # pipeline silently stalled behind it. This second check does - the
        # content-aware backstop that doesn't depend on the Stop hook's
        # (already-consumed) nag sentinel. ``check_pane_stall`` already
        # carries the pane's text/ready in its returned state, so the idle
        # decision reuses that rather than re-reading the screen.
        idle_state, is_idle = idle_completion_decision(
            last_idle.get(role),
            text=new_state.get("text", ""),
            ready=bool(new_state.get("ready")),
            now=now,
            idle_after_seconds=idle_after_seconds,
        )
        last_idle[role] = idle_state

        if is_idle and has_unhanded_forward_work(root, role, instance):
            await _handle_idle_completion(
                backend, root=root, instance=instance, role=role
            )
        else:
            _clear_idle_marker(root, role, instance)


async def run_watchdog(
    backend: TerminalBackend,
    *,
    root: str,
    instance: str | None,
    interval_seconds: float,
    stall_after_seconds: float,
    idle_after_seconds: float | None = None,
) -> None:
    """Poll ``root``'s workspace panes forever, at ``interval_seconds``,
    flagging any pane that looks stalled/crashed or that has silently
    completed (finished, idle at the prompt, nothing handed off).

    ``idle_after_seconds`` defaults to ``stall_after_seconds`` when not given,
    so the single ``--stall-after`` knob governs both windows unless a caller
    wants them to differ.

    Runs until interrupted (Ctrl-C) or the process is killed - there is no
    other exit condition, since a watchdog for an unattended run is meant
    to keep watching for as long as that run might still be going.
    """
    if idle_after_seconds is None:
        idle_after_seconds = stall_after_seconds
    last_seen: dict[str, Any] = {}
    last_idle: dict[str, Any] = {}
    logger.info(
        "Watching '%s' every %ds (stall after %ds, silent completion after %ds)",
        root,
        interval_seconds,
        stall_after_seconds,
        idle_after_seconds,
    )
    while True:
        try:
            await _check_once(
                backend,
                root=root,
                instance=instance,
                last_seen=last_seen,
                last_idle=last_idle,
                stall_after_seconds=stall_after_seconds,
                idle_after_seconds=idle_after_seconds,
            )
        except Exception:
            logger.exception("Watchdog poll failed - continuing")
        await asyncio.sleep(interval_seconds)
