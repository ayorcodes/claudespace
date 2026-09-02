"""``backends/ghostty.py`` with a faked osascript runner (the subprocess
boundary is injected via ``GhosttyBackend(runner=...)`` - see the design
doc's Tests Required): build/reveal/find/each/send map to the expected
AppleScript command sequences, and the reachability probe classifies each
failure mode correctly.
"""

from __future__ import annotations

import asyncio

import pytest

from claudespace.backends import ghostty, ghostty_state
from claudespace.backends.base import BackendUnavailableError
from claudespace.config import PaneConfig, Template


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ghostty_state, "STATE_DIR", tmp_path / "ghostty")


class ScriptedRunner:
    """Fakes the ``osascript`` subprocess boundary: records every script it
    was asked to run and answers from a caller-supplied queue/mapping.
    """

    def __init__(self):
        self.calls: list[str] = []
        self._responses: list[str] = []
        self._by_prefix: dict[str, str] = {}
        self._raise: Exception | None = None
        self._custom = None

    def set_custom(self, fn) -> "ScriptedRunner":
        self._custom = fn
        return self

    def queue(self, *responses: str) -> "ScriptedRunner":
        self._responses.extend(responses)
        return self

    def respond_when_containing(self, needle: str, response: str) -> "ScriptedRunner":
        self._by_prefix[needle] = response
        return self

    def fail_with(self, exc: Exception) -> "ScriptedRunner":
        self._raise = exc
        return self

    def __call__(self, script: str, timeout: float) -> str:
        self.calls.append(script)
        if self._raise is not None:
            raise self._raise
        if self._custom is not None:
            return self._custom(script, timeout)
        for needle, response in self._by_prefix.items():
            if needle in script:
                return response
        if self._responses:
            return self._responses.pop(0)
        return ""


def _template() -> Template:
    return Template(
        layout="main_left_grid_right",
        panes=(
            PaneConfig(role="principal", command="claude"),
            PaneConfig(role="implementer", command="claude"),
            PaneConfig(role="reviewer", command="claude"),
            PaneConfig(role="planner", command="claude"),
            PaneConfig(role="researcher", command="claude"),
        ),
    )


class TestScriptBuilders:
    def test_split_addresses_by_id_and_direction(self):
        script = ghostty.script_split("term-1", "right")
        assert '"term-1"' in script
        assert "direction right" in script

    def test_input_text_quotes_the_payload(self):
        script = ghostty.script_input_text("term-1", 'say "hi"')
        assert '\\"hi\\"' in script

    def test_as_applescript_str_escapes_backslashes_and_quotes(self):
        assert ghostty._as_applescript_str('a"b\\c') == '"a\\"b\\\\c"'


class TestReachabilityProbe:
    def test_not_running_exits_before_any_osascript_call(self, monkeypatch):
        monkeypatch.setattr(ghostty.utils, "is_ghostty_running", lambda: False)
        runner = ScriptedRunner()
        backend = ghostty.GhosttyBackend(runner=runner)
        with pytest.raises(SystemExit):
            backend._probe_reachability()
        assert runner.calls == []

    def test_tcc_denial_is_classified(self, monkeypatch, caplog):
        monkeypatch.setattr(ghostty.utils, "is_ghostty_running", lambda: True)
        runner = ScriptedRunner().fail_with(
            ghostty.OsascriptError("Not authorized to send Apple events", returncode=1)
        )
        backend = ghostty.GhosttyBackend(runner=runner)
        with pytest.raises(SystemExit):
            backend._probe_reachability()
        assert "Automation" in caplog.text or "authorized" in caplog.text.lower()

    def test_timeout_is_classified(self, monkeypatch, caplog):
        monkeypatch.setattr(ghostty.utils, "is_ghostty_running", lambda: True)
        runner = ScriptedRunner().fail_with(BackendUnavailableError("timed out"))
        backend = ghostty.GhosttyBackend(runner=runner)
        with pytest.raises(SystemExit):
            backend._probe_reachability()
        assert "timed out" in caplog.text.lower()

    def test_old_version_is_rejected(self, monkeypatch, caplog):
        monkeypatch.setattr(ghostty.utils, "is_ghostty_running", lambda: True)
        runner = ScriptedRunner().queue("1.2.0")
        backend = ghostty.GhosttyBackend(runner=runner)
        with pytest.raises(SystemExit):
            backend._probe_reachability()
        assert "1.2.0" in caplog.text

    def test_current_version_passes(self, monkeypatch):
        monkeypatch.setattr(ghostty.utils, "is_ghostty_running", lambda: True)
        runner = ScriptedRunner().queue("1.3.0")
        backend = ghostty.GhosttyBackend(runner=runner)
        backend._probe_reachability()  # does not raise/exit


