"""``backends/cmux_cli.py``: argv shape for each wrapper (mocked subprocess)
and timeout/error classification, mirroring ``test_tmux_cli.py``."""

from __future__ import annotations

import asyncio
import json

import pytest

from claudespace.backends import cmux_cli
from claudespace.backends.base import BackendUnavailableError


class FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr


class TestArgvShapes:
    @pytest.fixture
    def recorder(self, monkeypatch):
        calls = []

        async def _fake_exec(*args, **kwargs):
            calls.append(args)
            return FakeProcess(b"OK surface:2 workspace:1", b"", 0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        return calls

    def test_ping(self, recorder):
        asyncio.run(cmux_cli.run("ping"))
        assert recorder[0] == ("cmux", "ping")

    def test_workspace_create_uses_cwd_and_no_focus_steal(self, monkeypatch):
        calls = []

        async def _fake_exec(*args, **kwargs):
            calls.append(args)
            return FakeProcess(b"OK workspace:6", b"", 0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        ref = asyncio.run(cmux_cli.workspace_create("/tmp/some-root"))
        assert calls[0] == (
            "cmux", "workspace", "create", "--cwd", "/tmp/some-root", "--focus", "false",
        )
        assert ref == "workspace:6"

    def test_new_split_targets_the_source_surface(self, monkeypatch):
        calls = []

        async def _fake_exec(*args, **kwargs):
            calls.append(args)
            return FakeProcess(b"OK surface:12 workspace:6", b"", 0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        ref = asyncio.run(
            cmux_cli.new_split("right", workspace_ref="workspace:6", surface_ref="surface:11")
        )
        assert calls[0] == (
            "cmux", "new-split", "right", "--workspace", "workspace:6", "--surface", "surface:11",
        )
        assert ref == "surface:12"

    def test_send_text_guards_a_leading_dash_with_double_dash(self, recorder):
        asyncio.run(
            cmux_cli.send_text(
                workspace_ref="workspace:1", surface_ref="surface:2", text="-not-a-flag"
            )
        )
        assert recorder[0] == (
            "cmux", "send", "--workspace", "workspace:1", "--surface", "surface:2", "--", "-not-a-flag",
        )

    def test_rename_tab_guards_a_leading_dash_with_double_dash(self, recorder):
        asyncio.run(
            cmux_cli.rename_tab(
                workspace_ref="workspace:1", surface_ref="surface:2", title="cs:abcd1234:researcher"
            )
        )
        assert recorder[0] == (
            "cmux", "rename-tab", "--workspace", "workspace:1", "--surface", "surface:2",
            "--", "cs:abcd1234:researcher",
        )

    def test_send_key_enter(self, recorder):
        asyncio.run(
            cmux_cli.send_key(workspace_ref="workspace:1", surface_ref="surface:2", key="enter")
        )
        assert recorder[0] == (
            "cmux", "send-key", "--workspace", "workspace:1", "--surface", "surface:2", "enter",
        )

    def test_capture_pane(self, recorder):
        asyncio.run(
            cmux_cli.capture_pane(workspace_ref="workspace:1", surface_ref="surface:2", lines=20)
        )
        assert recorder[0] == (
            "cmux", "capture-pane", "--workspace", "workspace:1", "--surface", "surface:2",
            "--lines", "20",
        )

    def test_rename_workspace_guards_a_leading_dash_with_double_dash(self, recorder):
        asyncio.run(
            cmux_cli.rename_workspace(workspace_ref="workspace:1", title="-not-a-flag")
        )
        assert recorder[0] == (
            "cmux", "rename-workspace", "--workspace", "workspace:1", "--", "-not-a-flag",
        )

    def test_notify_builds_expected_argv(self, recorder):
        asyncio.run(
            cmux_cli.notify(title="claudespace: implementer done", body="see docs/x.md")
        )
        assert recorder[0] == (
            "cmux", "notify", "--title", "claudespace: implementer done",
            "--body", "see docs/x.md",
        )

    def test_notify_targets_a_workspace_when_given(self, recorder):
        asyncio.run(
            cmux_cli.notify(title="t", body="b", workspace_ref="workspace:6")
        )
        assert recorder[0] == (
            "cmux", "notify", "--title", "t", "--body", "b", "--workspace", "workspace:6",
        )

    def test_notify_omits_body_flag_when_empty(self, recorder):
        asyncio.run(cmux_cli.notify(title="t", body=""))
        assert recorder[0] == ("cmux", "notify", "--title", "t")

    def test_workspace_close_is_the_canonical_verb(self, recorder):
        asyncio.run(cmux_cli.workspace_close("workspace:6"))
        assert recorder[0] == ("cmux", "workspace", "close", "workspace:6")

    def test_capabilities_parses_json(self, monkeypatch):
        async def _fake_exec(*args, **kwargs):
            return FakeProcess(json.dumps({"access_mode": "automation"}).encode(), b"", 0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        assert asyncio.run(cmux_cli.capabilities()) == {"access_mode": "automation"}

    def test_workspace_list_parses_json(self, monkeypatch):
        payload = {"workspaces": [{"ref": "workspace:1", "id": "abc"}]}

        async def _fake_exec(*args, **kwargs):
            return FakeProcess(json.dumps(payload).encode(), b"", 0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        assert asyncio.run(cmux_cli.workspace_list()) == payload["workspaces"]

    def test_surface_list_sends_workspace_id_as_json_rpc_param(self, monkeypatch):
        calls = []
        payload = {"surfaces": [{"ref": "surface:1"}]}

        async def _fake_exec(*args, **kwargs):
            calls.append(args)
            return FakeProcess(json.dumps(payload).encode(), b"", 0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        result = asyncio.run(cmux_cli.surface_list("F0F251DA-5FE6-43F3-8FF9-4A9336404852"))
        assert result == payload["surfaces"]
        assert calls[0][:2] == ("cmux", "rpc")
        assert calls[0][2] == "surface.list"
        assert json.loads(calls[0][3]) == {"workspace_id": "F0F251DA-5FE6-43F3-8FF9-4A9336404852"}


class TestErrorClassification:
    def test_nonzero_exit_raises_cmux_command_error(self, monkeypatch):
        async def _fake_exec(*args, **kwargs):
            return FakeProcess(b"", b"Error: not_found: Surface not found", 1)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        with pytest.raises(cmux_cli.CmuxCommandError, match="Surface not found"):
            asyncio.run(cmux_cli.run("capture-pane"))

    def test_timeout_raises_backend_unavailable_and_kills_the_process(self, monkeypatch):
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
            asyncio.run(cmux_cli.run("ping", timeout=0.05))
        assert processes[0].killed is True

    def test_workspace_list_degrades_to_empty_on_command_error(self, monkeypatch):
        async def _fake_exec(*args, **kwargs):
            return FakeProcess(b"", b"boom", 1)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        assert asyncio.run(cmux_cli.workspace_list()) == []

    def test_surface_list_degrades_to_empty_on_command_error(self, monkeypatch):
        async def _fake_exec(*args, **kwargs):
            return FakeProcess(b"", b"boom", 1)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        assert asyncio.run(cmux_cli.surface_list("some-id")) == []

    def test_capture_pane_degrades_to_empty_string_on_command_error(self, monkeypatch):
        async def _fake_exec(*args, **kwargs):
            return FakeProcess(b"", b"boom", 1)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        assert asyncio.run(
            cmux_cli.capture_pane(workspace_ref="workspace:1", surface_ref="surface:2")
        ) == ""

    def test_rename_tab_is_best_effort_and_never_raises(self, monkeypatch):
        async def _fake_exec(*args, **kwargs):
            return FakeProcess(b"", b"boom", 1)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        asyncio.run(
            cmux_cli.rename_tab(workspace_ref="workspace:1", surface_ref="surface:2", title="x")
        )

    def test_workspace_close_is_best_effort_and_never_raises(self, monkeypatch):
        async def _fake_exec(*args, **kwargs):
            return FakeProcess(b"", b"boom", 1)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        asyncio.run(cmux_cli.workspace_close("workspace:1"))

    def test_rename_workspace_is_best_effort_and_never_raises(self, monkeypatch):
        async def _fake_exec(*args, **kwargs):
            return FakeProcess(b"", b"boom", 1)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        asyncio.run(cmux_cli.rename_workspace(workspace_ref="workspace:1", title="x"))

    def test_notify_is_best_effort_and_never_raises(self, monkeypatch):
        async def _fake_exec(*args, **kwargs):
            return FakeProcess(b"", b"boom", 1)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        asyncio.run(cmux_cli.notify(title="t", body="b"))


@pytest.mark.skipif(not cmux_cli.is_cmux_available(), reason="cmux not installed")
class TestHeadlessRoundTrip:
    """Real cmux, exercising the socket for real (Tests Required:
    integration, gated like the tmux headless suite)."""

    def test_ping_pong(self):
        assert asyncio.run(cmux_cli.ping()) == "PONG"

    def test_workspace_create_split_send_capture_and_close(self, tmp_path):
        async def _scenario():
            root = str(tmp_path)
            workspace_ref = await cmux_cli.workspace_create(root)
            try:
                workspaces = await cmux_cli.workspace_list()
                match = next(w for w in workspaces if w["ref"] == workspace_ref)
                surfaces = await cmux_cli.surface_list(match["id"])
                assert len(surfaces) == 1
                root_surface = surfaces[0]["ref"]

                new_surface = await cmux_cli.new_split(
                    "right", workspace_ref=workspace_ref, surface_ref=root_surface
                )
                assert new_surface != root_surface

                await cmux_cli.rename_tab(
                    workspace_ref=workspace_ref, surface_ref=new_surface, title="cs:deadbeef:researcher"
                )
                surfaces = await cmux_cli.surface_list(match["id"])
                by_ref = {s["ref"]: s for s in surfaces}
                assert by_ref[new_surface]["title"] == "cs:deadbeef:researcher"

                await cmux_cli.send_text(
                    workspace_ref=workspace_ref, surface_ref=new_surface, text="echo cmux-cli-test-marker"
                )
                await cmux_cli.send_key(workspace_ref=workspace_ref, surface_ref=new_surface, key="enter")
                await asyncio.sleep(0.5)
                captured = await cmux_cli.capture_pane(
                    workspace_ref=workspace_ref, surface_ref=new_surface, lines=20
                )
                assert "cmux-cli-test-marker" in captured
            finally:
                await cmux_cli.workspace_close(workspace_ref)

        asyncio.run(_scenario())
