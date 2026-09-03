"""``TerminalBackend.notify``'s concrete default (D5): tmux/iTerm2 inherit
it unchanged, so it's byte-for-byte the same ``osascript`` call
``watchdog._notify`` used to make itself before this method existed."""

from __future__ import annotations

import asyncio
import subprocess

from claudespace.backends.tmux import TmuxBackend


def test_default_notify_invokes_osascript(monkeypatch):
    calls = []
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: calls.append((a, k)) or None
    )

    # TmuxBackend inherits TerminalBackend.notify unchanged - never overrides
    # it (only CmuxBackend does, D5).
    backend = TmuxBackend()
    asyncio.run(backend.notify(title="claudespace: possible stall", message="hi"))

    assert len(calls) == 1
    args, kwargs = calls[0]
    argv = args[0]
    assert argv[0] == "osascript"
    assert "-e" in argv
    script = argv[argv.index("-e") + 1]
    assert "possible stall" in script
    assert "hi" in script
    assert kwargs.get("check") is True


def test_default_notify_swallows_a_failure(monkeypatch):
    def _boom(*a, **k):
        raise subprocess.CalledProcessError(1, "osascript")

    monkeypatch.setattr(subprocess, "run", _boom)

    backend = TmuxBackend()
    # Best-effort: must not raise even when the underlying call fails.
    asyncio.run(backend.notify(title="t", message="m"))
