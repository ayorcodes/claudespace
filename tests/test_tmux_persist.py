"""``backends/tmux_persist.py`` (Increment 2): conf rendering, and the
dump/rehydrate sidecar round-trip against a real, isolated tmux server -
Tests Required's "sidecar dump/rehydrate round-trip against a synthetic
resurrect save."
"""

from __future__ import annotations

import json
import shutil
import tempfile

import pytest

from claudespace.backends import tmux_cli, tmux_persist


@pytest.fixture(autouse=True)
def _isolated_paths(monkeypatch, tmp_path):
    socket_dir = tempfile.mkdtemp(prefix="cstmux-")
    monkeypatch.setenv("TMUX_TMPDIR", socket_dir)
    monkeypatch.setattr(tmux_persist, "DATA_HOME", tmp_path / "claudespace")
    monkeypatch.setattr(tmux_persist, "PLUGINS_DIR", tmp_path / "claudespace" / "tmux-plugins")
    monkeypatch.setattr(tmux_persist, "TMUX_DATA_DIR", tmp_path / "claudespace" / "tmux")
    monkeypatch.setattr(tmux_persist, "CONF_PATH", tmp_path / "claudespace" / "tmux" / "claudespace.tmux.conf")
    monkeypatch.setattr(tmux_persist, "TAGS_DIR", tmp_path / "claudespace" / "tmux" / "tags")
    monkeypatch.setattr(
        tmux_persist, "LAST_TAGS_PATH", tmp_path / "claudespace" / "tmux" / "tags" / "last.json"
    )
    monkeypatch.setattr(
        tmux_persist, "REHYDRATED_MARKER", tmp_path / "claudespace" / "tmux" / "rehydrated-at"
    )
    yield
    shutil.rmtree(socket_dir, ignore_errors=True)


class TestRenderConf:
    def test_persist_false_yields_none(self):
        assert tmux_persist.render_conf(persist=False, interval_minutes=15) is None

    def test_persist_true_includes_key_options(self):
        content = tmux_persist.render_conf(persist=True, interval_minutes=15)
        assert '@resurrect-processes' in content
        assert '"~claude"' in content
        assert "@continuum-save-interval '15'" in content
        assert "resurrect.tmux'" in content
        assert "continuum.tmux'" in content
        assert "dump" in content
        assert "rehydrate" in content


class TestWriteConf:
    def test_persist_false_removes_any_existing_conf(self):
        tmux_persist.TMUX_DATA_DIR.mkdir(parents=True)
        tmux_persist.CONF_PATH.write_text("stale")
        tmux_persist.write_conf(persist=False, interval_minutes=15)
        assert not tmux_persist.CONF_PATH.exists()

    def test_persist_true_writes_the_file(self):
        tmux_persist.write_conf(persist=True, interval_minutes=15)
        assert tmux_persist.CONF_PATH.is_file()
        assert "@continuum-restore" in tmux_persist.CONF_PATH.read_text()

    def test_is_idempotent_when_unchanged(self):
        tmux_persist.write_conf(persist=True, interval_minutes=15)
        first_mtime = tmux_persist.CONF_PATH.stat().st_mtime_ns
        tmux_persist.write_conf(persist=True, interval_minutes=15)
        assert tmux_persist.CONF_PATH.stat().st_mtime_ns == first_mtime

    def test_rewrites_when_interval_changes(self):
        tmux_persist.write_conf(persist=True, interval_minutes=15)
        tmux_persist.write_conf(persist=True, interval_minutes=30)
        assert "'30'" in tmux_persist.CONF_PATH.read_text()


class TestPluginsPresent:
    def test_false_when_missing(self):
        assert tmux_persist.plugins_present() is False

    def test_true_when_entrypoints_exist(self):
        (tmux_persist.PLUGINS_DIR / "resurrect").mkdir(parents=True)
        (tmux_persist.PLUGINS_DIR / "resurrect" / "resurrect.tmux").write_text("#!/bin/sh\n")
        (tmux_persist.PLUGINS_DIR / "continuum").mkdir(parents=True)
        (tmux_persist.PLUGINS_DIR / "continuum" / "continuum.tmux").write_text("#!/bin/sh\n")
        assert tmux_persist.plugins_present() is True


class TestMarkerMtime:
    def test_none_when_never_touched(self):
        assert tmux_persist.marker_mtime() is None

    def test_returns_a_float_after_touch(self):
        tmux_persist._touch_marker()
        assert isinstance(tmux_persist.marker_mtime(), float)


@pytest.mark.skipif(not tmux_cli.is_tmux_available(), reason="tmux not installed")
class TestDumpRehydrateRoundTrip:
    """A synthetic resurrect save: build real panes, tag them, dump, wipe
    the tags (simulating a restore that dropped them), rehydrate, and
    confirm every tag comes back - matched by the same positional
    coordinates resurrect itself restores at.
    """

    def test_round_trip_restores_tags_by_position(self):
        import asyncio

        async def _scenario():
            session = "cs-persist-test"
            pane0 = await tmux_cli.new_session(session)
            pane1 = await tmux_cli.split_window(pane0, vertical=True, session=session)

            await tmux_cli.set_pane_option(pane0, "@cs_workspace", "/some/root")
            await tmux_cli.set_pane_option(pane0, "@cs_role", "researcher")
            await tmux_cli.set_pane_option(pane1, "@cs_workspace", "/some/root")
            await tmux_cli.set_pane_option(pane1, "@cs_role", "planner")

            tmux_persist.dump()
            saved = json.loads(tmux_persist.LAST_TAGS_PATH.read_text())
            assert len(saved["panes"]) == 2

            # Simulate a restore that recreated the same positions but
            # dropped the @cs_* tags (resurrect doesn't save them - the
            # whole reason this module exists).
            await tmux_cli.set_pane_option(pane0, "@cs_workspace", "")
            await tmux_cli.set_pane_option(pane0, "@cs_role", "")
            await tmux_cli.set_pane_option(pane1, "@cs_workspace", "")
            await tmux_cli.set_pane_option(pane1, "@cs_role", "")

            tmux_persist.rehydrate()

            assert await tmux_cli.show_pane_option(pane0, "@cs_role") == "researcher"
            assert await tmux_cli.show_pane_option(pane1, "@cs_role") == "planner"
            assert tmux_persist.marker_mtime() is not None

            await tmux_cli.kill_session(session)

        asyncio.run(_scenario())

    def test_rehydrate_with_no_sidecar_touches_marker_without_crashing(self):
        tmux_persist.rehydrate()
        assert tmux_persist.marker_mtime() is not None

    def test_dump_with_no_claudespace_panes_writes_empty_list(self):
        import asyncio

        async def _scenario():
            session = "cs-persist-empty"
            await tmux_cli.new_session(session)  # untagged - not a claudespace pane
            tmux_persist.dump()
            saved = json.loads(tmux_persist.LAST_TAGS_PATH.read_text())
            assert saved["panes"] == []
            await tmux_cli.kill_session(session)

        asyncio.run(_scenario())
