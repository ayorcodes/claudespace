"""``installed_channel()``/``competing_installs()`` (D6): which package
manager provisioned the running venv, and what else is on PATH.
"""

from __future__ import annotations

import os
import stat
import sys

from claudespace import channel


def test_installed_channel_reads_the_marker_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    (tmp_path / channel.CHANNEL_MARKER_NAME).write_text("npm")
    assert channel.installed_channel() == "npm"


def test_installed_channel_falls_back_to_pipx_path_heuristic(tmp_path, monkeypatch):
    venv = tmp_path / "pipx" / "venvs" / "claudespace"
    venv.mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(venv))
    assert channel.installed_channel() == "pipx"


def test_installed_channel_falls_back_to_npm_path_heuristic(tmp_path, monkeypatch):
    venv = tmp_path / "lib" / "node_modules" / "@ayorcodes" / "claudespace" / ".venv"
    venv.mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(venv))
    assert channel.installed_channel() == "npm"


def test_installed_channel_defaults_to_pipx_when_unrecognised(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    assert channel.installed_channel() == "pipx"


def test_write_channel_marker_writes_the_given_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    channel.write_channel_marker("npm")
    assert (tmp_path / channel.CHANNEL_MARKER_NAME).read_text() == "npm"
    assert channel.installed_channel() == "npm"


def _make_executable(path):
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def test_competing_installs_detects_both_channels(tmp_path, monkeypatch):
    pipx_dir = tmp_path / "pipx_bin"
    pipx_dir.mkdir()
    _make_executable(pipx_dir / "claudespace")

    npm_dir = tmp_path / "npm_bin"
    npm_dir.mkdir()
    npm_pkg_bin = tmp_path / "lib" / "node_modules" / "@ayorcodes" / "claudespace" / "bin"
    npm_pkg_bin.mkdir(parents=True)
    _make_executable(npm_pkg_bin / "claudespace.sh")
    os.symlink(npm_pkg_bin / "claudespace.sh", npm_dir / "claudespace")

    pipx_venv_bin = tmp_path / "pipx" / "venvs" / "claudespace" / "bin"
    pipx_venv_bin.mkdir(parents=True)
    _make_executable(pipx_venv_bin / "claudespace")
    os.remove(pipx_dir / "claudespace")
    os.symlink(pipx_venv_bin / "claudespace", pipx_dir / "claudespace")

    monkeypatch.setenv("PATH", os.pathsep.join([str(npm_dir), str(pipx_dir)]))
    found = channel.competing_installs()
    assert {c.name for c in found} == {"npm", "pipx"}


def test_competing_installs_deduplicates_the_same_resolved_binary(tmp_path, monkeypatch):
    real = tmp_path / "real_bin"
    real.mkdir()
    _make_executable(real / "claudespace")

    link_dir_a = tmp_path / "a"
    link_dir_a.mkdir()
    os.symlink(real / "claudespace", link_dir_a / "claudespace")
    link_dir_b = tmp_path / "b"
    link_dir_b.mkdir()
    os.symlink(real / "claudespace", link_dir_b / "claudespace")

    monkeypatch.setenv("PATH", os.pathsep.join([str(link_dir_a), str(link_dir_b)]))
    assert len(channel.competing_installs()) == 1


def test_competing_installs_returns_empty_when_none_found(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert channel.competing_installs() == []
