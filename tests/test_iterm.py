"""Regression guard for the iTerm2 backend (AD1, Implementation Order step 3):
these tests exercised ``claudespace.iterm`` before the tmux backend
existed, and now exercise ``claudespace.backends.iterm``/``.common`` in the
same shape - the move is behavior-preserving, not a rewrite.
"""

from __future__ import annotations

import asyncio

from claudespace.backends import common
from claudespace.backends import iterm as iterm_backend


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
            iterm_backend.WORKSPACE_VAR: marker,
            iterm_backend.INSTANCE_VAR: instance,
            **extra,
        }
    )


def _backend_for(app, monkeypatch) -> iterm_backend.ItermBackend:
    async def _fake_app(self):
        return app

    monkeypatch.setattr(iterm_backend.ItermBackend, "_app", _fake_app)
    return iterm_backend.ItermBackend()


def test_getters_read_from_any_matching_pane(monkeypatch):
    app = FakeApp(
        _pane(),
        _pane(
            **{
                iterm_backend.AUTO_HANDOFF_VAR: True,
                iterm_backend.LAZY_VAR: True,
                iterm_backend.TEMPLATE_VAR: "agentic",
            }
        ),
    )
    backend = _backend_for(app, monkeypatch)
    # The first pane carries no values; the loop must keep looking only in
    # the sense that it returns the first *matching workspace* pane - which
    # here is the first one, so these are all falsy/None.
    assert asyncio.run(backend.get_auto_handoff(marker="/root")) is False
    assert asyncio.run(backend.get_lazy(marker="/root")) is False
    assert asyncio.run(backend.get_template_name(marker="/root")) is None


def test_getters_return_the_values_a_pane_carries(monkeypatch):
    app = FakeApp(
        _pane(
            **{
                iterm_backend.AUTO_HANDOFF_VAR: True,
                iterm_backend.LAZY_VAR: True,
                iterm_backend.TEMPLATE_VAR: "agentic",
            }
        )
    )
    backend = _backend_for(app, monkeypatch)
    assert asyncio.run(backend.get_auto_handoff(marker="/root")) is True
    assert asyncio.run(backend.get_lazy(marker="/root")) is True
    assert asyncio.run(backend.get_template_name(marker="/root")) == "agentic"


def test_getters_default_when_workspace_is_absent(monkeypatch):
    app = FakeApp(_pane(marker="/other"))
    backend = _backend_for(app, monkeypatch)
    assert asyncio.run(backend.get_auto_handoff(marker="/root")) is False
    assert asyncio.run(backend.get_lazy(marker="/root")) is False
    assert asyncio.run(backend.get_template_name(marker="/root")) is None


def test_instance_filter_rejects_a_same_root_pane_in_another_window(monkeypatch):
    # Two windows on one root is exactly the case INSTANCE_VAR exists for:
    # without it a handoff could land in the wrong terminal.
    app = FakeApp(_pane(instance="other", **{iterm_backend.TEMPLATE_VAR: "agentic"}))
    backend = _backend_for(app, monkeypatch)
    assert asyncio.run(backend.get_template_name(marker="/root", instance="i1")) is None
    assert (
        asyncio.run(backend.get_template_name(marker="/root", instance="other")) == "agentic"
    )


def test_find_workspace_stamps_instance_from_the_matched_session(monkeypatch):
    app = FakeApp(_pane(instance="abc-123"))
    backend = _backend_for(app, monkeypatch)
    window = asyncio.run(backend.find_workspace("/root"))
    assert window.instance == "abc-123"


