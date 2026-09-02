"""Template seeding, loading, and the retired-role-command migration.

The migration rewrites a file the user hand-edits, so its blast radius
matters more than most of this package.
"""

from __future__ import annotations

import pytest

from claudespace.config import (
    NATIVE_TEMPLATE_TOML,
    ROLE_COMMANDS,
    Template,
    ensure_agentic_template_seeded,
    ensure_native_template_seeded,
    load_terminal_backend,
    load_user_templates,
    migrate_role_commands,
)


@pytest.fixture
def toml_path(tmp_path):
    return tmp_path / "templates.toml"


def test_seeded_native_is_loadable_and_uses_plain_claude(toml_path):
    assert ensure_native_template_seeded(toml_path) is True
    templates = load_user_templates(toml_path)
    assert set(templates) == {"native"}
    for pane in templates["native"].panes:
        assert pane.command.startswith("claude --model ")


def test_seeding_is_idempotent(toml_path):
    ensure_native_template_seeded(toml_path)
    assert ensure_native_template_seeded(toml_path) is False


def test_seeding_native_does_not_clobber_a_user_template(toml_path):
    toml_path.write_text(
        '[templates.mine]\nlayout = "main_left_grid_right"\n\n'
        '[[templates.mine.panes]]\nrole = "researcher"\ncommand = "my-wrapper"\n'
    )
    ensure_native_template_seeded(toml_path)
    templates = load_user_templates(toml_path)
    assert set(templates) == {"mine", "native"}
    assert templates["mine"].panes[0].command == "my-wrapper"


def test_both_seeders_coexist(toml_path):
    ensure_native_template_seeded(toml_path)
    ensure_agentic_template_seeded(toml_path)
    templates = load_user_templates(toml_path)
    assert set(templates) == {"native", "agentic"}
    assert templates["agentic"].entry_role == "conductor"


def test_malformed_toml_is_left_alone_by_seeders(toml_path):
    toml_path.write_text("this is not = = toml")
    assert ensure_native_template_seeded(toml_path) is False
    assert toml_path.read_text() == "this is not = = toml"


def test_malformed_toml_raises_a_named_error_on_load(toml_path):
    toml_path.write_text("this is not = = toml")
    with pytest.raises(ValueError, match=str(toml_path)):
        load_user_templates(toml_path)


def test_missing_file_yields_no_templates(tmp_path):
    assert load_user_templates(tmp_path / "nope.toml") == {}


def test_entry_role_must_be_one_of_the_panes():
    with pytest.raises(ValueError, match="entry_role"):
        Template(
            layout="main_left_grid_right",
            panes=(),
            entry_role="researcher",
        )


class TestMigrateRoleCommands:
    def test_rewrites_retired_dash_and_colon_names(self, toml_path):
        toml_path.write_text(
            '[templates.t]\nlayout = "main_left_grid_right"\nentry_role = "planner"\n\n'
            '[[templates.t.panes]]\nrole = "planner"\ncommand = "claudespace-planner"\n\n'
            '[[templates.t.panes]]\nrole = "reviewer"\ncommand = "claudespace:reviewer"\n'
        )
        assert migrate_role_commands(toml_path) is True
        commands = {p.role: p.command for p in load_user_templates(toml_path)["t"].panes}
        assert commands["planner"] == ROLE_COMMANDS["planner"]
        assert commands["reviewer"] == ROLE_COMMANDS["reviewer"]

    def test_leaves_user_wrappers_untouched(self, toml_path):
        original = (
            '[templates.t]\nlayout = "main_left_grid_right"\n\n'
            '[[templates.t.panes]]\nrole = "researcher"\n'
            'command = "claude2 --model claude-sonnet-5 --effort low"\n'
        )
        toml_path.write_text(original)
        assert migrate_role_commands(toml_path) is False
        assert toml_path.read_text() == original

    def test_backs_up_before_rewriting(self, toml_path):
        toml_path.write_text(
            '[templates.t]\nlayout = "main_left_grid_right"\nentry_role = "planner"\n\n'
            '[[templates.t.panes]]\nrole = "planner"\ncommand = "claudespace-planner"\n'
        )
        migrate_role_commands(toml_path)
        backups = list(toml_path.parent.glob("templates.toml.bak-*"))
        assert len(backups) == 1
        assert "claudespace-planner" in backups[0].read_text()

    def test_no_backup_when_nothing_changes(self, toml_path):
        toml_path.write_text(NATIVE_TEMPLATE_TOML)
        assert migrate_role_commands(toml_path) is False
        assert list(toml_path.parent.glob("*.bak-*")) == []

    def test_missing_file_is_a_no_op(self, tmp_path):
        assert migrate_role_commands(tmp_path / "nope.toml") is False


class TestLoadTerminalBackend:
    # AD5's selection matrix: unset -> iterm2, config.toml value, env
    # override precedence over the file, invalid value -> named error.
    def test_defaults_to_iterm2_when_nothing_is_configured(self, toml_path):
        assert load_terminal_backend(toml_path, env={}) == "iterm2"

    def test_reads_the_configured_value(self, toml_path):
        toml_path.write_text('[terminal]\nbackend = "ghostty"\n')
        assert load_terminal_backend(toml_path, env={}) == "ghostty"

    def test_env_override_wins_over_the_file(self, toml_path):
        toml_path.write_text('[terminal]\nbackend = "ghostty"\n')
        assert (
            load_terminal_backend(toml_path, env={"CLAUDESPACE_TERMINAL": "iterm2"})
            == "iterm2"
        )

    def test_env_override_works_without_a_file(self, tmp_path):
        assert (
            load_terminal_backend(
                tmp_path / "nope.toml", env={"CLAUDESPACE_TERMINAL": "ghostty"}
            )
            == "ghostty"
        )

    def test_unknown_value_in_the_file_is_a_named_error(self, toml_path):
        toml_path.write_text('[terminal]\nbackend = "warp"\n')
        with pytest.raises(ValueError, match="warp"):
            load_terminal_backend(toml_path, env={})

    def test_unknown_env_value_is_a_named_error(self, toml_path):
        with pytest.raises(ValueError, match="warp"):
            load_terminal_backend(toml_path, env={"CLAUDESPACE_TERMINAL": "warp"})

    def test_missing_terminal_table_defaults_to_iterm2(self, toml_path):
        toml_path.write_text('[templates.mine]\nlayout = "main_left_grid_right"\n')
        assert load_terminal_backend(toml_path, env={}) == "iterm2"
