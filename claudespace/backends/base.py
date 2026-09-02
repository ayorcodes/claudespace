"""The ``TerminalBackend`` interface (AD1).

One instance is created per CLI invocation (see ``backends.get_backend``)
and threaded through to whichever entrypoint ``run`` calls -
``workspace.py``, ``handoff.py``, ``watchdog.py``, ``messaging.py`` all take
the backend, never a specific terminal's connection/app object directly.

``Pane``/``Window`` are opaque handles: iTerm2's are ``iterm2.Session``/
``iterm2.Window``, tmux's are small dataclasses wrapping a ``#{pane_id}``/
session name (see ``backends/tmux.py``). Nothing outside a backend's own
module inspects one beyond passing it back into another backend method.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

from claudespace.backends.common import DEFAULT_MAX_ITEMS

if TYPE_CHECKING:
    from claudespace.config import Template


class Pane(Protocol):
    """Opaque handle to one terminal pane."""


class Window(Protocol):
    """Opaque handle to one terminal window."""


class BackendUnavailableError(RuntimeError):
    """The backend's automation surface can't be reached at all - app not
    running, automation permission denied, an osascript timeout, etc.

    Raised from within a backend's ``run``/reachability probe and translated
    there into a specific, actionable message before ``sys.exit(1)`` - never
    allowed to propagate as a bare traceback, and never caught anywhere to
    fall back to a different backend (AC7).
    """


class TerminalBackend(ABC):
    """Terminal-automation surface every consumer module drives through."""

    @abstractmethod
    def run(self, entrypoint: Callable[["TerminalBackend"], Awaitable[None]]) -> None:
        """Establish the backend's connection/loop, then run ``entrypoint``
        with this backend instance.

        Exits the process (``sys.exit(1)``) with an actionable,
        backend-specific message if the terminal can't be reached - never
        hangs, never falls back to another backend.
        """

    @abstractmethod
    async def build_workspace(
        self,
        *,
        marker: str,
        root: str,
        template_name: str,
        template: "Template",
        auto_handoff: bool = True,
        lazy: bool = False,
        think: bool = False,
        max_items: int = DEFAULT_MAX_ITEMS,
    ) -> Window:
        """Create a new window and launch either every pane or just the
        entry pane (``lazy=True``). See the old ``iterm.build_workspace``."""

    @abstractmethod
    async def find_workspace(self, marker: str) -> Window | None:
        """Return the window tagged with ``marker``, if one exists."""

    @abstractmethod
    async def list_windows(self) -> list[Window]:
        """Every currently open window - used only by ``workspace.py`` to
        snapshot "what's open before we build" so a stray default window
        from a cold app launch can be identified afterwards (see
        ``close_window_if_empty``). Not part of the old ``iterm.py``
        surface (``workspace.py`` called ``iterm2.async_get_app`` itself
        for this), but every backend needs an equivalent whole-app view.
        """

    @abstractmethod
    async def find_role_pane(
        self, *, marker: str, role: str, instance: str | None = None
    ) -> Pane | None:
        """Find the pane tagged with ``role`` inside workspace ``marker``."""

    @abstractmethod
    def each_pane(
        self, *, marker: str, instance: str | None = None
    ) -> AsyncIterator[tuple[str, Pane]]:
        """Yield ``(role, pane)`` for every pane tagged with workspace
        ``marker`` - used by ``watchdog.py``, which needs each pane's role
        to report a stall usefully and would otherwise have to re-derive it
        with a second per-pane lookup."""

    @abstractmethod
    async def activate_window(self, window: Window) -> None:
        """Bring an existing workspace window to the foreground."""

    @abstractmethod
    async def activate_pane(self, pane: Pane) -> None:
        """Focus ``pane`` (and its window/tab), so the active-pane highlight
        follows a handoff to its destination."""

    @abstractmethod
    async def send_role_prompt(
        self, role: str, pane: Pane, *, text: str, submit: bool
    ) -> None:
        """Wait for claude to be ready in ``pane``, then type ``text`` into
        it, submitting afterwards iff ``submit``."""

    @abstractmethod
    async def send_new(self, pane: Pane) -> None:
        """Wait for claude to be ready in ``pane``, then submit ``/new``."""

    @abstractmethod
    async def get_auto_handoff(
        self, *, marker: str, instance: str | None = None
    ) -> bool:
        """Auto-handoff toggle; ``False`` (prefill-only) if not found."""

    @abstractmethod
    async def get_lazy(self, *, marker: str, instance: str | None = None) -> bool:
        """``--lazy`` toggle; ``False`` if not found."""

    @abstractmethod
    async def get_template_name(
        self, *, marker: str, instance: str | None = None
    ) -> str | None:
        """Template the workspace was built with; ``None`` if not found."""

    @abstractmethod
    async def get_run_doc(
        self, *, marker: str, instance: str | None = None
    ) -> tuple[str | None, float | None]:
        """The workspace's current run doc path and start timestamp."""

    @abstractmethod
    async def set_run_doc(
        self,
        *,
        marker: str,
        instance: str | None = None,
        doc: str,
        started_at: float,
    ) -> None:
        """Stamp every pane in workspace ``marker`` with the active run's
        doc path and start time."""

    @abstractmethod
    async def close_window_if_empty(self, window: Window) -> None:
        """Close ``window`` if none of its panes are tagged as a workspace
        pane."""

    @abstractmethod
    async def split_pane(self, pane: Pane, *, vertical: bool) -> Pane:
        """Split ``pane``, returning the newly created sibling pane.

        The one primitive ``layouts.py`` needs directly (it previously
        called ``iterm2.Session.async_split_pane`` itself, outside
        ``iterm.py``'s old public surface - see ``Layout.build``): the
        layout *tree* stays backend-agnostic, only this primitive differs
        between iTerm2's native split and tmux's ``split-window``.
        ``vertical=True`` produces a left/right divider (new pane on the
        right), ``vertical=False`` a top/bottom divider (new pane below).
        """

    @abstractmethod
    async def reveal_role(
        self,
        *,
        marker: str,
        instance: str,
        root: str,
        template: "Template",
        role: str,
        source: Pane,
    ) -> Pane | None:
        """Split off room for ``role`` near ``source`` and launch it.
        Returns the newly launched pane."""

    @abstractmethod
    async def check_pane_stall(
        self,
        pane: Pane,
        *,
        role: str,
        previous: Any,
        now: float,
        stall_after_seconds: float,
    ) -> tuple[Any, bool]:
        """One watchdog poll's stall decision for ``pane``.

        ``previous`` is whatever this same method returned as its first
        element on this role's last poll (``None`` on the first poll ever
        seen for it). Both backends use the same full-fidelity
        content-diff algorithm (AD6) - ``backends/common.py``'s
        ``screen_signature``/``stall_decision`` - fed from each backend's
        own way of reading a pane's visible text. Returns
        ``(new_state, is_stalled_now)``; ``watchdog.py`` stores
        ``new_state`` verbatim and only acts on ``is_stalled_now``.
        """
