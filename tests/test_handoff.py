"""``.nagged`` mtime scoping (see handoff.py's ``_maybe_nag_missing_marker``
docstring): a leftover ``.nagged`` from an earlier conductor backlog item
must not silence the nag for the item now in flight - the motivating bug
per-session marker scoping ships alongside.
"""

from __future__ import annotations

import asyncio
import os

from claudespace import pipeline
from claudespace.handoff import (
    HANDOFF_STATE_SUFFIX,
    NAG_STATE_SUFFIX,
    _handle_new_topic,
    _maybe_nag_missing_marker,
)


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


def _write_stale_and_handed(path: str, *, marker_mtime: float, sentinel_mtime: float) -> None:
    with open(path, "w") as f:
        f.write("docs/x.md")
    os.utime(path, (marker_mtime, marker_mtime))
    sentinel_path = path + HANDOFF_STATE_SUFFIX
    open(sentinel_path, "w").close()
    os.utime(sentinel_path, (sentinel_mtime, sentinel_mtime))


def test_stale_but_already_handed_done_marker_is_not_nagged(tmp_path):
    root, done_path = _prep(tmp_path)
    _write_stale_and_handed(done_path, marker_mtime=100.0, sentinel_mtime=200.0)
    backend = _FakeBackend(run_started=None)

    fired = asyncio.run(
        _maybe_nag_missing_marker(backend, root=root, instance="i1", role="implementer")
    )

    assert fired is False
    assert not os.path.isfile(done_path + NAG_STATE_SUFFIX)


def test_stale_but_already_handed_blocked_marker_is_not_nagged(tmp_path):
    root, done_path = _prep(tmp_path, role="implementer")
    blocked_path = pipeline.blocked_marker_path(root, "implementer", "i1")
    _write_stale_and_handed(blocked_path, marker_mtime=100.0, sentinel_mtime=200.0)
    backend = _FakeBackend(run_started=None)

    fired = asyncio.run(
        _maybe_nag_missing_marker(backend, root=root, instance="i1", role="implementer")
    )

    assert fired is False
    assert not os.path.isfile(done_path + NAG_STATE_SUFFIX)


def test_marker_present_without_handed_off_sentinel_is_not_touched_by_suppression(tmp_path):
    # Regression guard for the "not suppressed" edge case (design doc edge
    # case 2): the role wrote the marker, but the hook hasn't processed it
    # (or the send failed) - no sentinel means no proof it landed, so this is
    # a *fresh* marker (caught by the pre-existing _read_fresh_marker check,
    # not the new _marker_present_and_handed suppression) and must not nag.
    root, done_path = _prep(tmp_path)
    with open(done_path, "w") as f:
        f.write("docs/x.md")
    os.utime(done_path, (1.0, 1.0))
    backend = _FakeBackend(run_started=None)

    fired = asyncio.run(
        _maybe_nag_missing_marker(backend, root=root, instance="i1", role="implementer")
    )

    assert fired is False
    assert not os.path.isfile(done_path + NAG_STATE_SUFFIX)


def test_genuinely_missing_marker_still_nags(tmp_path):
    root, done_path = _prep(tmp_path)
    backend = _FakeBackend(run_started=None)

    fired = asyncio.run(
        _maybe_nag_missing_marker(backend, root=root, instance="i1", role="implementer")
    )

    assert fired is True
    assert os.path.isfile(done_path + NAG_STATE_SUFFIX)


def test_nags_without_crashing_when_the_scoped_session_dir_does_not_exist_yet(tmp_path):
    # The role never wrote its .done/.blocked at all - the exact case this
    # nag exists to catch - so nothing may have created its scoped
    # s/<instance>/ subdirectory yet (unlike the flat legacy layout, where
    # the base .claudespace/ dir was always pre-created at workspace build
    # time). Deliberately skip _prep's mkdir to reproduce that.
    root = str(tmp_path)
    done_path = pipeline.done_marker_path(root, "implementer", "i1")
    assert not os.path.isdir(os.path.dirname(done_path))
    backend = _FakeBackend(run_started=100.0)

    fired = asyncio.run(
        _maybe_nag_missing_marker(backend, root=root, instance="i1", role="implementer")
    )

    assert fired is True
    assert os.path.isfile(done_path + NAG_STATE_SUFFIX)


class _RunDocBackend:
    """Duck-typed backend for ``_handle_new_topic``: tracks a mutable
    ``run_doc`` and records set_run_doc calls. find_role_pane/send_new are
    no-ops (only the clear path touches them, which these tests don't hit)."""

    def __init__(self, *, doc: str | None, run_started: float | None):
        self._doc = doc
        self._run_started = run_started
        self.sets: list[str] = []

    async def get_run_doc(self, *, marker, instance=None):
        return self._doc, self._run_started

    async def set_run_doc(self, *, marker, instance=None, doc, started_at):
        self._doc = doc
        self._run_started = started_at
        self.sets.append(doc)

    async def find_role_pane(self, *, marker, role, instance=None):
        return None

    async def send_new(self, pane):
        pass


def test_new_topic_warning_records_doc_so_a_retrigger_resumes(tmp_path):
    # A genuinely new topic landing on a workspace whose prior run is still
    # in flight (different doc, started, no reviewer PASS) warns once and
    # suppresses auto-submit - but records the new doc, so retriggering the
    # SAME doc resumes silently instead of re-warning (which had forced the
    # user to press Enter on every retry).
    root = str(tmp_path)
    backend = _RunDocBackend(doc="docs/old-feature.md", run_started=1000.0)

    async def _run():
        warning = await _handle_new_topic(
            backend, root=root, instance="i1", doc_artifact="docs/new-topic.md"
        )
        assert warning is not None and "old-feature.md" in warning
        assert backend._doc == "docs/new-topic.md"  # recorded despite only warning

        again = await _handle_new_topic(
            backend, root=root, instance="i1", doc_artifact="docs/new-topic.md"
        )
        assert again is None  # resume fast-path -> auto-submit, no second warning

    asyncio.run(_run())


def test_same_doc_never_warns_in_the_first_place(tmp_path):
    # The pre-existing fast-path: an incoming doc equal to the current
    # run_doc always resumes, never warns.
    root = str(tmp_path)
    backend = _RunDocBackend(doc="docs/topic.md", run_started=1000.0)

    async def _run():
        assert await _handle_new_topic(
            backend, root=root, instance="i1", doc_artifact="docs/topic.md"
        ) is None

    asyncio.run(_run())
