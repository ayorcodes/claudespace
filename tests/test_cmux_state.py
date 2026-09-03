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
from claudespace.backends.cmux import CmuxBackend, _SEED_DIMS, _apply_split, _choose_split

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


def _simulate_reveals(n_extra: int):
    """Grow a lazy layout from one seeded pane by ``n_extra`` reveals, exactly
    as reveal_role does, returning (final dims model, list of split directions).
    """
    dims: dict[str, list[int]] = {"surface:0": list(_SEED_DIMS)}
    directions: list[bool] = []
    for i in range(n_extra):
        target, vertical = _choose_split(dims)
        _apply_split(dims, target, f"surface:{i + 1}", vertical=vertical)
        directions.append(vertical)
    return dims, directions


class TestLazyLayoutModel:
    """The layout fix: cmux has no real geometry, so reveal balances against a
    virtual model. Guards against the regression where every reveal split
    vertically and panes degenerated into a row of ever-narrower columns.
    """

    def test_first_split_of_a_wide_seed_is_vertical(self):
        # A main-left column, matching the eager layout's first divider.
        _target, vertical = _choose_split({"surface:0": list(_SEED_DIMS)})
        assert vertical is True

    def test_apply_split_halves_the_right_dimension(self):
        dims = {"a": [160, 48]}
        _apply_split(dims, "a", "b", vertical=True)
        assert dims == {"a": [80, 48], "b": [80, 48]}
        dims = {"a": [80, 48]}
        _apply_split(dims, "a", "b", vertical=False)
        assert dims == {"a": [80, 24], "b": [80, 24]}

    def test_choice_is_deterministic_largest_area_then_ref(self):
        dims = {"a": [80, 48], "b": [160, 48], "c": [80, 48]}
        assert _choose_split(dims)[0] == "b"  # largest area
        assert _choose_split({"a": [80, 48], "b": [80, 48]})[0] == "a"  # tie -> ref order

    def test_five_panes_form_a_grid_not_a_single_row(self):
        dims, directions = _simulate_reveals(4)
        assert len(dims) == 5
        # The bug was 4 vertical splits in a row. A grid must mix directions.
        assert False in directions, f"expected some horizontal splits, got {directions}"
        assert True in directions
        # No pane ends up a degenerate sliver: widths and heights stay within a
        # 2x spread of each other (a balanced grid, not a fan of thin columns).
        widths = [w for w, _h in dims.values()]
        heights = [h for _w, h in dims.values()]
        assert max(widths) / min(widths) <= 2
        assert max(heights) / min(heights) <= 2