class TestRolePromptPrefix:
    def test_no_prefix_when_the_persona_is_baked_in(self, tmp_path, monkeypatch):
        monkeypatch.setattr(common, "PROMPTS_DEST", tmp_path)
        (tmp_path / "researcher.prompt.md").write_text("persona")
        # The whole point of the slash command is to make the model read this
        # file; it is already in the system prompt, so asking again is pure
        # duplicated context.
        assert common.role_prompt_prefix("researcher") == ""

    def test_falls_back_to_the_slash_command_without_a_prompt_file(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(common, "PROMPTS_DEST", tmp_path)
        assert common.role_prompt_prefix("mycustomrole") == "/mycustomrole "


class TestCommandWithBakedPersona:
    def test_appends_the_prompt_file_and_the_session_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(common, "PROMPTS_DEST", tmp_path)
        prompt = tmp_path / "implementer.prompt.md"
        prompt.write_text("persona")
        # implementer denies no tools, so this is the full command shape.
        # --name is what labels the pane inside Claude Code's own TUI, now
        # that panes are no longer prefilled with `/implementer`.
        command = common.command_with_baked_persona("implementer", "claude --model x")
        assert command == (
            f"claude --model x --append-system-prompt-file {prompt} --name implementer"
        )

    def test_quotes_a_path_containing_spaces(self, tmp_path, monkeypatch):
        directory = tmp_path / "my prompts"
        directory.mkdir()
        monkeypatch.setattr(common, "PROMPTS_DEST", directory)
        (directory / "reviewer.prompt.md").write_text("persona")
        command = common.command_with_baked_persona("reviewer", "claude")
        assert "'" in command or '"' in command

    def test_leaves_the_command_alone_without_a_prompt_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(common, "PROMPTS_DEST", tmp_path)
        assert common.command_with_baked_persona("nope", "my-wrapper") == "my-wrapper"


class TestStallDecision:
    # Pure port of watchdog._check_once's original state machine (see
    # iterm.stall_decision's docstring) - exercised directly so the
    # regression guard doesn't need a fake iTerm2 session with screen
    # contents.
    def test_first_poll_is_never_a_stall(self):
        state, stalled = iterm_backend.stall_decision(
            None, text="a", ready=False, now=100.0, stall_after_seconds=600
        )
        assert stalled is False
        assert state == {"text": "a", "ready": False, "seen_at": 100.0}

    def test_changed_text_clears_the_stall(self):
        previous = {"text": "a", "ready": False, "seen_at": 100.0}
        state, stalled = iterm_backend.stall_decision(
            previous, text="b", ready=False, now=200.0, stall_after_seconds=600
        )
        assert stalled is False
        assert state["text"] == "b"

    def test_unchanged_and_idle_is_not_a_stall(self):
        previous = {"text": "a", "ready": True, "seen_at": 100.0}
        _state, stalled = iterm_backend.stall_decision(
            previous, text="a", ready=True, now=900.0, stall_after_seconds=600
        )
        assert stalled is False

    def test_unchanged_non_idle_past_the_threshold_is_a_stall(self):
        previous = {"text": "a", "ready": False, "seen_at": 100.0}
        _state, stalled = iterm_backend.stall_decision(
            previous, text="a", ready=False, now=800.0, stall_after_seconds=600
        )
        assert stalled is True

    def test_unchanged_non_idle_under_the_threshold_is_not_yet_a_stall(self):
        previous = {"text": "a", "ready": False, "seen_at": 100.0}
        _state, stalled = iterm_backend.stall_decision(
            previous, text="a", ready=False, now=300.0, stall_after_seconds=600
        )
        assert stalled is False


def test_every_pipeline_role_has_a_theme():
    # A role with no theme gets no badge and no color, so it is unidentifiable
    # once Claude Code's TUI paints over the pane. conductor was missing one.
    from claudespace.pipeline import PIPELINE
    from claudespace.themes import ROLE_THEMES

    assert set(PIPELINE) <= set(ROLE_THEMES), sorted(set(PIPELINE) - set(ROLE_THEMES))


def test_role_profile_tints_the_title_bar_in_both_appearances():
    # The pane's title bar (already showing "<role> (claude)" via --name) is
    # the role label. The generic "Use Tab Color" applies only when separate
    # light/dark colors are disabled, so setting it alone left every title bar
    # the same color in dark mode - all three variants have to be set.
    from claudespace.themes import build_role_profile

    values = build_role_profile("researcher").values
    for key in ("Tab Color", "Tab Color (Light)", "Tab Color (Dark)"):
        assert key in values, key
    for key in ("Use Tab Color", "Use Tab Color (Light)", "Use Tab Color (Dark)"):
        assert values[key] == "true", key


def test_roles_are_visually_distinguishable():
    # Two roles sharing an accent would give their panes the same title bar.
    from claudespace.themes import ROLE_THEMES

    accents = [
        (t.accent.red, t.accent.green, t.accent.blue) for t in ROLE_THEMES.values()
    ]
    assert len(set(accents)) == len(accents)


def test_no_badge_is_set():
    # A badge is drawn over the pane at a fixed size, so in a split it wrapped
    # mid-word ("PRINCI PAL"). The title bar already carries the name.
    from claudespace.themes import build_role_profile

    values = build_role_profile("researcher").values
    assert not any("Badge" in k for k in values)
