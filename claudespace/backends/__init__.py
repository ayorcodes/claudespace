"""Terminal-backend selection (AD5).

``get_backend()`` is the single place a consumer (``cli.py``) turns "which
terminal am I driving" into a concrete ``TerminalBackend`` instance -
nothing downstream of it ever imports ``iterm2`` or knows Ghostty exists.
"""

from __future__ import annotations

from claudespace.backends.base import TerminalBackend
from claudespace.config import load_terminal_backend

__all__ = ["TerminalBackend", "get_backend"]


def get_backend(name: str | None = None) -> TerminalBackend:
    """Construct the configured ``TerminalBackend``.

    ``name`` overrides config/env resolution - used by tests. Otherwise
    resolves via ``config.load_terminal_backend()`` (env var, then
    ``config.toml``, then the iTerm2 default). Raises ``ValueError`` (from
    ``load_terminal_backend``) naming an unknown configured value rather
    than silently picking a default.
    """
    resolved = name if name is not None else load_terminal_backend()

    if resolved == "iterm2":
        from claudespace.backends.iterm import ItermBackend

        return ItermBackend()
    if resolved == "ghostty":
        from claudespace.backends.ghostty import GhosttyBackend

        return GhosttyBackend()
    raise ValueError(
        f"Unknown terminal backend '{resolved}'. Known backends: ghostty, iterm2"
    )
