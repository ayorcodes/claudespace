"""``watchdog._check_once``'s stall notification: D6 routes delivery through
``backend.notify`` (never a raw ``osascript`` call) and dedups on the
*onset* of a stall, not every poll - the "notifications persist" symptom
the design fixes (Tests Required)."""

from __future__ import annotations

import asyncio
import os

from claudespace import watchdog
from claudespace.pipeline import session_marker_dir


class _FakeBackend:
    """Duck-typed stand-in for ``TerminalBackend``: ``_check_once`` only
    ever calls ``each_pane``, ``check_pane_stall`` and ``notify`` on it.

    ``check_pane_stall`` returns the same ``{text, ready, seen_at}`` state
    the real backends' ``stall_decision`` does, so ``_check_once``'s
    idle-completion branch (which reads ``text``/``ready`` back out of that
    state) sees realistic values. Panes here report a blank, non-ready
    screen by default, so the idle branch stays dormant unless a test opts
    into it via ``ready_text``.
    """

    def __init__(
        self,
        *,
        stalled_sequence: list[bool],
        ready_text: str | None = None,
        role: str = "researcher",
    ):
        self._stalled_sequence = list(stalled_sequence)
        self._ready_text = ready_text
        self._role = role
        self.notifications: list[tuple[str, str]] = []

    async def each_pane(self, *, marker, instance=None):
        yield self._role, object()

    async def check_pane_stall(self, pane, *, role, previous, now, stall_after_seconds):
        is_stalled = self._stalled_sequence.pop(0)
        if self._ready_text is not None:
            return {"text": self._ready_text, "ready": True, "seen_at": now}, is_stalled
        return {"text": "", "ready": False, "seen_at": now}, is_stalled

    async def notify(self, *, title, message, marker=None, instance=None):
        self.notifications.append((title, message))


def _stall_marker(root: str, role: str, instance: str | None) -> str:
    return f"{session_marker_dir(root, instance)}/{role}.stalled"


def _idle_marker(root: str, role: str, instance: str | None) -> str:
    return f"{session_marker_dir(root, instance)}/{role}.silent"


async def _poll(
    backend, *, root, instance, last_seen, last_idle=None, idle_after_seconds=600
):
    await watchdog._check_once(
        backend,
        root=root,
        instance=instance,
        last_seen=last_seen,
        last_idle=last_idle if last_idle is not None else {},
        stall_after_seconds=600,
        idle_after_seconds=idle_after_seconds,
    )


def test_stall_onset_notifies_exactly_once(tmp_path):
    root = str(tmp_path)
    backend = _FakeBackend(stalled_sequence=[True, True, True])
    last_seen: dict = {}

    asyncio.run(_poll(backend, root=root, instance="i1", last_seen=last_seen))
    assert len(backend.notifications) == 1
    assert os.path.isfile(_stall_marker(root, "researcher", "i1"))

    # Still stalled on the next two polls: no re-notify, but the marker
    # keeps getting re-written (its mtime tracks the latest detection).
    asyncio.run(_poll(backend, root=root, instance="i1", last_seen=last_seen))
    asyncio.run(_poll(backend, root=root, instance="i1", last_seen=last_seen))
    assert len(backend.notifications) == 1


def test_clearing_then_re_stalling_notifies_again(tmp_path):
    root = str(tmp_path)
    backend = _FakeBackend(stalled_sequence=[True, False, True])
    last_seen: dict = {}

    asyncio.run(_poll(backend, root=root, instance="i1", last_seen=last_seen))
    assert len(backend.notifications) == 1

    asyncio.run(_poll(backend, root=root, instance="i1", last_seen=last_seen))
    assert not os.path.isfile(_stall_marker(root, "researcher", "i1"))

    asyncio.run(_poll(backend, root=root, instance="i1", last_seen=last_seen))
    assert len(backend.notifications) == 2


def test_backend_notify_is_called_not_a_raw_osascript(tmp_path, monkeypatch):
    import subprocess

    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))

    root = str(tmp_path)
    backend = _FakeBackend(stalled_sequence=[True])
    last_seen: dict = {}

    asyncio.run(_poll(backend, root=root, instance="i1", last_seen=last_seen))

    assert not called  # nothing shelled out to osascript directly


# --- Fix 1: silent-completion (idle-at-prompt, no handoff) backstop ---------


def test_idle_completion_flags_and_notifies_once(tmp_path):
    # A pane that finished, is idle at the ready prompt, and has a forward
    # handoff owed with no marker written is flagged once on onset - the
    # failure the stall check (idle == healthy) structurally can't see.
    root = str(tmp_path)
    # idle_after_seconds=0: any elapsed since first-seen crosses the window,
    # so the very first poll where the pane is idle flags it.
    backend = _FakeBackend(
        stalled_sequence=[False, False, False], ready_text="idle at prompt"
    )
    last_seen: dict = {}
    last_idle: dict = {}

    asyncio.run(
        _poll(
            backend, root=root, instance="i1", last_seen=last_seen,
            last_idle=last_idle, idle_after_seconds=0,
        )
    )
    assert len(backend.notifications) == 1
    assert backend.notifications[0][0] == "claudespace: no handoff"
    assert os.path.isfile(_idle_marker(root, "researcher", "i1"))

    # Still idle on the next two polls: marker keeps being re-written, but no
    # re-notify (onset dedup, same idiom as the stall marker).
    for _ in range(2):
        asyncio.run(
            _poll(
                backend, root=root, instance="i1", last_seen=last_seen,
                last_idle=last_idle, idle_after_seconds=0,
            )
        )
    assert len(backend.notifications) == 1


