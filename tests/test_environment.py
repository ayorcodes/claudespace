"""Backend-aware install/doctor terminal detection (AD1-AD3): ``doctor``
must report what's usable across *any* supported terminal setup, not just
assume iTerm2 - while the real-run/watchdog path (``check_environment``)
keeps ensuring iTerm2 specifically, since that's reached only when iTerm2
is the chosen backend."""

from __future__ import annotations

import subprocess

from claudespace import environment


def _mdfind_hit(monkeypatch, hit: bool):
    def _fake_run(cmd, **kwargs):
        stdout = "/Applications/Fake.app\n" if hit else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout)

    monkeypatch.setattr(environment.subprocess, "run", _fake_run)


class TestAppInstalled:
    def test_hardcoded_path_hit(self, monkeypatch, tmp_path):
        app = tmp_path / "Fake.app"
        app.mkdir()
        _mdfind_hit(monkeypatch, hit=False)
        assert environment._app_installed("com.example.fake", (str(app),)) is True

    def test_mdfind_hit(self, monkeypatch, tmp_path):
        _mdfind_hit(monkeypatch, hit=True)
        assert (
            environment._app_installed("com.example.fake", (str(tmp_path / "nope"),))
            is True
        )

    def test_neither_hit(self, monkeypatch, tmp_path):
        _mdfind_hit(monkeypatch, hit=False)
        assert (
            environment._app_installed("com.example.fake", (str(tmp_path / "nope"),))
            is False
        )


class TestIsGhosttyInstalled:
    def test_delegates_to_app_installed(self, monkeypatch):
        calls = []

        def _fake(bundle_id, app_paths=()):
            calls.append((bundle_id, app_paths))
            return True

        monkeypatch.setattr(environment, "_app_installed", _fake)
        assert environment.is_ghostty_installed() is True
        assert calls == [(environment.utils.GHOSTTY_BUNDLE_ID, ())]


class TestIsCmuxInstalled:
    def test_delegates_to_app_installed(self, monkeypatch):
        calls = []

        def _fake(bundle_id, app_paths=()):
            calls.append((bundle_id, app_paths))
            return True

        monkeypatch.setattr(environment, "_app_installed", _fake)
        assert environment.is_cmux_installed() is True
        assert calls == [(environment.utils.CMUX_BUNDLE_ID, ())]


class TestIsCmuxReachable:
    def _fake_run(self, monkeypatch, *, returncode, stdout="", stderr=""):
        import subprocess

        def _run(cmd, **kwargs):
            assert cmd == ["cmux", "ping"]
            return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

        monkeypatch.setattr(environment.subprocess, "run", _run)

    def test_pong_is_reachable(self, monkeypatch):
        self._fake_run(monkeypatch, returncode=0, stdout="PONG")
        assert environment.is_cmux_reachable() == (True, None)

    def test_access_denied_names_the_socket_control_mode_fix(self, monkeypatch):
        self._fake_run(
            monkeypatch,
            returncode=1,
            stderr="Error: ERROR: Access denied - only processes started inside "
            "cmux can connect",
        )
        reachable, message = environment.is_cmux_reachable()
        assert reachable is False
        assert "socketControlMode" in message
        assert "automation" in message

    def test_other_failure_is_a_generic_message(self, monkeypatch):
        self._fake_run(monkeypatch, returncode=1, stderr="boom")
        reachable, message = environment.is_cmux_reachable()
        assert reachable is False
        assert message == "boom"

    def test_missing_binary_is_not_reachable(self, monkeypatch):
        def _run(cmd, **kwargs):
            raise FileNotFoundError("no such file")

        monkeypatch.setattr(environment.subprocess, "run", _run)
        reachable, message = environment.is_cmux_reachable()
        assert reachable is False
        assert "cmux ping" in message


class TestIsItermInstalledUnchanged:
    def test_byte_for_byte_preserved_behaviour(self, monkeypatch, tmp_path):
        # Reimplemented on _app_installed (AD3) - same bundle ID and paths.
        monkeypatch.setattr(environment, "ITERM_APP_PATHS", (str(tmp_path / "nope"),))
        _mdfind_hit(monkeypatch, hit=True)
        assert environment.is_iterm_installed() is True

        _mdfind_hit(monkeypatch, hit=False)
        assert environment.is_iterm_installed() is False


