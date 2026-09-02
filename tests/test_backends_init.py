"""``backends.get_backend()`` (AD5): resolves config/env into the right
concrete backend class, or a named error for an unknown one.
"""

from __future__ import annotations

import pytest

from claudespace import backends
from claudespace.backends.ghostty import GhosttyBackend
from claudespace.backends.iterm import ItermBackend


def test_defaults_to_iterm_backend(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDESPACE_TERMINAL", raising=False)
    monkeypatch.setattr(
        "claudespace.config.CONFIG_PATH", tmp_path / "nope.toml"
    )
    assert isinstance(backends.get_backend(), ItermBackend)


def test_explicit_name_bypasses_config(monkeypatch):
    assert isinstance(backends.get_backend("ghostty"), GhosttyBackend)
    assert isinstance(backends.get_backend("iterm2"), ItermBackend)


def test_unknown_explicit_name_raises():
    with pytest.raises(ValueError, match="warp"):
        backends.get_backend("warp")


def test_env_selects_ghostty(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDESPACE_TERMINAL", "ghostty")
    assert isinstance(backends.get_backend(), GhosttyBackend)