def test_idle_under_the_window_is_not_yet_flagged(tmp_path, monkeypatch):
    # Idle, but not for long enough: no flag until the idle window elapses.
    clock = {"t": 1000.0}
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: clock["t"])

    root = str(tmp_path)
    backend = _FakeBackend(
        stalled_sequence=[False, False], ready_text="idle at prompt"
    )
    last_seen: dict = {}
    last_idle: dict = {}

    asyncio.run(
        _poll(
            backend, root=root, instance="i1", last_seen=last_seen,
            last_idle=last_idle, idle_after_seconds=600,
        )
    )
    assert not os.path.isfile(_idle_marker(root, "researcher", "i1"))
    assert backend.notifications == []

    # Advance past the window with the screen unchanged: now it flags.
    clock["t"] += 601
    asyncio.run(
        _poll(
            backend, root=root, instance="i1", last_seen=last_seen,
            last_idle=last_idle, idle_after_seconds=600,
        )
    )
    assert os.path.isfile(_idle_marker(root, "researcher", "i1"))
    assert len(backend.notifications) == 1


def test_idle_completion_ignored_when_marker_already_handed_off(tmp_path):
    # A role that finished AND handed off is idle at the prompt for a
    # legitimate reason - proven by the .handed-off sentinel. Not a silent
    # completion, so never flagged.
    from claudespace import pipeline
    from claudespace.handoff import HANDOFF_STATE_SUFFIX

    root = str(tmp_path)
    done_path = pipeline.done_marker_path(root, "researcher", "i1")
    os.makedirs(os.path.dirname(done_path), exist_ok=True)
    with open(done_path, "w") as f:
        f.write("docs/x.md")
    open(done_path + HANDOFF_STATE_SUFFIX, "w").close()

    backend = _FakeBackend(stalled_sequence=[False], ready_text="idle at prompt")
    asyncio.run(
        _poll(
            backend, root=root, instance="i1", last_seen={},
            last_idle={}, idle_after_seconds=0,
        )
    )
    assert not os.path.isfile(_idle_marker(root, "researcher", "i1"))
    assert backend.notifications == []


def test_idle_completion_ignored_for_terminal_reviewer(tmp_path):
    # reviewer's PASS is terminal outside a conductor run: idle at the prompt
    # with no marker is *done*, not a silent completion.
    root = str(tmp_path)
    backend = _FakeBackend(
        stalled_sequence=[False], ready_text="idle at prompt", role="reviewer"
    )
    asyncio.run(
        _poll(
            backend, root=root, instance="i1", last_seen={},
            last_idle={}, idle_after_seconds=0,
        )
    )
    assert not os.path.isfile(_idle_marker(root, "reviewer", "i1"))
    assert backend.notifications == []


def test_a_stalled_pane_is_not_also_flagged_idle(tmp_path):
    # The two checks are mutually exclusive: a stall short-circuits before the
    # idle branch, so a pane reported stalled never also writes an idle marker
    # (and the idle clock isn't advanced behind its back).
    root = str(tmp_path)
    backend = _FakeBackend(
        stalled_sequence=[True], ready_text="idle at prompt"
    )
    asyncio.run(
        _poll(
            backend, root=root, instance="i1", last_seen={},
            last_idle={}, idle_after_seconds=0,
        )
    )
    assert os.path.isfile(_stall_marker(root, "researcher", "i1"))
    assert not os.path.isfile(_idle_marker(root, "researcher", "i1"))


def test_idle_marker_cleared_when_pane_becomes_active(tmp_path):
    # Flagged idle, then the screen changes (role resumed): the idle marker is
    # cleared so a later genuine silent completion notifies afresh.
    root = str(tmp_path)
    idle_backend = _FakeBackend(stalled_sequence=[False], ready_text="idle at prompt")
    last_seen: dict = {}
    last_idle: dict = {}
    asyncio.run(
        _poll(
            idle_backend, root=root, instance="i1", last_seen=last_seen,
            last_idle=last_idle, idle_after_seconds=0,
        )
    )
    assert os.path.isfile(_idle_marker(root, "researcher", "i1"))

    # A non-ready (active) screen on the next poll clears the idle marker.
    active_backend = _FakeBackend(stalled_sequence=[False])
    asyncio.run(
        _poll(
            active_backend, root=root, instance="i1", last_seen=last_seen,
            last_idle=last_idle, idle_after_seconds=0,
        )
    )
    assert not os.path.isfile(_idle_marker(root, "researcher", "i1"))
