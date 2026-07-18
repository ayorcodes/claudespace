"""Claude role wrappers, each pinned to a model and effort level.

These are installed as console-scripts (``claudespace:principal`` etc.) so
workspace templates work right after ``pip install`` with no shell config
required.
"""

from __future__ import annotations

import os
import sys


def _exec_claude(model: str, effort: str) -> None:
    """Replace this process with ``claude --model <model> --effort <effort> <extra args>``."""
    os.execvp(
        "claude", ["claude", "--model", model, "--effort", effort, *sys.argv[1:]]
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
