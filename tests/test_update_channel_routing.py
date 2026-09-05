"""``run_update()`` routes by install channel (D6): npm-installed claudespace
must update itself via npm, never fall through to the pipx clone-and-install
path, which would leave two competing installs on the machine.
"""

from __future__ import annotations

import pytest

from claudespace import update


def test_npm_channel_runs_npm_install(monkeypatch):
    monkeypatch.setattr(update.channel_module, "installed_channel", lambda: "npm")
    calls = []
    monkeypatch.setattr(update, "_run_npm_update", lambda: calls.append("npm"))
    monkeypatch.setattr(update, "_run_pipx_update", lambda: calls.append("pipx"))
    update.run_update()
    assert calls == ["npm"]


def test_pipx_channel_runs_pipx_update(monkeypatch):
    monkeypatch.setattr(update.channel_module, "installed_channel", lambda: "pipx")
    calls = []
    monkeypatch.setattr(update, "_run_npm_update", lambda: calls.append("npm"))
    monkeypatch.setattr(update, "_run_pipx_update", lambda: calls.append("pipx"))
    update.run_update()
    assert calls == ["pipx"]


def test_npm_update_invokes_npm_install_g_latest(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda tool: "/usr/bin/npm")
    calls = []

    class FakeResult:
        returncode = 0

    def _fake_run(cmd):
        calls.append(cmd)
        return FakeResult()

    monkeypatch.setattr(update.subprocess, "run", _fake_run)
    update._run_npm_update()
    assert calls == [["npm", "install", "-g", f"{update.NPM_PACKAGE}@latest"]]


def test_npm_update_exits_when_npm_missing(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda tool: None)
    with pytest.raises(SystemExit):
        update._run_npm_update()


def test_npm_update_exits_non_zero_on_install_failure(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda tool: "/usr/bin/npm")

    class FakeResult:
        returncode = 1

    monkeypatch.setattr(update.subprocess, "run", lambda cmd: FakeResult())
    with pytest.raises(SystemExit):
        update._run_npm_update()
