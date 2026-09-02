"""``cli.py``'s argument parsing and backend resolution: iTerm2 stays the
default with no flag; ``--tmux`` overrides it for one invocation, the same
way ``CLAUDESPACE_TERMINAL`` does (but without needing a config file)."""

from __future__ import annotations

from claudespace import cli
from claudespace.backends.iterm import ItermBackend
from claudespace.backends.tmux import TmuxBackend


def test_tmux_flag_defaults_to_false():
    args = cli._build_parser().parse_args(["--root", "/tmp"])
    assert args.tmux is False


def test_tmux_flag_can_be_set_on_the_main_command():
    args = cli._build_parser().parse_args(["--tmux", "--root", "/tmp"])
    assert args.tmux is True


def test_tmux_flag_can_be_set_on_watchdog():
    args = cli._build_parser().parse_args(["watchdog", "--tmux"])
    assert args.tmux is True


def test_resolve_backend_defaults_to_iterm2(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDESPACE_TERMINAL", raising=False)
    monkeypatch.setattr("claudespace.config.CONFIG_PATH", tmp_path / "nope.toml")
    assert isinstance(cli._resolve_backend(), ItermBackend)


def test_resolve_backend_force_tmux_overrides_the_default(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDESPACE_TERMINAL", raising=False)
    monkeypatch.setattr("claudespace.config.CONFIG_PATH", tmp_path / "nope.toml")
    assert isinstance(cli._resolve_backend(force_tmux=True), TmuxBackend)


def test_resolve_backend_force_tmux_overrides_an_explicit_iterm2_config(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("CLAUDESPACE_TERMINAL", raising=False)
    config_path = tmp_path / "config.toml"
    config_path.write_text('[terminal]\nbackend = "iterm2"\n')
    monkeypatch.setattr("claudespace.config.CONFIG_PATH", config_path)
    assert isinstance(cli._resolve_backend(force_tmux=True), TmuxBackend)
