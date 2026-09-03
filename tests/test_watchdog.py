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
    ever calls ``each_pane``, ``check_pane_stall`` and ``notify`` on it."""

    def __init__(self, *, stalled_sequence: list[bool]):
        self._stalled_sequence = list(stalled_sequence)
        self.notifications: list[tuple[str, str]] = []

    async def each_pane(self, *, marker, instance=None):
        yield "researcher", object()

    async def check_pane_stall(self, pane, *, role, previous, now, stall_after_seconds):
        is_stalled = self._stalled_sequence.pop(0)
        return {"seen_at": now}, is_stalled

    async def notify(self, *, title, message, marker=None, instance=None):
        self.notifications.append((title, message))


def _stall_marker(root: str, role: str, instance: str | None) -> str:
    return f"{session_marker_dir(root, instance)}/{role}.stalled"


async def _poll(backend, *, root, instance, last_seen):
    await watchdog._check_once(
        backend, root=root, instance=instance, last_seen=last_seen, stall_after_seconds=600
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
    assert len(backend.notifications) == 1
