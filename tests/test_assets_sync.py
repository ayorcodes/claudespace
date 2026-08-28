"""Global Stop-hook install/removal against ~/.claude/settings.json.

This module edits a file Claude Code owns and claudespace only borrows, so
the cases that matter are the destructive ones: malformed input, other
people's hooks, and leaving nothing behind on uninstall.
"""

from __future__ import annotations

import json

from pathlib import Path

import pytest

from claudespace import assets_sync


@pytest.fixture
def settings(tmp_path):
    return tmp_path / "settings.json"


def _stop_commands(path):
    hooks = json.loads(path.read_text()).get("hooks", {}).get("Stop", [])
    return [h["command"] for entry in hooks for h in entry.get("hooks", [])]


def test_installs_into_a_missing_file(settings):
    assert assets_sync._install_handoff_hook(settings) is True
    assert _stop_commands(settings) == [assets_sync.HANDOFF_HOOK_COMMAND]


def test_install_is_idempotent(settings):
    assets_sync._install_handoff_hook(settings)
    assert assets_sync._install_handoff_hook(settings) is False
    assert _stop_commands(settings) == [assets_sync.HANDOFF_HOOK_COMMAND]


def test_preserves_unrelated_settings(settings):
    settings.write_text(json.dumps({"model": "claude-opus-5", "theme": "dark"}))
    assets_sync._install_handoff_hook(settings)
    data = json.loads(settings.read_text())
    assert data["model"] == "claude-opus-5"
    assert data["theme"] == "dark"


def test_preserves_someone_elses_stop_hook(settings):
    settings.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}})
    )
    assets_sync._install_handoff_hook(settings)
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
    assets_sync._install_handoff_hook(settings)
    commands = _stop_commands(settings)
    assert commands == [assets_sync.HANDOFF_HOOK_COMMAND]


def test_malformed_settings_raises_an_actionable_error(settings):
    settings.write_text("{ not json,")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        assets_sync._install_handoff_hook(settings)


def test_backs_up_before_rewriting(settings):
    settings.write_text(json.dumps({"model": "claude-opus-5"}))
    assets_sync._install_handoff_hook(settings)
    backups = list(settings.parent.glob("settings.json.bak-*"))
    assert len(backups) == 1
    assert "claude-opus-5" in backups[0].read_text()


class TestRemoveHandoffHook:
    def test_removes_the_hook_and_its_empty_scaffolding(self, settings):
        assets_sync._install_handoff_hook(settings)
        assert assets_sync.remove_handoff_hook(settings) is True
        # Nothing claudespace added should survive, including the now-empty
        # "hooks"/"Stop" containers it created.
        assert json.loads(settings.read_text()) == {}

    def test_keeps_scaffolding_that_still_holds_other_hooks(self, settings):
        settings.write_text(
            json.dumps(
                {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}}
            )
        )
        assets_sync._install_handoff_hook(settings)
        assets_sync.remove_handoff_hook(settings)
        assert _stop_commands(settings) == ["other"]

    def test_is_a_no_op_when_not_installed(self, settings):
        settings.write_text(json.dumps({"model": "claude-opus-5"}))
        assert assets_sync.remove_handoff_hook(settings) is False

    def test_is_a_no_op_when_the_file_is_missing(self, settings):
        assert assets_sync.remove_handoff_hook(settings) is False


class TestClaudeConfigDirs:
    """Which Claude Code config homes the hook gets installed into.

    A user running several profiles via
    `alias claudemax='CLAUDE_CONFIG_DIR=$HOME/.claudeMax claude'` can point a
    claudespace template at a pane that runs under any of them. Installing
    only into ~/.claude leaves those panes with no handoff hook - or a stale
    one that fails on every turn.
    """

    def test_always_includes_the_default_config_dir(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("CLAUDESPACE_CONFIG_DIRS", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(assets_sync, "DEFAULT_CONFIG_DIR", tmp_path / ".claude")
        assert assets_sync.claude_config_dirs() == [tmp_path / ".claude"]

    def test_discovers_sibling_profiles_that_have_settings(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("CLAUDESPACE_CONFIG_DIRS", raising=False)
        for name in (".claude", ".claudeMax", ".claude2"):
            d = tmp_path / name
            d.mkdir()
            (d / "settings.json").write_text("{}")
        # A ~/.claude* directory with no settings.json is not a profile.
        (tmp_path / ".claudeJunk").mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(assets_sync, "DEFAULT_CONFIG_DIR", tmp_path / ".claude")

        found = {p.name for p in assets_sync.claude_config_dirs()}
        assert found == {".claude", ".claudeMax", ".claude2"}

    def test_honours_claude_config_dir(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLAUDESPACE_CONFIG_DIRS", raising=False)
        custom = tmp_path / "elsewhere"
        custom.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(assets_sync, "DEFAULT_CONFIG_DIR", tmp_path / ".claude")
        assert custom in assets_sync.claude_config_dirs()

    def test_override_replaces_discovery(self, monkeypatch, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        monkeypatch.setenv("CLAUDESPACE_CONFIG_DIRS", f"{a}:{b}")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "ignored"))
        assert assets_sync.claude_config_dirs() == [a, b]

    def test_no_duplicates(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLAUDESPACE_CONFIG_DIRS", raising=False)
        d = tmp_path / ".claude"
        d.mkdir()
        (d / "settings.json").write_text("{}")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(assets_sync, "DEFAULT_CONFIG_DIR", d)
        assert assets_sync.claude_config_dirs() == [d]


def test_stale_colon_hook_is_replaced(settings):
    # The exact failure reported: a profile still carrying the long-removed
    # `claudespace:handoff`, which errors on every turn.
    settings.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "claudespace:handoff"}]}
    ]}}))
    assets_sync._install_handoff_hook(settings)
    commands = _stop_commands(settings)
    assert commands == [assets_sync.HANDOFF_HOOK_COMMAND]
    assert "claudespace:handoff" not in commands
