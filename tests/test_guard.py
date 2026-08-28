"""The PreToolUse guard that stops a read-only role modifying code.

A researcher pane was observed rewriting a component mid-investigation. Its
prompt forbade it; nothing enforced that. Tool denial can't express the rule
either - denying Edit just routes the model to Write, and denying Write stops
these roles persisting the artifact they exist to produce. So the guard
filters by path.
"""

from __future__ import annotations

import pytest

from claudespace.config import READ_ONLY_ROLES
from claudespace.guard import decide, is_allowed_path


def _write(path, tool="Edit"):
    return {"tool_name": tool, "tool_input": {"file_path": path}}


class TestIsAllowedPath:
    @pytest.mark.parametrize(
        "path",
        [
            "/repo/docs/research/2026-08-28-x.md",
            "/repo/README.markdown",
            "/repo/.claudespace/researcher.done",
            "/repo/.claudespace/reports/x-implementer-report.md",
            "/repo/docs/backlog-thing.MD",
        ],
    )
    def test_artifacts_and_markers_are_allowed(self, path):
        assert is_allowed_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "/repo/src/app.tsx",
            "/repo/apps/web/components/catalog-manager.tsx",
            "/repo/pyproject.toml",
            "/repo/Makefile",
            "/repo/.claudespacex/notes.txt",
        ],
    )
    def test_code_is_blocked(self, path):
        assert not is_allowed_path(path)

    def test_a_missing_path_is_not_second_guessed(self):
        # Nothing to judge - defer to the normal permission flow.
        assert is_allowed_path("")


class TestDecide:
    @pytest.mark.parametrize("role", sorted(READ_ONLY_ROLES))
    def test_every_read_only_role_is_blocked_from_code(self, role):
        assert decide(_write("/repo/src/app.ts"), role) is not None

    @pytest.mark.parametrize("role", sorted(READ_ONLY_ROLES))
    def test_every_read_only_role_can_still_write_its_artifact(self, role):
        assert decide(_write("/repo/docs/brief.md"), role) is None
        assert decide(_write("/repo/.claudespace/x.done"), role) is None

    @pytest.mark.parametrize("role", ["implementer", "conductor"])
    def test_roles_that_may_edit_are_untouched(self, role):
        assert decide(_write("/repo/src/app.ts"), role) is None

    def test_no_role_means_no_opinion(self):
        # The hook is installed globally; outside a claudespace pane it must
        # never interfere with an unrelated session.
        assert decide(_write("/repo/src/app.ts"), None) is None
        assert decide(_write("/repo/src/app.ts"), "") is None

    @pytest.mark.parametrize("tool", ["Write", "Edit", "NotebookEdit", "MultiEdit"])
    def test_covers_every_write_tool(self, tool):
        # Denying only Edit is what failed - the model reaches for Write.
        assert decide(_write("/repo/src/app.ts", tool), "researcher") is not None

    @pytest.mark.parametrize("tool", ["Read", "Grep", "Bash", "Glob"])
    def test_reading_and_investigating_are_never_blocked(self, tool):
        assert decide(_write("/repo/src/app.ts", tool), "researcher") is None

    def test_denial_names_the_role_and_the_path(self):
        reason = decide(_write("/repo/src/app.ts"), "researcher")
        assert "researcher" in reason
        assert "/repo/src/app.ts" in reason
