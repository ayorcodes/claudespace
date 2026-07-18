"""Claude role wrappers, each pinned to a model and effort level.

These are installed as console-scripts (``claudespace:principal`` etc.) so
workspace templates work right after ``pip install`` with no shell config
required.
"""

from __future__ import annotations

import os
import sys


def _exec_claude(model: str, effort: str) -> None:
    """Replace this process with ``claude --model <model> --effort <effort> <extra args>``.

    Sets ``CLAUDESPACE_ROLE`` so this pane's Claude process (and anything it
    triggers, e.g. the Stop hook) knows which pipeline role it is running as.
    ``CLAUDESPACE_ROOT`` is expected to already be set by the shell command
    that launched this pane (``workspace.py`` sends ``cd <root> && ...``);
    fall back to the current directory if it is somehow missing.
    """
    role = sys._getframe(1).f_code.co_name
    env = dict(os.environ, CLAUDESPACE_ROLE=role)
    env.setdefault("CLAUDESPACE_ROOT", os.getcwd())
    os.execvpe(
        "claude",
        ["claude", "--model", model, "--effort", effort, *sys.argv[1:]],
        env,
    )


def principal() -> None:
    _exec_claude("claude-opus-4-8", "medium")


def planner() -> None:
    _exec_claude("claude-opus-4-8", "medium")


def implementer() -> None:
    _exec_claude("claude-sonnet-5", "medium")


def reviewer() -> None:
    _exec_claude("claude-sonnet-5", "medium")


def researcher() -> None:
    _exec_claude("claude-sonnet-5", "low")
