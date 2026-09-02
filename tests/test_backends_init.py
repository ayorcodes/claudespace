"""``backends.get_backend()`` (AD5): resolves config/env into the right
concrete backend class, or a named error for an unknown one.
"""

from __future__ import annotations

import pytest

from claudespace import backends
from claudespace.backends.iterm import ItermBackend
from claudespace.backends.tmux import TmuxBackend


def test_defaults_to_iterm_backend(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDESPACE_TERMINAL", raising=False)
    monkeypatch.setattr(
        "claudespace.config.CONFIG_PATH", tmp_path / "nope.toml"
    )
    assert isinstance(backends.get_backend(), ItermBackend)


def test_explicit_name_bypasses_config(monkeypatch):
    assert isinstance(backends.get_backend("tmux"), TmuxBackend)
    assert isinstance(backends.get_backend("iterm2"), ItermBackend)


def test_unknown_explicit_name_raises():
    with pytest.raises(ValueError, match="warp"):
        backends.get_backend("warp")


def test_env_selects_tmux(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDESPACE_TERMINAL", "tmux")
    assert isinstance(backends.get_backend(), TmuxBackend)


def test_tmux_backend_uses_the_configured_viewer(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[terminal.tmux]\nviewer = "iterm2"\n')
    monkeypatch.setattr("claudespace.config.CONFIG_PATH", config_path)
    backend = backends.get_backend("tmux")
    assert backend._viewer == "iterm2"


def test_tmux_backend_defaults_to_ghostty_viewer(monkeypatch, tmp_path):
    monkeypatch.setattr("claudespace.config.CONFIG_PATH", tmp_path / "nope.toml")
    backend = backends.get_backend("tmux")
    assert backend._viewer == "ghostty"
