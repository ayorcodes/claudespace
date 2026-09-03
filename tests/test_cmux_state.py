"""``backends/cmux.py`` runtime-state storage: pure filesystem, no cmux
needed - so these run everywhere (unlike the cmux-gated backend suite).

The regression this file guards: cmux keyed its per-session state
(``lazy``/``template``/``run_doc``) on the repo root, which a mid-run git
worktree moves out from under a pane. Build wrote the state under the original
checkout; the handoff hook, running with ``CLAUDESPACE_ROOT`` re-exported into
the worktree, then read a different path, found nothing, and silently skipped
the lazy reveal - so ``--lazy`` + worktree meant the next role's pane never
appeared. State is now keyed on the (worktree-invariant) instance instead.
"""

from __future__ import annotations

import asyncio

import pytest

from claudespace.backends import cmux
from claudespace.backends.cmux import CmuxBackend

INSTANCE = "abcd1234-5678-90ab-cdef-1234567890ab"


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    # Honour XDG_STATE_HOME so writes land in a throwaway dir, never the real
    # ~/.local/state. Confirms the backend honours it, too.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


def test_state_round_trips_by_instance():
    cmux._write_state(INSTANCE, {"template": "native", "lazy": True})
    assert cmux._read_state(INSTANCE) == {"template": "native", "lazy": True}


def test_missing_state_reads_as_empty():
    assert cmux._read_state("no-such-instance") == {}


def test_state_path_is_not_under_any_repo_root():
    # Keyed on the instance at a user-level dir, with no repo/marker path
    # anywhere in it - that root-independence is the whole fix.
    path = cmux._state_path(INSTANCE)
    assert INSTANCE in path
    assert ".claudespace" not in path
    assert "/xdg-state/" in path


def test_lazy_reveal_survives_a_worktree_root_change():
    # The exact failure mode. Build writes state while CLAUDESPACE_ROOT is the
    # original checkout; the planner->principal handoff reads it with
    # CLAUDESPACE_ROOT re-exported into a *different* worktree path. Same
    # instance -> the read must still find the state, so _reveal_destination
    # gets a real template/lazy and reveals principal. get_* with an explicit
    # instance never touches cmux, so this needs no running cmux.
    backend = CmuxBackend()
    build_root = "/Users/dev/Work/vendoorly"
    worktree_root = "/Users/dev/Work/vendoorly-worktrees/waiter-table-visibility"

    cmux._write_state(
        INSTANCE,
        {"auto_handoff": True, "lazy": True, "template": "native",
         "run_doc": None, "run_started": None},
    )

    async def _scenario():
        # Sanity: reads under the *build* root work (the case that never broke).
        assert await backend.get_template_name(marker=build_root, instance=INSTANCE) == "native"
        assert await backend.get_lazy(marker=build_root, instance=INSTANCE) is True

        # The regression: reads under the *worktree* root must be identical.
        # Before the fix these returned None/False -> reveal silently skipped.
        assert await backend.get_template_name(marker=worktree_root, instance=INSTANCE) == "native"
        assert await backend.get_lazy(marker=worktree_root, instance=INSTANCE) is True
        assert await backend.get_auto_handoff(marker=worktree_root, instance=INSTANCE) is True

    asyncio.run(_scenario())


def test_run_doc_round_trips_across_a_worktree_root_change():
    backend = CmuxBackend()
    build_root = "/Users/dev/Work/vendoorly"
    worktree_root = "/Users/dev/Work/vendoorly-worktrees/feature"

    cmux._write_state(INSTANCE, {"template": "native", "lazy": True})

    async def _scenario():
        await backend.set_run_doc(
            marker=build_root, instance=INSTANCE, doc="docs/x.md", started_at=42.0
        )
        # Read back from the worktree root - same instance, same state.
        doc, started = await backend.get_run_doc(marker=worktree_root, instance=INSTANCE)
        assert doc == "docs/x.md"
        assert started == 42.0

    asyncio.run(_scenario())
