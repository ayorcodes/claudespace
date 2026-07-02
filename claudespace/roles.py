"""Claude role wrappers, each pinned to a model.

These are installed as console-scripts (``claudespace:planner`` etc.) so workspace
templates work right after ``pip install`` with no shell config required.
"""

from __future__ import annotations

import os
import sys


def _exec_claude(model: str) -> None:
    """Replace this process with ``claude --model <model> <extra args>``."""
    os.execvp("claude", ["claude", "--model", model, *sys.argv[1:]])


def planner() -> None:
    _exec_claude("opus")


def implementer() -> None:
    _exec_claude("sonnet")


def reviewer() -> None:
    _exec_claude("opus")


def memory() -> None:
    _exec_claude("haiku")
