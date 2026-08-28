"""Workspace-variable lookups and the persona/prefill decision.

Covers the two pieces of iterm.py that don't need a live iTerm2: the
session-variable getters (three near-identical loops collapsed into one) and
the rule deciding whether a pane still needs a `/role` slash command.
"""

from __future__ import annotations

import asyncio

from claudespace import iterm


class FakeSession:
    def __init__(self, variables: dict):
        self._variables = variables

    async def async_get_variable(self, name):
        return self._variables.get(name)


class FakeTab:
    def __init__(self, sessions):
        self.sessions = sessions


class FakeWindow:
    def __init__(self, tabs):
        self.tabs = tabs


class FakeApp:
    def __init__(self, *sessions):
        self.windows = [FakeWindow([FakeTab(list(sessions))])]


def _pane(marker="/root", instance="i1", **extra):
    return FakeSession(
        {
            iterm.WORKSPACE_VAR: marker,
            iterm.INSTANCE_VAR: instance,
            **extra,
        }
    )


def test_getters_read_from_any_matching_pane():
    app = FakeApp(
        _pane(),
        _pane(
            **{
                iterm.AUTO_HANDOFF_VAR: True,
                iterm.LAZY_VAR: True,
                iterm.TEMPLATE_VAR: "agentic",
            }
        ),
    )
    # The first pane carries no values; the loop must keep looking only in
    # the sense that it returns the first *matching workspace* pane - which
    # here is the first one, so these are all falsy/None.
    assert asyncio.run(iterm.get_auto_handoff(app, marker="/root")) is False
    assert asyncio.run(iterm.get_lazy(app, marker="/root")) is False
    assert asyncio.run(iterm.get_template_name(app, marker="/root")) is None


def test_getters_return_the_values_a_pane_carries():
    app = FakeApp(
        _pane(
            **{
                iterm.AUTO_HANDOFF_VAR: True,
                iterm.LAZY_VAR: True,
                iterm.TEMPLATE_VAR: "agentic",
            }
        )
    )
    assert asyncio.run(iterm.get_auto_handoff(app, marker="/root")) is True
    assert asyncio.run(iterm.get_lazy(app, marker="/root")) is True
    assert asyncio.run(iterm.get_template_name(app, marker="/root")) == "agentic"


def test_getters_default_when_workspace_is_absent():
    app = FakeApp(_pane(marker="/other"))
    assert asyncio.run(iterm.get_auto_handoff(app, marker="/root")) is False
    assert asyncio.run(iterm.get_lazy(app, marker="/root")) is False
    assert asyncio.run(iterm.get_template_name(app, marker="/root")) is None


def test_instance_filter_rejects_a_same_root_pane_in_another_window():
    # Two windows on one root is exactly the case INSTANCE_VAR exists for:
    # without it a handoff could land in the wrong terminal.
    app = FakeApp(_pane(instance="other", **{iterm.TEMPLATE_VAR: "agentic"}))
    assert asyncio.run(iterm.get_template_name(app, marker="/root", instance="i1")) is None
    assert asyncio.run(iterm.get_template_name(app, marker="/root", instance="other")) == "agentic"


class TestRolePromptPrefix:
    def test_no_prefix_when_the_persona_is_baked_in(self, tmp_path, monkeypatch):
        monkeypatch.setattr(iterm, "PROMPTS_DEST", tmp_path)
        (tmp_path / "researcher.prompt.md").write_text("persona")
        # The whole point of the slash command is to make the model read this
        # file; it is already in the system prompt, so asking again is pure
        # duplicated context.
        assert iterm.role_prompt_prefix("researcher") == ""

    def test_falls_back_to_the_slash_command_without_a_prompt_file(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(iterm, "PROMPTS_DEST", tmp_path)
        assert iterm.role_prompt_prefix("mycustomrole") == "/mycustomrole "


class TestCommandWithBakedPersona:
    def test_appends_the_prompt_file_and_the_session_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(iterm, "PROMPTS_DEST", tmp_path)
        prompt = tmp_path / "reviewer.prompt.md"
        prompt.write_text("persona")
        command = iterm._command_with_baked_persona("reviewer", "claude --model x")
        # --name is what labels the pane inside Claude Code's own TUI, now
        # that panes are no longer prefilled with `/reviewer`.
        assert command == (
            f"claude --model x --append-system-prompt-file {prompt} --name reviewer"
        )

    def test_quotes_a_path_containing_spaces(self, tmp_path, monkeypatch):
        directory = tmp_path / "my prompts"
        directory.mkdir()
        monkeypatch.setattr(iterm, "PROMPTS_DEST", directory)
        (directory / "reviewer.prompt.md").write_text("persona")
        command = iterm._command_with_baked_persona("reviewer", "claude")
        assert "'" in command or '"' in command

    def test_leaves_the_command_alone_without_a_prompt_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(iterm, "PROMPTS_DEST", tmp_path)
        assert iterm._command_with_baked_persona("nope", "my-wrapper") == "my-wrapper"


def test_every_pipeline_role_has_a_theme():
    # A role with no theme gets no badge and no color, so it is unidentifiable
    # once Claude Code's TUI paints over the pane. conductor was missing one.
    from claudespace.pipeline import PIPELINE
    from claudespace.themes import ROLE_THEMES

    assert set(PIPELINE) <= set(ROLE_THEMES), sorted(set(PIPELINE) - set(ROLE_THEMES))


def test_role_profile_carries_a_badge():
    # The badge is the one role label that survives Claude Code's TUI, so a
    # role losing it means an unidentifiable pane.
    from claudespace.themes import build_role_profile

    values = build_role_profile("researcher").values
    assert values["Badge Text"] == '"RESEARCHER"'
    assert "Badge Color" in values
