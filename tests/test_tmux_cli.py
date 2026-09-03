"""``backends/tmux_cli.py``: argv shape for each wrapper (mocked subprocess),
timeout/error classification, and a headless round-trip against a real,
isolated ``tmux`` server (Tests Required: "Headless tmux integration").
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid

import pytest

from claudespace.backends import tmux_cli
from claudespace.backends.base import BackendUnavailableError


@pytest.fixture(autouse=True)
def _isolated_tmux_server(monkeypatch, tmp_path):
    # A dedicated TMUX_TMPDIR gives this test module its own tmux server
    # socket directory, isolated from any tmux server already running on
    # the machine (the user's own session, or another test run). Uses a
    # short path directly under /tmp rather than pytest's tmp_path - tmux's
    # socket path has to fit in a unix socket address (~104 bytes on
    # macOS), and pytest's per-test nested tmp_path routinely blows that.
    socket_dir = tempfile.mkdtemp(prefix="cstmux-")
    monkeypatch.setenv("TMUX_TMPDIR", socket_dir)
    # Every call is now prefixed with -L claudespace (AD8) and, if a
    # private conf exists, -f <it> (Increment 2) - pin that to "absent" so
    # plain tmux_cli tests never depend on, or accidentally load, whatever
    # this machine's own ~/.local/share/claudespace/tmux/ happens to hold.
    # Dedicated persistence tests override this back to a real path.
    monkeypatch.setattr(
        "claudespace.backends.tmux_persist.CONF_PATH", tmp_path / "no-conf-here.conf"
    )
    yield
    shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.fixture
def session_name():
    return f"cs-test-{uuid.uuid4().hex[:8]}"


class FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr


class TestArgvShapes:
    """Mock the subprocess boundary to assert exact argv per wrapper."""

    @pytest.fixture
    def recorder(self, monkeypatch):
        calls = []

        async def _fake_exec(*args, **kwargs):
            calls.append(args)
            return FakeProcess(b"ok", b"", 0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        return calls

    SOCKET_PREFIX = ("tmux", "-L", "claudespace")

    def test_send_keys_literal_uses_dash_l_and_double_dash(self, recorder):
        asyncio.run(tmux_cli.send_keys_literal("%1", "-not-a-flag"))
        assert recorder[0] == self.SOCKET_PREFIX + (
            "send-keys", "-t", "%1", "-l", "--", "-not-a-flag"
        )

    def test_send_text_paste_routes_through_a_buffer_with_the_full_text(self, recorder):
        # The regression this guards: a handoff prompt must reach the pane
        # as one atomic paste, not a send-keys keystroke burst (which drops
        # the leading portion of a multi-KB prompt). The whole text goes
        # into a set-buffer verbatim, then paste-buffer -p (bracketed paste)
        # injects it and -d frees the buffer.
        text = "-read " + "x" * 4000 + " and continue"
        asyncio.run(tmux_cli.send_text_paste("%3", text))
        assert recorder[0] == self.SOCKET_PREFIX + (
            "set-buffer", "-b", "cs__3", "--", text
        )
        assert recorder[1] == self.SOCKET_PREFIX + (
            "paste-buffer", "-d", "-p", "-b", "cs__3", "-t", "%3"
        )
        # Never falls back to a keystroke send for any part of the prompt.
        assert not any("send-keys" in call for call in recorder)

    def test_send_text_paste_buffer_name_is_derived_per_target(self, recorder):
        # Concurrent handoffs to different panes must not clobber each
        # other's buffer, so the name is a function of the target pane id.
        asyncio.run(tmux_cli.send_text_paste("%12", "hi"))
        assert recorder[0][: len(self.SOCKET_PREFIX) + 3] == self.SOCKET_PREFIX + (
            "set-buffer", "-b", "cs__12"
        )

    def test_split_window_horizontal_flag_for_vertical_true(self, recorder):
        asyncio.run(tmux_cli.split_window("%1", vertical=True, session="s"))
        assert recorder[0][: len(self.SOCKET_PREFIX) + 1] == self.SOCKET_PREFIX + ("split-window",)
        assert "-h" in recorder[0]

    def test_split_window_vertical_flag_for_vertical_false(self, recorder):
        asyncio.run(tmux_cli.split_window("%1", vertical=False, session="s"))
        assert "-v" in recorder[0]

    def test_capture_pane_joins_wrapped_lines(self, recorder):
        asyncio.run(tmux_cli.capture_pane("%1"))
        assert recorder[0] == self.SOCKET_PREFIX + ("capture-pane", "-p", "-J", "-t", "%1")

    def test_set_pane_option_targets_pane_scope(self, recorder):
        asyncio.run(tmux_cli.set_pane_option("%1", "@cs_role", "researcher"))
        assert recorder[0] == self.SOCKET_PREFIX + (
            "set-option",
            "-p",
            "-t",
            "%1",
            "@cs_role",
            "researcher",
        )


class TestErrorClassification:
    def test_nonzero_exit_raises_tmux_command_error(self, monkeypatch):
        async def _fake_exec(*args, **kwargs):
            return FakeProcess(b"", b"no such session", 1)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        with pytest.raises(tmux_cli.TmuxCommandError, match="no such session"):
            asyncio.run(tmux_cli.run("has-session", "-t", "nope"))

    def test_timeout_raises_backend_unavailable(self, monkeypatch):
        class HangingProcess:
            def __init__(self):
                self.killed = False

            async def communicate(self):
                await asyncio.sleep(10)

            def kill(self):
                self.killed = True

            async def wait(self):
                return 0

        processes = []

        async def _fake_exec(*args, **kwargs):
            proc = HangingProcess()
            processes.append(proc)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        with pytest.raises(BackendUnavailableError):
            asyncio.run(tmux_cli.run("has-session", "-t", "nope", timeout=0.05))

        # The real bug this guards against: asyncio.wait_for's cancellation
        # does not kill the underlying process on its own - orphaning a
        # real `tmux` client that can go on to mutate state (e.g. actually
        # create the session) well after this call already reported
        # failure. run() must kill it itself.
        assert processes[0].killed is True


class TestParseVersion:
    def test_strips_trailing_letter_suffix(self):
        assert tmux_cli.parse_version("tmux 3.7c") == (3, 7)

    def test_plain_version(self):
        assert tmux_cli.parse_version("tmux 3.0") == (3, 0)


@pytest.mark.skipif(not tmux_cli.is_tmux_available(), reason="tmux not installed")
class TestHeadlessRoundTrip:
    """Real tmux, no terminal/display involved (AD3's whole payoff)."""

    def test_new_session_split_and_kill(self, session_name):
        async def _scenario():
            root_pane = await tmux_cli.new_session(session_name)
            assert root_pane.startswith("%")
            assert await tmux_cli.has_session(session_name)

            new_pane = await tmux_cli.split_window(root_pane, vertical=True, session=session_name)
            assert new_pane != root_pane

            rows = await tmux_cli.list_panes(session_name, ("pane_id",))
            assert {r["pane_id"] for r in rows} == {root_pane, new_pane}

            await tmux_cli.kill_session(session_name)
            assert not await tmux_cli.has_session(session_name)

        asyncio.run(_scenario())

    def test_pane_options_round_trip(self, session_name):
        async def _scenario():
            pane = await tmux_cli.new_session(session_name)
            await tmux_cli.set_pane_option(pane, "@cs_role", "researcher")
            value = await tmux_cli.show_pane_option(pane, "@cs_role")
            assert value == "researcher"
            await tmux_cli.kill_session(session_name)

        asyncio.run(_scenario())

    def test_send_keys_and_capture_pane_round_trip(self, session_name):
        async def _scenario():
            pane = await tmux_cli.new_session(session_name)
            await tmux_cli.send_keys_literal(pane, "echo hello-claudespace")
            await tmux_cli.send_enter(pane)
            await asyncio.sleep(0.3)
            captured = await tmux_cli.capture_pane(pane)
            assert "hello-claudespace" in captured
            await tmux_cli.kill_session(session_name)

        asyncio.run(_scenario())

    def test_paste_buffer_carries_a_large_prompt_without_truncation(self, session_name):
        # send-keys -l drops the leading ~2 KB of a ~2.5 KB prompt (only the
        # trailing ~0.5 KB survives the raw-mode input burst); the paste
        # buffer that send_text_paste uses instead must round-trip every
        # byte, front included, at that size class. show-buffer reads the
        # buffer back so this asserts the boundary itself is lossless,
        # independent of any live TUI.
        async def _scenario():
            await tmux_cli.new_session(session_name)
            big = "HEAD-" + "x" * 3000 + "-TAIL"
            await tmux_cli.run("set-buffer", "-b", "cs_big", "--", big)
            back = await tmux_cli.run("show-buffer", "-b", "cs_big")
            assert back == big
            assert back.startswith("HEAD-") and back.endswith("-TAIL")
            await tmux_cli.kill_session(session_name)

        asyncio.run(_scenario())

    def test_list_panes_all_finds_the_pane_across_the_server(self, session_name):
        async def _scenario():
            pane = await tmux_cli.new_session(session_name)
            await tmux_cli.set_pane_option(pane, "@cs_workspace", "/some/marker")
            rows = await tmux_cli.list_panes_all(("pane_id", "@cs_workspace"))
            matching = [r for r in rows if r.get("pane_id") == pane]
            assert matching and matching[0]["@cs_workspace"] == "/some/marker"
            await tmux_cli.kill_session(session_name)

        asyncio.run(_scenario())
