"""Global Stop-hook install/removal against ~/.claude/settings.json.

This module edits a file Claude Code owns and claudespace only borrows, so
the cases that matter are the destructive ones: malformed input, other
people's hooks, and leaving nothing behind on uninstall.
"""

from __future__ import annotations

import json

import pytest

from claudespace import assets_sync


@pytest.fixture
def settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(assets_sync, "SETTINGS_DEST", path)
    return path


def _stop_commands(path):
    hooks = json.loads(path.read_text()).get("hooks", {}).get("Stop", [])
    return [h["command"] for entry in hooks for h in entry.get("hooks", [])]


def test_installs_into_a_missing_file(settings):
    assert assets_sync._install_handoff_hook() is True
    assert _stop_commands(settings) == [assets_sync.HANDOFF_HOOK_COMMAND]


def test_install_is_idempotent(settings):
    assets_sync._install_handoff_hook()
    assert assets_sync._install_handoff_hook() is False
    assert _stop_commands(settings) == [assets_sync.HANDOFF_HOOK_COMMAND]


def test_preserves_unrelated_settings(settings):
    settings.write_text(json.dumps({"model": "claude-opus-5", "theme": "dark"}))
    assets_sync._install_handoff_hook()
    data = json.loads(settings.read_text())
    assert data["model"] == "claude-opus-5"
    assert data["theme"] == "dark"


def test_preserves_someone_elses_stop_hook(settings):
    settings.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}})
    )
    assets_sync._install_handoff_hook()
    assert "other" in _stop_commands(settings)
    assert assets_sync.HANDOFF_HOOK_COMMAND in _stop_commands(settings)


def test_replaces_the_legacy_colon_named_hook(settings):
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": assets_sync.LEGACY_HANDOFF_HOOK_COMMAND,
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )
    assets_sync._install_handoff_hook()
    commands = _stop_commands(settings)
    assert commands == [assets_sync.HANDOFF_HOOK_COMMAND]


def test_malformed_settings_raises_an_actionable_error(settings):
    settings.write_text("{ not json,")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        assets_sync._install_handoff_hook()


def test_backs_up_before_rewriting(settings):
    settings.write_text(json.dumps({"model": "claude-opus-5"}))
    assets_sync._install_handoff_hook()
    backups = list(settings.parent.glob("settings.json.bak-*"))
    assert len(backups) == 1
    assert "claude-opus-5" in backups[0].read_text()


class TestRemoveHandoffHook:
    def test_removes_the_hook_and_its_empty_scaffolding(self, settings):
        assets_sync._install_handoff_hook()
        assert assets_sync.remove_handoff_hook() is True
        # Nothing claudespace added should survive, including the now-empty
        # "hooks"/"Stop" containers it created.
        assert json.loads(settings.read_text()) == {}

    def test_keeps_scaffolding_that_still_holds_other_hooks(self, settings):
        settings.write_text(
            json.dumps(
                {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}}
            )
        )
        assets_sync._install_handoff_hook()
        assets_sync.remove_handoff_hook()
        assert _stop_commands(settings) == ["other"]

    def test_is_a_no_op_when_not_installed(self, settings):
        settings.write_text(json.dumps({"model": "claude-opus-5"}))
        assert assets_sync.remove_handoff_hook() is False

    def test_is_a_no_op_when_the_file_is_missing(self, settings):
        assert assets_sync.remove_handoff_hook() is False