class TestBuildWorkspaceEager:
    def test_creates_window_and_launches_every_pane(self):
        runner = ScriptedRunner()
        # Every split returns a fresh incrementing terminal id.
        split_ids = iter(["term-2", "term-3", "term-4", "term-5"])

        def _call(script, timeout):
            if "split" in script:
                return next(split_ids)
            if "make new window" in script:
                return "win-1|term-root"
            return ""

        runner.set_custom(_call)
        backend = ghostty.GhosttyBackend(runner=runner)

        window = asyncio.run(
            backend.build_workspace(
                marker="/root",
                root="/root",
                template_name="native",
                template=_template(),
            )
        )
        assert window.window_id == "win-1"

        state = ghostty_state.load("/root")
        instance = next(iter(state["instances"].values()))
        assert set(instance["roles"]) == {
            "principal",
            "implementer",
            "reviewer",
            "planner",
            "researcher",
        }

    def test_lazy_launches_only_the_entry_role(self):
        runner = ScriptedRunner()
        runner.respond_when_containing("make new window", "win-1|term-root")
        backend = ghostty.GhosttyBackend(runner=runner)

        asyncio.run(
            backend.build_workspace(
                marker="/root",
                root="/root",
                template_name="native",
                template=_template(),
                lazy=True,
            )
        )
        state = ghostty_state.load("/root")
        instance = next(iter(state["instances"].values()))
        assert set(instance["roles"]) == {"researcher"}
        assert instance["lazy"] is True


class TestFindAndEachPane:
    def test_find_role_pane_checks_liveness(self):
        ghostty_state.set_role_pane("/root", "inst-1", "researcher", "term-1")
        runner = ScriptedRunner().respond_when_containing("exists", "true")
        backend = ghostty.GhosttyBackend(runner=runner)
        pane = asyncio.run(backend.find_role_pane(marker="/root", role="researcher"))
        assert pane is not None
        assert pane.terminal_id == "term-1"

    def test_find_role_pane_treats_a_dead_terminal_as_absent(self):
        ghostty_state.set_role_pane("/root", "inst-1", "researcher", "term-1")
        runner = ScriptedRunner().respond_when_containing("exists", "false")
        backend = ghostty.GhosttyBackend(runner=runner)
        pane = asyncio.run(backend.find_role_pane(marker="/root", role="researcher"))
        assert pane is None

    def test_each_pane_yields_role_and_pane(self):
        ghostty_state.set_role_pane("/root", "inst-1", "researcher", "term-1")
        ghostty_state.set_role_pane("/root", "inst-1", "planner", "term-2")
        runner = ScriptedRunner().respond_when_containing("exists", "true")
        backend = ghostty.GhosttyBackend(runner=runner)

        async def _collect():
            return [pair async for pair in backend.each_pane(marker="/root")]

        results = asyncio.run(_collect())
        assert {role: pane.terminal_id for role, pane in results} == {
            "researcher": "term-1",
            "planner": "term-2",
        }


class TestGettersAndRunDoc:
    def test_get_auto_handoff_and_lazy_default_false_when_absent(self):
        backend = ghostty.GhosttyBackend(runner=ScriptedRunner())
        assert asyncio.run(backend.get_auto_handoff(marker="/root")) is False
        assert asyncio.run(backend.get_lazy(marker="/root")) is False

    def test_get_run_doc_round_trips_via_set_run_doc(self):
        ghostty_state.update_instance("/root", "inst-1", template="native")
        backend = ghostty.GhosttyBackend(runner=ScriptedRunner())
        asyncio.run(
            backend.set_run_doc(
                marker="/root", instance="inst-1", doc="docs/x.md", started_at=123.0
            )
        )
        doc, started = asyncio.run(backend.get_run_doc(marker="/root", instance="inst-1"))
        assert doc == "docs/x.md"
        assert started == 123.0


class TestCheckPaneStall:
    def test_alive_pane_is_never_flagged(self):
        runner = ScriptedRunner().respond_when_containing("exists", "true")
        backend = ghostty.GhosttyBackend(runner=runner)
        pane = ghostty.GhosttyPane(terminal_id="term-1", window_id="win-1")
        state, stalled = asyncio.run(
            backend.check_pane_stall(
                pane, role="researcher", previous=None, now=1.0, stall_after_seconds=600
            )
        )
        assert stalled is False
        assert state == {"notified": False}

    def test_dead_pane_is_flagged_once(self):
        runner = ScriptedRunner().respond_when_containing("exists", "false")
        backend = ghostty.GhosttyBackend(runner=runner)
        pane = ghostty.GhosttyPane(terminal_id="term-1", window_id="win-1")

        state1, stalled1 = asyncio.run(
            backend.check_pane_stall(
                pane, role="researcher", previous=None, now=1.0, stall_after_seconds=600
            )
        )
        assert stalled1 is True

        _state2, stalled2 = asyncio.run(
            backend.check_pane_stall(
                pane, role="researcher", previous=state1, now=2.0, stall_after_seconds=600
            )
        )
        assert stalled2 is False  # already notified, not re-flagged every poll
