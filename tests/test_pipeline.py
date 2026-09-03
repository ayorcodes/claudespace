"""Marker parsing and pipeline shape.

These are the rules handoff.py walks on every Stop hook, so a regression
here misroutes work silently rather than failing loudly.
"""

from __future__ import annotations

import pytest

from claudespace.pipeline import (
    DOWNSTREAM_ROLES,
    PIPELINE,
    blocked_marker_path,
    conductor_run_marker_path,
    done_marker_path,
    parse_blocked_marker,
    parse_done_marker,
    resolve_root,
    session_marker_dir,
    think_marker_path,
    worktree_marker_path,
)


def test_every_route_target_is_a_real_stage():
    for role, stage in PIPELINE.items():
        targets = (
            ((stage.next_role,) if stage.next_role else ())
            + stage.bounce_to
            + stage.alt_next_roles
        )
        for target in targets:
            assert target in PIPELINE, f"{role} routes to unknown role {target!r}"


def test_reviewer_is_terminal_but_can_reach_conductor():
    stage = PIPELINE["reviewer"]
    assert stage.next_role is None
    assert "conductor" in stage.alt_next_roles


def test_researcher_has_no_bounce_path():
    assert PIPELINE["researcher"].bounce_to == ()


def test_downstream_roles_excludes_researcher_and_conductor():
    assert "researcher" not in DOWNSTREAM_ROLES
    assert "conductor" not in DOWNSTREAM_ROLES


class TestParseDoneMarker:
    def test_bare_path_uses_next_role(self):
        stage = PIPELINE["researcher"]
        assert parse_done_marker("docs/a.md", stage=stage) == ("planner", "docs/a.md")

    def test_route_directive_selects_alt_role(self):
        stage = PIPELINE["researcher"]
        assert parse_done_marker("route: principal\ndocs/a.md", stage=stage) == (
            "principal",
            "docs/a.md",
        )

    def test_disallowed_route_falls_back_rather_than_stalling(self):
        # researcher's alt_next_roles now covers every other role (see
        # pipeline.py's "alt_next_roles does double duty"), so the only way
        # to exercise the fallback is a route naming something that isn't a
        # role at all.
        stage = PIPELINE["researcher"]
        assert parse_done_marker("route: bogus\ndocs/a.md", stage=stage) == (
            "planner",
            "docs/a.md",
        )

    def test_route_reaches_any_other_role(self):
        # Any role can hand off to any other role's specialized operation,
        # not just the roles adjacent to it in the default pipeline order.
        stage = PIPELINE["researcher"]
        assert parse_done_marker("route: reviewer\ndocs/a.md", stage=stage) == (
            "reviewer",
            "docs/a.md",
        )

    @pytest.mark.parametrize(
        "content",
        ["docs/a.md\n\ntrailing note", "  docs/a.md  \n", "docs/a.md\nnote"],
    )
    def test_artifact_is_always_one_line(self, content):
        # A literal newline here becomes a paste chip in the destination
        # pane, which the submit-retry logic can't recognise.
        _, artifact = parse_done_marker(content, stage=PIPELINE["researcher"])
        assert "\n" not in artifact


class TestParseBlockedMarker:
    def test_single_bounce_target_needs_no_directive(self):
        assert parse_blocked_marker("n.md", stage=PIPELINE["reviewer"]) == (
            "implementer",
            "n.md",
        )

    def test_multiple_targets_without_directive_is_undecidable(self):
        # None means "skip the handoff" - better than guessing a target.
        assert parse_blocked_marker("n.md", stage=PIPELINE["implementer"]) == (
            None,
            "n.md",
        )

    def test_directive_picks_among_multiple_targets(self):
        assert parse_blocked_marker(
            "route: planner\nn.md", stage=PIPELINE["implementer"]
        ) == ("planner", "n.md")


def test_marker_paths_tolerate_a_trailing_slash_on_root():
    assert done_marker_path("/x/", "researcher") == "/x/.claudespace/researcher.done"
    assert blocked_marker_path("/x", "planner") == "/x/.claudespace/planner.blocked"
    assert think_marker_path("/x/") == "/x/.claudespace/think"


def test_resolve_root_without_a_worktree_marker_is_a_no_op(tmp_path):
    root = str(tmp_path)
    assert resolve_root(root) == root


def test_resolve_root_follows_a_worktree_marker_to_a_real_directory(tmp_path):
    root = tmp_path / "main"
    worktree = tmp_path / "worktrees" / "vat-exclusion"
    (root / ".claudespace").mkdir(parents=True)
    worktree.mkdir(parents=True)
    (root / ".claudespace" / "worktree").write_text(str(worktree) + "\n")

    assert resolve_root(str(root)) == str(worktree)


def test_resolve_root_ignores_a_worktree_marker_pointing_nowhere(tmp_path):
    root = tmp_path / "main"
    (root / ".claudespace").mkdir(parents=True)
    (root / ".claudespace" / "worktree").write_text("/does/not/exist")

    assert resolve_root(str(root)) == str(root)


def test_marker_path_builders_honor_a_worktree_marker(tmp_path):
    root = tmp_path / "main"
    worktree = tmp_path / "worktrees" / "vat-exclusion"
    (root / ".claudespace").mkdir(parents=True)
    worktree.mkdir(parents=True)
    (root / ".claudespace" / "worktree").write_text(str(worktree))

    assert done_marker_path(str(root), "researcher") == str(
        worktree / ".claudespace" / "researcher.done"
    )
    assert blocked_marker_path(str(root), "planner") == str(
        worktree / ".claudespace" / "planner.blocked"
    )
    assert worktree_marker_path(str(root)) == str(root / ".claudespace" / "worktree")


def test_session_marker_dir_without_instance_is_byte_identical_to_flat_path():
    assert session_marker_dir("/x", None) == "/x/.claudespace"
    assert session_marker_dir("/x/", None) == "/x/.claudespace"
    assert session_marker_dir("/x", "") == "/x/.claudespace"


def test_session_marker_dir_with_instance_nests_under_s():
    assert session_marker_dir("/x", "abc-123") == "/x/.claudespace/s/abc-123"


def test_marker_path_builders_scope_under_instance():
    assert done_marker_path("/x", "researcher", "id") == "/x/.claudespace/s/id/researcher.done"
    assert blocked_marker_path("/x", "planner", "id") == "/x/.claudespace/s/id/planner.blocked"
    assert think_marker_path("/x", "id") == "/x/.claudespace/s/id/think"
    assert conductor_run_marker_path("/x", "id") == "/x/.claudespace/s/id/conductor-run"
    assert worktree_marker_path("/x", "id") == "/x/.claudespace/s/id/worktree"


def test_resolve_root_with_instance_reads_the_scoped_worktree_pointer(tmp_path):
    root = tmp_path / "main"
    worktree = tmp_path / "worktrees" / "vat-exclusion"
    (root / ".claudespace" / "s" / "id").mkdir(parents=True)
    worktree.mkdir(parents=True)
    (root / ".claudespace" / "s" / "id" / "worktree").write_text(str(worktree))

    assert resolve_root(str(root), "id") == str(worktree)
    # the unscoped, 1-arg form still reads only the flat pointer
    assert resolve_root(str(root)) == str(root)
