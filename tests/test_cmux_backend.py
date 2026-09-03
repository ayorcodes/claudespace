"""``backends/cmux.py`` (``CmuxBackend``) integration: real cmux, gated like
the tmux headless suite (Tests Required)."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from claudespace.backends import cmux_cli
from claudespace.backends.cmux import CmuxBackend, _parse_title, _pane_title
from claudespace.config import PaneConfig, Template

pytestmark = pytest.mark.skipif(not cmux_cli.is_cmux_available(), reason="cmux not installed")


@pytest.fixture(autouse=True)
def _no_persona_baking(tmp_path, monkeypatch):
    monkeypatch.setattr("claudespace.backends.common.PROMPTS_DEST", tmp_path / "prompts")


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
    root = tmp_path / f"root-{uuid.uuid4().hex[:8]}"
    root.mkdir()
    return str(root)


async def _teardown(window):
    await cmux_cli.workspace_close(window.workspace_ref)


class TestTitleHelpers:
    def test_pane_title_carries_the_full_instance_uuid(self):
        # Not truncated to 8 hex chars (unlike tmux's cosmetic session-name
        # suffix): Window.instance must round-trip in full, since
        # workspace.py's --think toggle and this backend's own
        # workspace-state.json path both build session_marker_dir(marker,
        # instance) from it - a truncated id would point at a directory no
        # pane's env actually uses.
        instance = "abcd1234-5678-90ab-cdef-1234567890ab"
        assert _pane_title(instance, "researcher") == f"cs:{instance}:researcher"

    def test_parse_title_round_trips(self):
        instance = "abcd1234-5678-90ab-cdef-1234567890ab"
        assert _parse_title(f"cs:{instance}:researcher") == (instance, "researcher")

    def test_parse_title_rejects_a_user_renamed_tab(self):
        assert _parse_title("my custom tab") is None
        assert _parse_title("cs:abcd1234:researcher") is None  # truncated, not a full UUID
        assert _parse_title("cs:abcd1234-5678-90ab-cdef-1234567890ab:") is None


class TestBuildWorkspace:
    def test_eager_build_launches_every_pane(self, tmp_path):
        backend = CmuxBackend()
        marker = _marker(tmp_path)

        async def _scenario():
            window = await backend.build_workspace(
                marker=marker, root=marker, template_name="native", template=_native_template()
            )
            pairs = [pair async for pair in backend.each_pane(marker=marker, instance=window.instance)]
            roles = {role for role, _pane in pairs}
            assert roles == {"principal", "implementer", "reviewer", "planner", "researcher"}
            await _teardown(window)

        asyncio.run(_scenario())

    def test_lazy_build_launches_only_entry_role(self, tmp_path):
        backend = CmuxBackend()
        marker = _marker(tmp_path)

        async def _scenario():
            window = await backend.build_workspace(
                marker=marker,
                root=marker,
                template_name="native",
                template=_native_template(),
                lazy=True,
            )
            pairs = [pair async for pair in backend.each_pane(marker=marker, instance=window.instance)]
            assert [role for role, _pane in pairs] == ["researcher"]
            assert await backend.get_lazy(marker=marker, instance=window.instance) is True
            await _teardown(window)

        asyncio.run(_scenario())


class TestStateRoundTrip:
    def test_auto_handoff_lazy_and_template_persist(self, tmp_path):
        backend = CmuxBackend()
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
            assert await backend.get_auto_handoff(marker=marker, instance=window.instance) is False
            assert await backend.get_lazy(marker=marker, instance=window.instance) is True
            assert await backend.get_template_name(marker=marker, instance=window.instance) == "native"
            await _teardown(window)

        asyncio.run(_scenario())

    def test_run_doc_round_trips(self, tmp_path):
        backend = CmuxBackend()
        marker = _marker(tmp_path)

        async def _scenario():
            window = await backend.build_workspace(
                marker=marker, root=marker, template_name="native", template=_native_template()
            )
            await backend.set_run_doc(
                marker=marker, instance=window.instance, doc="docs/x.md", started_at=123.0
            )
            doc, started = await backend.get_run_doc(marker=marker, instance=window.instance)
            assert doc == "docs/x.md"
            assert started == 123.0
            await _teardown(window)

        asyncio.run(_scenario())

    def test_instance_less_reads_resolve_via_find_workspace(self, tmp_path):
        # workspace.py's attach-or-build probe calls the getters with no
        # instance at all - D3's "Instance-less reads" edge case.
        backend = CmuxBackend()
        marker = _marker(tmp_path)

        async def _scenario():
            window = await backend.build_workspace(
                marker=marker, root=marker, template_name="native", template=_native_template(),
            )
            assert await backend.get_template_name(marker=marker) == "native"
            await _teardown(window)

        asyncio.run(_scenario())

    def test_instance_less_read_with_no_workspace_degrades_to_absent(self, tmp_path):
        backend = CmuxBackend()
        marker = str(tmp_path / "never-built")
        assert asyncio.run(backend.get_auto_handoff(marker=marker)) is False
        assert asyncio.run(backend.get_template_name(marker=marker)) is None
        assert asyncio.run(backend.get_run_doc(marker=marker)) == (None, None)


class TestFindAndReveal:
    def test_find_workspace_and_find_role_pane(self, tmp_path):
        backend = CmuxBackend()
        marker = _marker(tmp_path)

        async def _scenario():
            window = await backend.build_workspace(
                marker=marker, root=marker, template_name="native", template=_native_template()
            )
            found = await backend.find_workspace(marker)
            assert found is not None
            assert found.workspace_ref == window.workspace_ref
            assert found.instance == window.instance

            pane = await backend.find_role_pane(marker=marker, role="planner", instance=window.instance)
            assert pane is not None

            assert (
                await backend.find_role_pane(marker=marker, role="conductor", instance=window.instance)
                is None
            )
            await _teardown(window)

        asyncio.run(_scenario())

    def test_two_windows_on_one_root_are_distinguished_by_instance(self, tmp_path):
        backend = CmuxBackend()
        marker = _marker(tmp_path)

        async def _scenario():
            window_a = await backend.build_workspace(
                marker=marker, root=marker, template_name="native", template=_native_template()
            )
            window_b = await backend.build_workspace(
                marker=marker, root=marker, template_name="native", template=_native_template()
            )
            assert window_a.instance != window_b.instance

            pane_a = await backend.find_role_pane(
                marker=marker, role="researcher", instance=window_a.instance
            )
            pane_b = await backend.find_role_pane(
                marker=marker, role="researcher", instance=window_b.instance
            )
            assert pane_a is not None and pane_b is not None
            assert pane_a.workspace_ref != pane_b.workspace_ref

            await _teardown(window_a)
            await _teardown(window_b)

        asyncio.run(_scenario())

    def test_reveal_role_splits_and_launches(self, tmp_path):
        backend = CmuxBackend()
        marker = _marker(tmp_path)

        async def _scenario():
            window = await backend.build_workspace(
                marker=marker,
                root=marker,
                template_name="native",
                template=_native_template(),
                lazy=True,
            )
            source = await backend.find_role_pane(
                marker=marker, role="researcher", instance=window.instance
            )
            assert source is not None

            revealed = await backend.reveal_role(
                marker=marker,
                instance=window.instance,
                root=marker,
                template=_native_template(),
                role="planner",
                source=source,
            )
            assert revealed is not None
            assert revealed.surface_ref != source.surface_ref

            found = await backend.find_role_pane(
                marker=marker, role="planner", instance=window.instance
            )
            assert found is not None
            assert found.surface_ref == revealed.surface_ref
            await _teardown(window)

        asyncio.run(_scenario())


class TestPromptDeliveryAndStall:
    def test_send_role_prompt_delivers_and_confirms_submission(self, tmp_path):
        backend = CmuxBackend()
        marker = _marker(tmp_path)

        async def _scenario():
            window = await backend.build_workspace(
                marker=marker, root=marker, template_name="native", template=_native_template(FAKE_CLAUDE)
            )
            pane = await backend.find_role_pane(
                marker=marker, role="researcher", instance=window.instance
            )
            assert pane is not None

            await backend.send_role_prompt("researcher", pane, text="hello from a handoff", submit=True)
            captured = await cmux_cli.capture_pane(
                workspace_ref=pane.workspace_ref, surface_ref=pane.surface_ref
            )
            assert "hello from a handoff" not in captured
            assert "❯" in captured
            await _teardown(window)

        asyncio.run(_scenario())

    def test_send_role_prompt_sends_a_large_prompt_without_truncation(self, tmp_path):
        # A10: cmux's `send` is a single atomic write - no chunking path
        # to regress, unlike tmux's send-keys keystroke burst.
        backend = CmuxBackend()
        marker = _marker(tmp_path)
        big_prompt = "START-" + "x" * 3000 + "-END"

        async def _scenario():
            window = await backend.build_workspace(
                marker=marker, root=marker, template_name="native", template=_native_template(FAKE_CLAUDE)
            )
            pane = await backend.find_role_pane(
                marker=marker, role="researcher", instance=window.instance
            )
            assert pane is not None

            await backend.send_role_prompt("researcher", pane, text=big_prompt, submit=True)
            captured = await cmux_cli.capture_pane(
                workspace_ref=pane.workspace_ref, surface_ref=pane.surface_ref, lines=2000
            )
            assert big_prompt not in captured
            assert "❯" in captured
            await _teardown(window)

        asyncio.run(_scenario())

    def test_check_pane_stall_never_flags_a_changing_screen(self, tmp_path):
        backend = CmuxBackend()
        marker = _marker(tmp_path)

        async def _scenario():
            window = await backend.build_workspace(
                marker=marker, root=marker, template_name="native", template=_native_template(FAKE_CLAUDE)
            )
            pane = await backend.find_role_pane(
                marker=marker, role="researcher", instance=window.instance
            )
            assert pane is not None

            state1, stalled1 = await backend.check_pane_stall(
                pane, role="researcher", previous=None, now=1.0, stall_after_seconds=600
            )
            assert stalled1 is False
            state2, stalled2 = await backend.check_pane_stall(
                pane, role="researcher", previous=state1, now=10000.0, stall_after_seconds=600
            )
            assert stalled2 is False
            await _teardown(window)

        asyncio.run(_scenario())
