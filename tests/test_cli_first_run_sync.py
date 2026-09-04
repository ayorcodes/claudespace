"""``cli.main()``'s guarded first-run asset sync (AD5).

Moved out of the installer/postinstall (which may run as root, see D4) and
into the first real ``claudespace`` invocation, gated by a per-version
sentinel so it costs nothing on every subsequent launch. ``uninstall`` must
never trigger it - re-syncing hooks that command is about to remove would
strand a stale sentinel and permanently skip re-installing them later.
"""

from __future__ import annotations

import sys

import pytest

from claudespace import assets_sync, cli


def _patch_main_dependencies(monkeypatch, *, ok: bool = True):
    monkeypatch.setattr(cli.environment, "require_macos", lambda: None)
    monkeypatch.setattr(cli.utils, "is_iterm_running", lambda: False)
    monkeypatch.setattr(
        cli.environment,
        "run_doctor_checks",
        lambda *, iterm_was_running, assume_yes, launch: ok,
    )
    monkeypatch.setattr(cli, "_check_tmux_persistence", lambda: None)
    monkeypatch.setattr(assets_sync, "uninstall", lambda: None)


class TestSyncIfNeeded:
    def test_runs_sync_when_no_sentinel_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(assets_sync, "SYNC_SENTINEL_DIR", tmp_path)
        calls = []
        monkeypatch.setattr(assets_sync, "sync_assets", lambda: calls.append(True))
        assert assets_sync.sync_if_needed(version="1.2.3") is True
        assert calls == [True]

    def test_writes_the_sentinel_after_a_successful_sync(self, tmp_path, monkeypatch):
        monkeypatch.setattr(assets_sync, "SYNC_SENTINEL_DIR", tmp_path)
        monkeypatch.setattr(assets_sync, "sync_assets", lambda: None)
        assets_sync.sync_if_needed(version="1.2.3")
        assert (tmp_path / ".asset-sync-1.2.3").exists()

    def test_skips_sync_when_the_sentinel_already_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(assets_sync, "SYNC_SENTINEL_DIR", tmp_path)
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / ".asset-sync-1.2.3").write_text("")
        calls = []
        monkeypatch.setattr(assets_sync, "sync_assets", lambda: calls.append(True))
        assert assets_sync.sync_if_needed(version="1.2.3") is False
        assert calls == []

    def test_a_failed_sync_does_not_write_the_sentinel(self, tmp_path, monkeypatch):
        monkeypatch.setattr(assets_sync, "SYNC_SENTINEL_DIR", tmp_path)

        def _boom():
            raise RuntimeError("settings.json is not valid JSON")

        monkeypatch.setattr(assets_sync, "sync_assets", _boom)
        with pytest.raises(RuntimeError):
            assets_sync.sync_if_needed(version="1.2.3")
        assert not (tmp_path / ".asset-sync-1.2.3").exists()

    def test_a_version_bump_re_triggers_sync(self, tmp_path, monkeypatch):
        monkeypatch.setattr(assets_sync, "SYNC_SENTINEL_DIR", tmp_path)
        monkeypatch.setattr(assets_sync, "sync_assets", lambda: None)
        assets_sync.sync_if_needed(version="1.0.0")
        assert assets_sync.sync_if_needed(version="2.0.0") is True


class TestMainTriggersFirstRunSync:
    def test_doctor_triggers_sync_if_needed(self, monkeypatch):
        _patch_main_dependencies(monkeypatch)
        calls = []
        monkeypatch.setattr(assets_sync, "sync_if_needed", lambda: calls.append(True))
        monkeypatch.setattr(sys, "argv", ["claudespace", "doctor"])
        with pytest.raises(SystemExit):
            cli.main()
        assert calls == [True]

    def test_uninstall_never_triggers_sync_if_needed(self, monkeypatch):
        _patch_main_dependencies(monkeypatch)
        calls = []
        monkeypatch.setattr(assets_sync, "sync_if_needed", lambda: calls.append(True))
        monkeypatch.setattr(sys, "argv", ["claudespace", "uninstall"])
        cli.main()
        assert calls == []

    def test_update_never_triggers_sync_if_needed(self, monkeypatch):
        _patch_main_dependencies(monkeypatch)
        calls = []
        monkeypatch.setattr(assets_sync, "sync_if_needed", lambda: calls.append(True))
        monkeypatch.setattr(cli.update, "run_update", lambda: None)
        monkeypatch.setattr(sys, "argv", ["claudespace", "update"])
        cli.main()
        assert calls == []