class TestDetectUsableBackends:
    def _patch(
        self,
        monkeypatch,
        *,
        iterm=False,
        tmux_available=False,
        viewer="ghostty",
        viewer_installed=False,
        cmux_installed=False,
        cmux_reachable=False,
    ):
        monkeypatch.setattr(environment, "is_iterm_installed", lambda: iterm)
        monkeypatch.setattr(
            environment.tmux_cli, "is_tmux_available", lambda: tmux_available
        )
        monkeypatch.setattr(environment, "load_tmux_viewer", lambda: viewer)
        monkeypatch.setattr(
            environment, "_viewer_installed", lambda v: viewer_installed
        )
        monkeypatch.setattr(environment, "is_cmux_installed", lambda: cmux_installed)
        monkeypatch.setattr(
            environment, "is_cmux_reachable", lambda: (cmux_reachable, None)
        )

    def test_iterm_only_present(self, monkeypatch):
        self._patch(monkeypatch, iterm=True)
        assert environment.detect_usable_backends() == ["iterm2"]

    def test_tmux_and_ghostty_present_no_iterm(self, monkeypatch):
        self._patch(monkeypatch, tmux_available=True, viewer_installed=True)
        assert environment.detect_usable_backends() == ["tmux"]

    def test_tmux_present_ghostty_absent_no_iterm(self, monkeypatch):
        self._patch(monkeypatch, tmux_available=True, viewer_installed=False)
        assert environment.detect_usable_backends() == []

    def test_both_present(self, monkeypatch):
        self._patch(monkeypatch, iterm=True, tmux_available=True, viewer_installed=True)
        assert environment.detect_usable_backends() == ["iterm2", "tmux"]

    def test_no_tmux_binary_skips_viewer_check(self, monkeypatch):
        self._patch(monkeypatch, tmux_available=False)
        assert environment.detect_usable_backends() == []

    def test_cmux_installed_and_reachable(self, monkeypatch):
        self._patch(monkeypatch, cmux_installed=True, cmux_reachable=True)
        assert environment.detect_usable_backends() == ["cmux"]

    def test_cmux_installed_but_not_reachable(self, monkeypatch):
        self._patch(monkeypatch, cmux_installed=True, cmux_reachable=False)
        assert environment.detect_usable_backends() == []

    def test_all_three_present(self, monkeypatch):
        self._patch(
            monkeypatch,
            iterm=True,
            tmux_available=True,
            viewer_installed=True,
            cmux_installed=True,
            cmux_reachable=True,
        )
        assert environment.detect_usable_backends() == ["iterm2", "tmux", "cmux"]


class TestViewerInstalled:
    def test_iterm2_viewer_delegates_to_is_iterm_installed(self, monkeypatch):
        monkeypatch.setattr(environment, "is_iterm_installed", lambda: True)
        assert environment._viewer_installed("iterm2") is True

    def test_ghostty_viewer_checks_app_installed(self, monkeypatch):
        monkeypatch.setattr(environment, "_app_installed", lambda bundle_id: True)
        assert environment._viewer_installed("ghostty") is True

    def test_unknown_viewer_is_not_usable(self, monkeypatch):
        assert environment._viewer_installed("some-unknown-terminal") is False


class TestRunDoctorChecks:
    def _patch(
        self,
        monkeypatch,
        *,
        claude_installed=True,
        usable=("iterm2",),
        iterm_installed=True,
        install_ok=True,
        api_ok=True,
    ):
        monkeypatch.setattr(environment, "is_claude_installed", lambda: claude_installed)
        monkeypatch.setattr(environment, "detect_usable_backends", lambda: list(usable))
        monkeypatch.setattr(environment, "is_iterm_installed", lambda: iterm_installed)
        install_calls = []

        def _fake_install(*, assume_yes):
            install_calls.append(assume_yes)
            return install_ok

        monkeypatch.setattr(environment, "install_iterm_via_brew", _fake_install)
        api_calls = []

        def _fake_api(*, iterm_was_running, launch):
            api_calls.append((iterm_was_running, launch))
            return api_ok

        monkeypatch.setattr(environment, "_ensure_api_enabled", _fake_api)
        monkeypatch.setattr(environment, "require_macos", lambda: None)
        return install_calls, api_calls

    def test_usable_setup_skips_fallback_install(self, monkeypatch, caplog):
        install_calls, _ = self._patch(monkeypatch, usable=("tmux",), iterm_installed=False)
        caplog.set_level("INFO")
        assert (
            environment.run_doctor_checks(iterm_was_running=False, assume_yes=False) is True
        )
        assert install_calls == []
        assert "Found usable terminal setup(s): tmux" in caplog.text

    def test_no_usable_setup_installs_iterm(self, monkeypatch, caplog):
        install_calls, _ = self._patch(monkeypatch, usable=())
        caplog.set_level("WARNING")
        environment.run_doctor_checks(iterm_was_running=False, assume_yes=True)
        assert install_calls == [True]
        assert "No supported terminal setup found" in caplog.text

    def test_no_usable_setup_and_install_fails_returns_false(self, monkeypatch):
        self._patch(monkeypatch, usable=(), install_ok=False)
        assert environment.run_doctor_checks(iterm_was_running=False, assume_yes=True) is False

    def test_iterm_present_ensures_api(self, monkeypatch):
        _, api_calls = self._patch(monkeypatch, iterm_installed=True)
        environment.run_doctor_checks(iterm_was_running=False, assume_yes=False)
        assert api_calls == [(False, True)]

    def test_iterm_absent_does_not_ensure_api(self, monkeypatch):
        _, api_calls = self._patch(
            monkeypatch, usable=("tmux",), iterm_installed=False
        )
        environment.run_doctor_checks(iterm_was_running=False, assume_yes=False)
        assert api_calls == []

    def test_claude_missing_returns_false(self, monkeypatch):
        self._patch(monkeypatch, claude_installed=False)
        assert environment.run_doctor_checks(iterm_was_running=False) is False
