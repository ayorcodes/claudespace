"""``.nagged`` mtime scoping (see handoff.py's ``_maybe_nag_missing_marker``
docstring): a leftover ``.nagged`` from an earlier conductor backlog item
must not silence the nag for the item now in flight - the motivating bug
per-session marker scoping ships alongside.
"""

from __future__ import annotations

import asyncio
import os

from claudespace import pipeline
from claudespace.handoff import NAG_STATE_SUFFIX, _maybe_nag_missing_marker


class _FakeBackend:
    """Duck-typed stand-in for ``TerminalBackend``: ``_maybe_nag_missing_marker``
    only ever calls ``get_run_doc`` and ``get_auto_handoff`` on it."""

    def __init__(self, *, run_started: float | None, auto_handoff: bool = True):
        self._run_started = run_started
        self._auto_handoff = auto_handoff

    async def get_run_doc(self, *, marker, instance=None):
        return None, self._run_started

    async def get_auto_handoff(self, *, marker, instance=None):
        return self._auto_handoff


def _prep(tmp_path, role="implementer", instance="i1"):
    root = str(tmp_path)
    done_path = pipeline.done_marker_path(root, role, instance)
    os.makedirs(os.path.dirname(done_path), exist_ok=True)
    return root, done_path


def _touch_nagged(done_path: str, *, mtime: float) -> str:
    nag_path = done_path + NAG_STATE_SUFFIX
    open(nag_path, "w").close()
    os.utime(nag_path, (mtime, mtime))
    return nag_path


def test_nagged_newer_than_run_started_is_not_renagged(tmp_path):
    root, done_path = _prep(tmp_path)
    _touch_nagged(done_path, mtime=200.0)
    backend = _FakeBackend(run_started=100.0)

    fired = asyncio.run(
        _maybe_nag_missing_marker(backend, root=root, instance="i1", role="implementer")
    )

    assert fired is False
    assert os.path.isfile(done_path + NAG_STATE_SUFFIX)


def test_nagged_older_than_run_started_is_cleared_and_renagged(tmp_path):
    root, done_path = _prep(tmp_path)
    nag_path = _touch_nagged(done_path, mtime=50.0)
    backend = _FakeBackend(run_started=100.0)

    fired = asyncio.run(
        _maybe_nag_missing_marker(backend, root=root, instance="i1", role="implementer")
    )

    assert fired is True
    # Re-nagged: the sentinel is back, with a fresh mtime rather than the
    # stale one from the earlier item.
    assert os.path.isfile(nag_path)
    assert os.path.getmtime(nag_path) >= 100.0


def test_run_started_none_treats_existing_nagged_as_valid(tmp_path):
    root, done_path = _prep(tmp_path)
    _touch_nagged(done_path, mtime=1.0)
    backend = _FakeBackend(run_started=None)

    fired = asyncio.run(
        _maybe_nag_missing_marker(backend, root=root, instance="i1", role="implementer")
    )

    assert fired is False


def test_no_existing_nagged_still_nags_without_checking_run_doc(tmp_path):
    root, done_path = _prep(tmp_path)
    backend = _FakeBackend(run_started=100.0)

    fired = asyncio.run(
        _maybe_nag_missing_marker(backend, root=root, instance="i1", role="implementer")
    )

    assert fired is True
    assert os.path.isfile(done_path + NAG_STATE_SUFFIX)
