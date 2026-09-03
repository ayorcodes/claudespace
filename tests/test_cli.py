"""``cli.py``'s argument parsing and backend resolution: iTerm2 stays the
default with no flag; ``--tmux`` overrides it for one invocation, the same
way ``CLAUDESPACE_TERMINAL`` does (but without needing a config file)."""

from __future__ import annotations

import sys

import pytest

from claudespace import cli
from claudespace.backends.cmux import CmuxBackend
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


def test_cmux_flag_defaults_to_false():
    args = cli._build_parser().parse_args(["--root", "/tmp"])
    assert args.cmux is False


def test_cmux_flag_can_be_set_on_the_main_command():
    args = cli._build_parser().parse_args(["--cmux", "--root", "/tmp"])
    assert args.cmux is True


def test_cmux_flag_can_be_set_on_watchdog():
    args = cli._build_parser().parse_args(["watchdog", "--cmux"])
    assert args.cmux is True


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


def test_resolve_backend_force_cmux_overrides_the_default(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDESPACE_TERMINAL", raising=False)
    monkeypatch.setattr("claudespace.config.CONFIG_PATH", tmp_path / "nope.toml")
    assert isinstance(cli._resolve_backend(force_cmux=True), CmuxBackend)


def test_resolve_backend_force_cmux_overrides_an_explicit_iterm2_config(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("CLAUDESPACE_TERMINAL", raising=False)
    config_path = tmp_path / "config.toml"
    config_path.write_text('[terminal]\nbackend = "iterm2"\n')
    monkeypatch.setattr("claudespace.config.CONFIG_PATH", config_path)
    assert isinstance(cli._resolve_backend(force_cmux=True), CmuxBackend)


def test_resolve_backend_rejects_both_tmux_and_cmux(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDESPACE_TERMINAL", raising=False)
    monkeypatch.setattr("claudespace.config.CONFIG_PATH", tmp_path / "nope.toml")
    with pytest.raises(SystemExit):
        cli._resolve_backend(force_tmux=True, force_cmux=True)


class TestThinkDefaultAndManualOverride:
    def test_think_defaults_to_on(self):
        args = cli._build_parser().parse_args(["--root", "/tmp"])
        assert args.think is True
        assert args.auto_handoff is True

    def test_manual_disables_both_auto_handoff_and_think(self):
        args = cli._build_parser().parse_args(["--manual", "--root", "/tmp"])
        cli._apply_manual_override(args)
        assert args.auto_handoff is False
        assert args.think is False

    def test_manual_wins_even_with_explicit_think(self):
        args = cli._build_parser().parse_args(["--manual", "--think", "--root", "/tmp"])
        cli._apply_manual_override(args)
        assert args.think is False

    def test_no_manual_leaves_think_on(self):
        args = cli._build_parser().parse_args(["--root", "/tmp"])
        cli._apply_manual_override(args)
        assert args.think is True
        assert args.auto_handoff is True


class TestRestoreFlag:
    def test_restore_flag_defaults_to_false(self):
        args = cli._build_parser().parse_args(["--root", "/tmp"])
        assert args.restore is False

    def test_restore_flag_can_be_set(self):
        args = cli._build_parser().parse_args(["--restore"])
        assert args.restore is True


ENTRY_A = {"session": "cs-aaa", "workspace": "/root/a", "instance": "i1", "roles": ["researcher"]}
ENTRY_B = {"session": "cs-bbb", "workspace": "/root/b", "instance": "i2", "roles": ["planner"]}


class TestPromptSelection:
    def test_non_interactive_returns_none_without_prompting(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
        assert cli._prompt_selection([ENTRY_A]) is None

    def test_single_entry_yes_selects_it(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        assert cli._prompt_selection([ENTRY_A]) == ENTRY_A

    def test_single_entry_blank_defaults_to_yes(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "")
        assert cli._prompt_selection([ENTRY_A]) == ENTRY_A

    def test_single_entry_no_declines(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        assert cli._prompt_selection([ENTRY_A]) is None

    def test_multiple_entries_picks_by_number(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "2")
        assert cli._prompt_selection([ENTRY_A, ENTRY_B]) == ENTRY_B

    def test_multiple_entries_blank_skips(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "")
        assert cli._prompt_selection([ENTRY_A, ENTRY_B]) is None

    def test_multiple_entries_out_of_range_returns_none(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "99")
        assert cli._prompt_selection([ENTRY_A, ENTRY_B]) is None

    def test_multiple_entries_non_numeric_returns_none(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "nope")
        assert cli._prompt_selection([ENTRY_A, ENTRY_B]) is None

    def test_ctrl_c_returns_none_multi_entry(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

        def _raise(_):
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", _raise)
        assert cli._prompt_selection([ENTRY_A, ENTRY_B]) is None

    def test_ctrl_c_returns_none_single_entry(self, monkeypatch):
        # Regression guard: the single-entry [Y/n] prompt used to skip the
        # try/except the multi-entry prompt had, so Ctrl-C there raised
        # straight through instead of declining cleanly.
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

        def _raise(_):
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", _raise)
        assert cli._prompt_selection([ENTRY_A]) is None

    def test_eof_returns_none_single_entry(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

        def _raise(_):
            raise EOFError

        monkeypatch.setattr("builtins.input", _raise)
        assert cli._prompt_selection([ENTRY_A]) is None


class TestDoctorSubcommand:
    def _patch(self, monkeypatch, *, ok: bool):
        monkeypatch.setattr(cli.environment, "require_macos", lambda: None)
        monkeypatch.setattr(cli.utils, "is_iterm_running", lambda: False)
        calls = []

        def _fake_run_doctor_checks(*, iterm_was_running, assume_yes, launch):
            calls.append((iterm_was_running, assume_yes, launch))
            return ok

        monkeypatch.setattr(cli.environment, "run_doctor_checks", _fake_run_doctor_checks)
        persistence_calls = []
        monkeypatch.setattr(
            cli, "_check_tmux_persistence", lambda: persistence_calls.append(True)
        )
        return calls, persistence_calls

    def test_doctor_calls_run_doctor_checks_not_check_environment(self, monkeypatch):
        calls, _ = self._patch(monkeypatch, ok=True)
        monkeypatch.setattr(sys, "argv", ["claudespace", "doctor"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        assert calls == [(False, False, True)]

    def test_doctor_still_runs_tmux_persistence_check(self, monkeypatch):
        _, persistence_calls = self._patch(monkeypatch, ok=True)
        monkeypatch.setattr(sys, "argv", ["claudespace", "doctor"])
        with pytest.raises(SystemExit):
            cli.main()
        assert persistence_calls == [True]

    def test_doctor_exit_code_mirrors_failure(self, monkeypatch):
        self._patch(monkeypatch, ok=False)
        monkeypatch.setattr(sys, "argv", ["claudespace", "doctor"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
