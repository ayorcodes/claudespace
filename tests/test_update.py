"""Interpreter resolution for `claudespace update`.

`update` uninstalls before reinstalling, so it must not reinstall against an
interpreter that the uninstall is about to delete - doing so failed with "No
executable for the provided Python version" and left the machine with
claudespace uninstalled.
"""

from __future__ import annotations

import sys

from claudespace import update


def test_a_pipx_venv_interpreter_is_recognised():
    assert update._in_pipx_venv("/Users/x/.local/pipx/venvs/claudespace/bin/python")
    assert not update._in_pipx_venv("/opt/homebrew/bin/python3.14")
    assert not update._in_pipx_venv("/usr/bin/python3")


def test_base_python_never_returns_a_path_inside_a_pipx_venv(monkeypatch, tmp_path):
    venv_python = tmp_path / ".local" / "pipx" / "venvs" / "claudespace" / "bin"
    venv_python.mkdir(parents=True)
    (venv_python / "python").write_text("")
    real = tmp_path / "python3.14"
    real.write_text("")

    monkeypatch.setattr(sys, "executable", str(venv_python / "python"))
    monkeypatch.setattr(sys, "_base_executable", str(real), raising=False)
    assert update._base_python() == str(real)


def test_base_python_returns_none_when_nothing_usable(monkeypatch, tmp_path):
    # Better to let pipx choose than to pass a path that doesn't exist.
    venv_python = tmp_path / ".local" / "pipx" / "venvs" / "claudespace" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")
    monkeypatch.setattr(sys, "executable", str(venv_python))
    monkeypatch.setattr(sys, "_base_executable", "/nonexistent/python", raising=False)
    assert update._base_python() is None


def test_base_python_accepts_a_plain_non_venv_interpreter(monkeypatch, tmp_path):
    real = tmp_path / "python3"
    real.write_text("")
    monkeypatch.setattr(sys, "executable", str(real))
    monkeypatch.setattr(sys, "_base_executable", None, raising=False)
    assert update._base_python() == str(real)
