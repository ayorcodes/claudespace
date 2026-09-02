"""``claudespace/utils.py``: viewer-launch argv shape.

Regression guard for a real bug (review round 3): ``launch_viewer`` spawned
a plain ``tmux attach`` with no ``-L claudespace``, so it looked on the
user's *default* tmux socket while the actual session lived on
claudespace's dedicated one (AD8) - every viewer launch failed with
"no sessions" even though the real session was right there.
"""

from __future__ import annotations

import pytest

from claudespace import utils
from claudespace.backends.tmux_cli import SOCKET_NAME


@pytest.fixture
def recorded_call(monkeypatch):
    calls = []
    monkeypatch.setattr(
        utils.subprocess, "run", lambda args, **kwargs: calls.append(args)
    )
    return calls


def test_launch_viewer_targets_the_dedicated_socket(recorded_call):
    utils.launch_viewer("cs-abc12345-def67890", viewer="ghostty")
    args = recorded_call[0]
    assert "-L" in args
    assert args[args.index("-L") + 1] == SOCKET_NAME


def test_launch_viewer_argv_shape(recorded_call):
    utils.launch_viewer("my-session", viewer="ghostty")
    args = recorded_call[0]
    assert args == [
        "open",
        "-b",
        utils.GHOSTTY_BUNDLE_ID,
        "-n",
        "--args",
        "-e",
        "tmux",
        "-L",
        SOCKET_NAME,
        "attach",
        "-t",
        "my-session",
    ]


def test_launch_viewer_unknown_viewer_raises():
    with pytest.raises(ValueError, match="warp"):
        utils.launch_viewer("s", viewer="warp")
