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
