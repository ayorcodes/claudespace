"""``backends/cmux.py`` (``CmuxBackend``) integration: real cmux, gated like
the tmux headless suite (Tests Required)."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from claudespace.backends import cmux_cli
from claudespace.backends.cmux import (
    CmuxBackend,
    _derive_slug,
    _display_title,
    _parse_title,
    _pane_title,
    _read_state,
)
from claudespace.config import PaneConfig, Template

pytestmark = pytest.mark.skipif(not cmux_cli.is_cmux_available(), reason="cmux not installed")


def _record(monkeypatch, name: str) -> list[tuple[tuple, dict]]:
    """Wrap ``cmux_cli.<name>`` to record every call while still invoking
    the real implementation - lets a test assert exactly what a real,
    running cmux was asked to do."""
    calls: list[tuple[tuple, dict]] = []
    original = getattr(cmux_cli, name)

    async def _wrapped(*args, **kwargs):
        calls.append((args, kwargs))
        return await original(*args, **kwargs)

    monkeypatch.setattr(cmux_cli, name, _wrapped)
    return calls


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
        # workspace.py's --think toggle builds session_marker_dir(marker,
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


class TestSlugDerivation:
    def test_derives_basename_without_extension(self):
        assert _derive_slug("docs/research/2026-09-03-foo.md") == "2026-09-03-foo"

    def test_degenerate_doc_yields_empty_string(self):
        assert _derive_slug("docs/research/") == ""
        assert _derive_slug("") == ""

    def test_display_title_falls_back_to_instance8_when_no_slug(self):
        instance = "abcd1234-5678-90ab-cdef-1234567890ab"
        assert _display_title({}, instance, "researcher") == "abcd1234 · researcher"

    def test_display_title_uses_slug_when_present(self):
        instance = "abcd1234-5678-90ab-cdef-1234567890ab"
        assert (
            _display_title({"slug": "my-feature"}, instance, "researcher")
            == "my-feature · researcher"
        )


class TestSlugAndLabeling:
    def test_launch_pane_fallback_label_is_instance8_and_records_surface(self, tmp_path, monkeypatch):
        calls = _record(monkeypatch, "rename_tab")
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
            titles = [kwargs["title"] for _args, kwargs in calls]
            assert titles == [f"{window.instance[:8]} · researcher"]
            state = _read_state(window.instance)
            assert state["surfaces"]["researcher"] is not None
            await _teardown(window)

        asyncio.run(_scenario())

    def test_set_run_doc_first_call_captures_slug_and_relabels(self, tmp_path, monkeypatch):
        workspace_calls = _record(monkeypatch, "rename_workspace")
        tab_calls = _record(monkeypatch, "rename_tab")
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
            tab_calls.clear()

            await backend.set_run_doc(
                marker=marker,
                instance=window.instance,
                doc="docs/research/2026-09-03-foo.md",
                started_at=1.0,
            )

            assert workspace_calls[-1][1]["title"] == "2026-09-03-foo"
            assert tab_calls[-1][1]["title"] == "2026-09-03-foo · researcher"
            assert _read_state(window.instance)["slug"] == "2026-09-03-foo"
            await _teardown(window)

        asyncio.run(_scenario())

    def test_set_run_doc_second_call_leaves_slug_and_labels_unchanged(self, tmp_path, monkeypatch):
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
            workspace_calls = _record(monkeypatch, "rename_workspace")
            await backend.set_run_doc(
                marker=marker, instance=window.instance, doc="docs/first.md", started_at=1.0
            )
            assert _read_state(window.instance)["slug"] == "first"
            assert len(workspace_calls) == 1

            await backend.set_run_doc(
                marker=marker, instance=window.instance, doc="docs/second.md", started_at=2.0
            )
            assert _read_state(window.instance)["slug"] == "first"
            assert len(workspace_calls) == 1  # no second relabel (FR4/AC3)
            await _teardown(window)

        asyncio.run(_scenario())

    def test_late_lazy_reveal_is_born_with_the_slug_label(self, tmp_path, monkeypatch):
        tab_calls = _record(monkeypatch, "rename_tab")
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
            await backend.set_run_doc(
                marker=marker, instance=window.instance, doc="docs/x.md", started_at=1.0
            )
            tab_calls.clear()

            source = await backend.find_role_pane(
                marker=marker, role="researcher", instance=window.instance
            )
            assert source is not None
            await backend.reveal_role(
                marker=marker,
                instance=window.instance,
                root=marker,
                template=_native_template(),
                role="planner",
                source=source,
            )

            assert tab_calls[-1][1]["title"] == "x · planner"
            await _teardown(window)

        asyncio.run(_scenario())


class TestMigrationFallback:
    def test_pre_upgrade_session_routes_via_title_scan_and_is_never_relabeled(
        self, tmp_path, monkeypatch
    ):
        # D2: a session built before this feature has no state file at all
        # for its instance, and its one identity-bearing surface still
        # carries the old cs:<uuid>:<role> title.
        marker = _marker(tmp_path)
        instance = str(uuid.uuid4())

        async def _scenario():
            workspace_ref = await cmux_cli.workspace_create(marker)
            try:
                workspace_id = next(
                    ws["id"]
                    for ws in await cmux_cli.workspace_list()
                    if ws["ref"] == workspace_ref
                )
                root_surface_ref = (await cmux_cli.surface_list(workspace_id))[0]["ref"]
                await cmux_cli.rename_tab(
                    workspace_ref=workspace_ref,
                    surface_ref=root_surface_ref,
                    title=_pane_title(instance, "researcher"),
                )

                backend = CmuxBackend()
                found = await backend.find_workspace(marker)
                assert found is not None
                assert found.instance == instance

                pane = await backend.find_role_pane(
                    marker=marker, role="researcher", instance=instance
                )
                assert pane is not None
                assert pane.surface_ref == root_surface_ref

                workspace_calls = _record(monkeypatch, "rename_workspace")
                tab_calls = _record(monkeypatch, "rename_tab")
                await backend.set_run_doc(
                    marker=marker, instance=instance, doc="docs/x.md", started_at=1.0
                )
                assert workspace_calls == []
                assert tab_calls == []  # never relabeled (D2/AC9)
            finally:
                await cmux_cli.workspace_close(workspace_ref)

        asyncio.run(_scenario())


class TestNotify:
    def test_notify_targets_the_resolved_workspace(self, tmp_path, monkeypatch):
        calls = _record(monkeypatch, "notify")
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
            await backend.notify(
                title="claudespace: researcher done",
                message="see docs/x.md",
                instance=window.instance,
            )
            assert len(calls) == 1
            _args, kwargs = calls[0]
            assert kwargs["title"] == "claudespace: researcher done"
            assert kwargs["body"] == "see docs/x.md"
            assert kwargs["workspace_ref"] == window.workspace_ref
            await _teardown(window)

        asyncio.run(_scenario())
