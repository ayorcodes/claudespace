"""``backends/tmux.py`` (``TmuxBackend``) headless integration: real tmux,
no terminal/display involved - the payoff of AD3 (Tests Required)."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid

import pytest

from claudespace.backends import tmux_cli
from claudespace.backends.tmux import TmuxBackend, _slugify_run_doc
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


class TestSlugifyRunDoc:
    def test_strips_docs_path_date_and_extension(self):
        assert _slugify_run_doc("docs/research/2026-09-03-fix-kitchen-heat-link.md") == "fix-kitchen-heat-link"

    def test_handles_free_text_backlog_description(self):
        assert _slugify_run_doc("Add dark mode") == "add-dark-mode"

    def test_truncates_long_slugs(self):
        long_doc = "docs/research/2026-09-03-" + ("a" * 50) + ".md"
        slug = _slugify_run_doc(long_doc)
        assert len(slug) <= 30

    def test_empty_or_symbols_only_falls_back_to_run(self):
        assert _slugify_run_doc("docs/research/2026-09-03-.md") == "run"
        assert _slugify_run_doc("///") == "run"

    def test_no_leading_or_trailing_hyphens(self):
        slug = _slugify_run_doc("docs/research/2026-09-03-foo-bar-.md")
        assert not slug.startswith("-")
        assert not slug.endswith("-")


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
            await backend.build_workspace(
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

            # set_run_doc renames the session for the task - kill_session
            # needs the *current* name, not the one build_workspace's own
            # return value would have had before that rename.
            current = await backend.find_workspace(marker)
            await tmux_cli.kill_session(current.session)

        asyncio.run(_scenario())


class TestSessionRenaming:
    def test_set_run_doc_renames_the_session_to_a_task_slug(self, tmp_path):
        backend = TmuxBackend(persist=False)
        marker = _marker(tmp_path)

        async def _scenario():
            window = await backend.build_workspace(
                marker=marker, root=marker, template_name="native", template=_native_template()
            )
            prefix = backend._session_prefix(window.session)

            await backend.set_run_doc(
                marker=marker,
                doc="docs/research/2026-09-03-fix-kitchen-heat-link.md",
                started_at=1.0,
            )
            renamed = await backend.find_workspace(marker)
            assert renamed.session == f"{prefix}-fix-kitchen-heat-link"

            # A second, different task re-renames it - tracks the *current*
            # task, not just the first one.
            await backend.set_run_doc(marker=marker, doc="docs/research/2026-09-03-add-dark-mode.md", started_at=2.0)
            renamed_again = await backend.find_workspace(marker)
            assert renamed_again.session == f"{prefix}-add-dark-mode"

            await tmux_cli.kill_session(renamed_again.session)

        asyncio.run(_scenario())

    def test_pane_lookups_still_work_after_a_rename(self, tmp_path):
        # Renaming is cosmetic - every lookup matches on @cs_* pane tags,
        # never the session name, so find_role_pane/each_pane must keep
        # working against the renamed session exactly as before.
        backend = TmuxBackend(persist=False)
        marker = _marker(tmp_path)

        async def _scenario():
            await backend.build_workspace(
                marker=marker, root=marker, template_name="native", template=_native_template()
            )
            await backend.set_run_doc(marker=marker, doc="docs/x.md", started_at=1.0)

            pane = await backend.find_role_pane(marker=marker, role="planner")
            assert pane is not None

            pairs = [pair async for pair in backend.each_pane(marker=marker)]
            assert {role for role, _p in pairs} == {
                "principal", "implementer", "reviewer", "planner", "researcher"
            }

            window = await backend.find_workspace(marker)
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

    def test_list_all_workspaces_groups_by_session(self, tmp_path):
        backend = TmuxBackend(persist=False)
        marker_a = _marker(tmp_path)
        marker_b = _marker(tmp_path)

        async def _scenario():
            window_a = await backend.build_workspace(
                marker=marker_a,
                root=marker_a,
                template_name="native",
                template=_native_template(),
                lazy=True,
            )
            window_b = await backend.build_workspace(
                marker=marker_b,
                root=marker_b,
                template_name="native",
                template=_native_template(),
                lazy=True,
            )

            entries = await backend.list_all_workspaces()
            by_session = {e["session"]: e for e in entries}
            assert window_a.session in by_session
            assert window_b.session in by_session
            assert by_session[window_a.session]["workspace"] == marker_a
            assert by_session[window_a.session]["roles"] == ["researcher"]

            await tmux_cli.kill_session(window_a.session)
            await tmux_cli.kill_session(window_b.session)

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

    def test_send_role_prompt_pastes_a_large_prompt_instead_of_streaming_keys(
        self, tmp_path, monkeypatch
    ):
        # A conductor handoff can carry a multi-KB inline dispatch. It must
        # go out as one atomic paste - send-keys -l would deliver only the
        # trailing ~0.5 KB of it, so the pane would start mid-word.
        backend = TmuxBackend(persist=False)
        marker = _marker(tmp_path)
        big_prompt = "read " + "detail " * 500 + "and continue"

        pasted: list[tuple[str, str]] = []
        streamed: list[tuple[str, str]] = []
        real_paste = tmux_cli.send_text_paste
        real_keys = tmux_cli.send_keys_literal

        async def _spy_paste(target, text):
            pasted.append((target, text))
            await real_paste(target, text)

        async def _spy_keys(target, text):
            streamed.append((target, text))
            await real_keys(target, text)

        monkeypatch.setattr(tmux_cli, "send_text_paste", _spy_paste)
        monkeypatch.setattr(tmux_cli, "send_keys_literal", _spy_keys)

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
                "researcher", pane, text=big_prompt, submit=True
            )
            # The prompt was pasted in full - not truncated, not streamed as
            # keystrokes - and it submitted (fake claude clears on newline).
            assert (pane.pane_id, big_prompt) in pasted
            assert not any(text == big_prompt for _, text in streamed)
            captured = await tmux_cli.capture_pane(pane.pane_id)
            assert big_prompt not in captured
            assert "❯" in captured
            await tmux_cli.kill_session(window.session)

        asyncio.run(_scenario())

    def test_check_pane_stall_flags_a_dead_pane(self, tmp_path):
        backend = TmuxBackend(persist=False)
        marker = _marker(tmp_path)

        async def _scenario():
            # Only the researcher pane exits quickly - the other 4 stay up
            # on the default long-lived command. Giving every pane the
            # same short-lived command was flaky: build_workspace's own
            # tagging/prefill sequence touches all 5 panes in turn, and if
            # that whole sequence takes longer than the exit delay, an
            # *earlier* pane can already be gone by the time a later step
            # tries to touch it ("can't find pane") - unrelated to what
            # this test is actually checking.
            template = Template(
                layout="main_left_grid_right",
                panes=(
                    PaneConfig(role="principal", command="printf '\\xe2\\x9d\\xaf '"),
                    PaneConfig(role="implementer", command="printf '\\xe2\\x9d\\xaf '"),
                    PaneConfig(role="reviewer", command="printf '\\xe2\\x9d\\xaf '"),
                    PaneConfig(role="planner", command="printf '\\xe2\\x9d\\xaf '"),
                    PaneConfig(
                        role="researcher",
                        # Long enough that build_workspace's own prefill
                        # step (which polls for the ready marker at up to
                        # a 0.25s cadence, and has to get through tagging
                        # all 5 panes first) always sees the marker before
                        # this pane exits - a too-short delay here raced
                        # that poll and made the test flaky.
                        command="printf '\\xe2\\x9d\\xaf '; sleep 3; exit 0",
                    ),
                ),
            )
            window = await backend.build_workspace(
                marker=marker,
                root=marker,
                template_name="native",
                template=template,
            )
            pane = await backend.find_role_pane(marker=marker, role="researcher")
            assert pane is not None
            await asyncio.sleep(4.0)

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
