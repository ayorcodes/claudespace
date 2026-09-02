"""``backends/tmux.py`` (``TmuxBackend``) headless integration: real tmux,
no terminal/display involved - the payoff of AD3 (Tests Required)."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid

import pytest

from claudespace.backends import tmux_cli
from claudespace.backends.tmux import TmuxBackend
from claudespace.config import PaneConfig, Template


@pytest.fixture(autouse=True)
def _isolated_tmux_server(monkeypatch, tmp_path):
    socket_dir = tempfile.mkdtemp(prefix="cstmux-")
    monkeypatch.setenv("TMUX_TMPDIR", socket_dir)
    monkeypatch.setattr(
        "claudespace.backends.tmux_persist.CONF_PATH", tmp_path / "no-conf-here.conf"
    )
    yield
    shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_persona_baking(tmp_path, monkeypatch):
    # Keep launched commands plain (no --append-system-prompt-file/--name):
    # these tests fake `claude` with plain shell snippets, and the launched
    # text is asserted against directly in a couple of tests.
    monkeypatch.setattr("claudespace.backends.common.PROMPTS_DEST", tmp_path / "prompts")


pytestmark = pytest.mark.skipif(not tmux_cli.is_tmux_available(), reason="tmux not installed")


# A fake `claude`: prints the ready marker once, then echoes each further
# line back with the marker again - deterministic stand-in for readiness
# polling and submit-confirmation without a real `claude` process.
FAKE_CLAUDE = "printf '\\xe2\\x9d\\xaf '; while IFS= read -r line; do clear; printf '\\xe2\\x9d\\xaf '; done"


def _native_template(command: str = "printf '\\xe2\\x9d\\xaf '") -> Template:
    return Template(
        layout="main_left_grid_right",
        panes=(
            PaneConfig(role="principal", command=command),
            PaneConfig(role="implementer", command=command),
            PaneConfig(role="reviewer", command=command),
            PaneConfig(role="planner", command=command),
            PaneConfig(role="researcher", command=command),
        ),
    )


def _marker(tmp_path) -> str:
    # tmux's `new-session -c <dir>` hangs its client indefinitely if <dir>
    # doesn't exist (rather than erroring) - unlike every real caller
    # (workspace.py always os.makedirs's the root first), so tests must
    # create it themselves.
    root = tmp_path / f"root-{uuid.uuid4().hex[:8]}"
    root.mkdir()
    return str(root)


class TestBuildWorkspace:
    def test_eager_build_launches_every_pane(self, tmp_path):
        backend = TmuxBackend(persist=False)
        marker = _marker(tmp_path)

        async def _scenario():
            window = await backend.build_workspace(
                marker=marker,
                root=marker,
                template_name="native",
                template=_native_template(),
            )
            pairs = [pair async for pair in backend.each_pane(marker=marker)]
            roles = {role for role, _pane in pairs}
            assert roles == {"principal", "implementer", "reviewer", "planner", "researcher"}
            await tmux_cli.kill_session(window.session)

        asyncio.run(_scenario())

    def test_lazy_build_launches_only_entry_role(self, tmp_path):
        backend = TmuxBackend(persist=False)
        marker = _marker(tmp_path)

        async def _scenario():
            window = await backend.build_workspace(
                marker=marker,
                root=marker,
                template_name="native",
                template=_native_template(),
                lazy=True,
            )
            pairs = [pair async for pair in backend.each_pane(marker=marker)]
            assert [role for role, _pane in pairs] == ["researcher"]
            assert await backend.get_lazy(marker=marker) is True
            await tmux_cli.kill_session(window.session)

        asyncio.run(_scenario())


class TestStateRoundTrip:
    def test_auto_handoff_lazy_and_template_persist(self, tmp_path):
        backend = TmuxBackend(persist=False)
        marker = _marker(tmp_path)

        async def _scenario():
            window = await backend.build_workspace(
                marker=marker,
                root=marker,
                template_name="native",
                template=_native_template(),
                auto_handoff=False,
                lazy=True,
            )
            assert await backend.get_auto_handoff(marker=marker) is False
            assert await backend.get_lazy(marker=marker) is True
            assert await backend.get_template_name(marker=marker) == "native"
            await tmux_cli.kill_session(window.session)

        asyncio.run(_scenario())

    def test_run_doc_round_trips_and_stamps_every_pane(self, tmp_path):
        backend = TmuxBackend(persist=False)
        marker = _marker(tmp_path)

        async def _scenario():
            window = await backend.build_workspace(
                marker=marker,
                root=marker,
                template_name="native",
                template=_native_template(),
            )
            await backend.set_run_doc(marker=marker, doc="docs/x.md", started_at=123.0)
            doc, started = await backend.get_run_doc(marker=marker)
            assert doc == "docs/x.md"
            assert started == 123.0

            # Stamped on every pane, not just one.
            rows = await tmux_cli.list_panes_all(("pane_id", "@cs_run_doc"))
            our_rows = [r for r in rows if r["pane_id"] in {
                p.pane_id async for _role, p in backend.each_pane(marker=marker)
            }]
            assert all(r["@cs_run_doc"] == "docs/x.md" for r in our_rows)
            await tmux_cli.kill_session(window.session)

        asyncio.run(_scenario())


class TestFindAndReveal:
    def test_find_workspace_and_find_role_pane(self, tmp_path):
        backend = TmuxBackend(persist=False)
        marker = _marker(tmp_path)

        async def _scenario():
            window = await backend.build_workspace(
                marker=marker,
                root=marker,
                template_name="native",
                template=_native_template(),
            )
            found = await backend.find_workspace(marker)
            assert found is not None
            assert found.session == window.session

            pane = await backend.find_role_pane(marker=marker, role="planner")
            assert pane is not None
            assert pane.session == window.session

            assert await backend.find_role_pane(marker=marker, role="conductor") is None
            await tmux_cli.kill_session(window.session)

        asyncio.run(_scenario())

    def test_reveal_role_splits_and_launches(self, tmp_path):
        backend = TmuxBackend(persist=False)
        marker = _marker(tmp_path)

        async def _scenario():
            window = await backend.build_workspace(
                marker=marker,
                root=marker,
                template_name="native",
                template=_native_template(),
                lazy=True,
            )
            source = await backend.find_role_pane(marker=marker, role="researcher")
            assert source is not None

            revealed = await backend.reveal_role(
                marker=marker,
                instance="whatever",
                root=marker,
                template=_native_template(),
                role="planner",
                source=source,
            )
            assert revealed is not None
            assert revealed.pane_id != source.pane_id

            found = await backend.find_role_pane(marker=marker, role="planner")
            assert found is not None
            assert found.pane_id == revealed.pane_id
            await tmux_cli.kill_session(window.session)

        asyncio.run(_scenario())

    def test_instance_filter_distinguishes_two_windows_on_one_root(self, tmp_path):
        backend = TmuxBackend(persist=False)
        marker = _marker(tmp_path)

        async def _scenario():
            window_a = await backend.build_workspace(
                marker=marker, root=marker, template_name="native", template=_native_template()
            )
            window_b = await backend.build_workspace(
                marker=marker, root=marker, template_name="native", template=_native_template()
            )
            assert window_a.session != window_b.session

            rows_a = await backend._matching_rows(marker, None)
            sessions = {r["session_name"] for r in rows_a}
            assert sessions == {window_a.session, window_b.session}

            await tmux_cli.kill_session(window_a.session)
            await tmux_cli.kill_session(window_b.session)

        asyncio.run(_scenario())


class TestPromptDeliveryAndStall:
    def test_send_role_prompt_delivers_and_confirms_submission(self, tmp_path):
        backend = TmuxBackend(persist=False)
        marker = _marker(tmp_path)

        async def _scenario():
            window = await backend.build_workspace(
                marker=marker,
                root=marker,
                template_name="native",
                template=_native_template(FAKE_CLAUDE),
            )
            pane = await backend.find_role_pane(marker=marker, role="researcher")
            assert pane is not None

            await backend.send_role_prompt(
                "researcher", pane, text="hello from a handoff", submit=True
            )
            captured = await tmux_cli.capture_pane(pane.pane_id)
            # The fake claude clears the screen and reprints the ready
            # marker after each submitted line - confirms the text was
            # actually submitted, not just typed and left sitting there.
            assert "hello from a handoff" not in captured
            assert "❯" in captured
            await tmux_cli.kill_session(window.session)

        asyncio.run(_scenario())

    def test_check_pane_stall_flags_a_dead_pane(self, tmp_path):
        backend = TmuxBackend(persist=False)
        marker = _marker(tmp_path)

        async def _scenario():
            # Prints the ready marker (so build's own prefill step doesn't
            # burn its full readiness timeout), then exits shortly after -
            # tmux destroys a pane whose process exits, which is exactly
            # the "crashed/closed pane" case this checks for.
            window = await backend.build_workspace(
                marker=marker,
                root=marker,
                template_name="native",
                template=_native_template("printf '\\xe2\\x9d\\xaf '; sleep 0.3; exit 0"),
            )
            pane = await backend.find_role_pane(marker=marker, role="researcher")
            assert pane is not None
            await asyncio.sleep(1.0)

            state1, _ = await backend.check_pane_stall(
                pane, role="researcher", previous=None, now=1.0, stall_after_seconds=600
            )
            state2, stalled = await backend.check_pane_stall(
                pane, role="researcher", previous=state1, now=700.0, stall_after_seconds=600
            )
            assert stalled is True
            await tmux_cli.kill_session(window.session)

        asyncio.run(_scenario())

    def test_check_pane_stall_never_flags_a_changing_screen(self, tmp_path):
        backend = TmuxBackend(persist=False)
        marker = _marker(tmp_path)

        async def _scenario():
            window = await backend.build_workspace(
                marker=marker,
                root=marker,
                template_name="native",
                template=_native_template(FAKE_CLAUDE),
            )
            pane = await backend.find_role_pane(marker=marker, role="researcher")
            assert pane is not None

            state1, stalled1 = await backend.check_pane_stall(
                pane, role="researcher", previous=None, now=1.0, stall_after_seconds=600
            )
            assert stalled1 is False
            # Idle at the ready prompt, unchanged - never a stall regardless
            # of elapsed time.
            state2, stalled2 = await backend.check_pane_stall(
                pane, role="researcher", previous=state1, now=10000.0, stall_after_seconds=600
            )
            assert stalled2 is False
            await tmux_cli.kill_session(window.session)

        asyncio.run(_scenario())
