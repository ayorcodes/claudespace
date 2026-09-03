"""launch_command_text's env exports - the only place CLAUDESPACE_MARKER_DIR
is computed, so a regression here breaks every prompt's `mkdir -p
$CLAUDESPACE_MARKER_DIR` in the same way (see pipeline.py's session_marker_dir).
"""

from __future__ import annotations

from claudespace.backends.common import launch_command_text


def _launch(root: str, instance: str) -> str:
    return launch_command_text(
        root=root,
        role="researcher",
        instance=instance,
        think=False,
        max_items=5,
        command="claude",
        backend_name="tmux",
    )


def test_launch_command_exports_scoped_marker_dir():
    text = _launch("/repo", "abc-123")
    assert "export CLAUDESPACE_MARKER_DIR=/repo/.claudespace/s/abc-123 &&" in text


def test_launch_command_marker_dir_follows_a_worktree_pointer(tmp_path):
    root = tmp_path / "main"
    worktree = tmp_path / "worktrees" / "feature"
    (root / ".claudespace" / "s" / "abc-123").mkdir(parents=True)
    worktree.mkdir(parents=True)
    (root / ".claudespace" / "s" / "abc-123" / "worktree").write_text(str(worktree))

    text = _launch(str(root), "abc-123")
    assert f"export CLAUDESPACE_ROOT={worktree} &&" in text
    assert f"export CLAUDESPACE_MARKER_DIR={worktree}/.claudespace/s/abc-123 &&" in text


# --- idle_completion_decision: sustained idle-at-prompt detection -----------

from claudespace.backends.common import idle_completion_decision  # noqa: E402


def test_idle_first_poll_is_never_flagged():
    state, idle = idle_completion_decision(
        None, text="a", ready=True, now=100.0, idle_after_seconds=600
    )
    assert idle is False
    assert state == {"text": "a", "ready": True, "since": 100.0}


def test_idle_carries_first_seen_forward_across_polls():
    # The clock measures from first sighting, not the last poll gap - so a
    # short poll interval still accumulates toward the idle window.
    s1, idle1 = idle_completion_decision(
        None, text="a", ready=True, now=100.0, idle_after_seconds=600
    )
    assert idle1 is False
    s2, idle2 = idle_completion_decision(
        s1, text="a", ready=True, now=400.0, idle_after_seconds=600
    )
    assert idle2 is False and s2["since"] == 100.0
    _s3, idle3 = idle_completion_decision(
        s2, text="a", ready=True, now=701.0, idle_after_seconds=600
    )
    assert idle3 is True


def test_idle_clock_resets_when_screen_changes():
    s1, _ = idle_completion_decision(
        None, text="a", ready=True, now=100.0, idle_after_seconds=600
    )
    s2, idle = idle_completion_decision(
        s1, text="b", ready=True, now=800.0, idle_after_seconds=600
    )
    assert idle is False  # changed screen restarts the window
    assert s2["since"] == 800.0


def test_a_non_ready_pane_is_never_idle_flagged():
    _state, idle = idle_completion_decision(
        {"text": "a", "ready": True, "since": 1.0},
        text="a", ready=False, now=10_000.0, idle_after_seconds=600,
    )
    assert idle is False
