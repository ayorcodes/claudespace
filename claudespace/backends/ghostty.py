"""Ghostty ``TerminalBackend`` implementation (experimental/opt-in).

Driven entirely via ``osascript`` against Ghostty's 1.3 AppleScript
dictionary (``application -> windows -> tabs -> terminals``, each with a
stable ``id``). Two gaps in that surface shape everything here:

- No screen-content read: readiness/submission confirmation fall back to
  polling the terminal ``name`` (see the design doc's Edge Cases), and the
  watchdog can only detect a pane's crash/disappearance, not a stuck
  dialog or runaway tool loop (AD6).
- No user-variable equivalent, so workspace/pane state lives in a file
  store instead (``backends/ghostty_state.py``, AD3).

Every ``osascript`` call goes through ``self._run``, which is injectable at
construction for tests (see ``Tests Required`` in the design doc) and is
the only place a subprocess is actually spawned.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from claudespace.backends.base import BackendUnavailableError, TerminalBackend
from claudespace.backends.common import (
    CLAUDE_READY_POLL_INTERVAL_SECONDS,
    CLAUDE_READY_TIMEOUT_SECONDS,
    DEFAULT_MAX_ITEMS,
    SUBMIT_KEYSTROKE_SETTLE_SECONDS,
    command_with_baked_persona,
    launch_command_text,
    role_prompt_prefix,
)
from claudespace.backends import ghostty_state
from claudespace.config import CANONICAL_PANES, PaneConfig, Template
from claudespace.layouts import get_layout
from claudespace.pipeline import think_marker_path
from claudespace.themes import ROLE_THEMES, banner_command
from claudespace import utils

logger = logging.getLogger(__name__)

OSASCRIPT_TIMEOUT_SECONDS = 5.0
MIN_GHOSTTY_VERSION = (1, 3)


@dataclass(frozen=True, slots=True)
class GhosttyPane:
    """Opaque pane handle: Ghostty addresses a terminal by its stable id
    alone (never composite with its window/tab), matching how the design's
    Data Flow section always refers to a revealed pane just by its id."""

    terminal_id: str
    window_id: str


@dataclass(frozen=True, slots=True)
class GhosttyWindow:
    window_id: str


class OsascriptError(RuntimeError):
    """A non-zero-exit ``osascript`` invocation, carrying its stderr so
    callers (notably the reachability probe) can classify the failure."""

    def __init__(self, message: str, *, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


NOT_MACOS_HELP = (
    "Ghostty support is macOS-only, matching claudespace's current platform "
    "scope."
)

NOT_RUNNING_HELP = (
    "Ghostty is not running. Start Ghostty and re-run claudespace "
    "(Ghostty support is experimental; iTerm2 remains the default backend)."
)

TCC_HELP = (
    "Ghostty refused the automation request (not authorized).\n"
    "Fix it in System Settings > Privacy & Security > Automation > "
    "<your terminal app> and enable 'Ghostty', then re-run claudespace."
)


def _version_mismatch_help(found: str) -> str:
    return (
        f"Ghostty's scripting dictionary looks older than the version "
        f"claudespace's Ghostty backend needs (found version {found!r}, "
        f"need >= {'.'.join(map(str, MIN_GHOSTTY_VERSION))}). Update Ghostty "
        "and re-run, or switch back to the iTerm2 backend."
    )


def _as_applescript_str(value: str) -> str:
    """Quote ``value`` as an AppleScript string literal.

    Every script below is built by interpolating an *id* (minted by us, at
    ``build_workspace``/``reveal_role`` time) or a caller-supplied prompt
    string through this - never raw shell interpolation of untrusted data,
    per the design's Security Considerations.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def script_probe_version() -> str:
    return 'tell application "Ghostty" to get version'


def script_create_window() -> str:
    return (
        'tell application "Ghostty"\n'
        "  activate\n"
        "  set w to make new window\n"
        "  set tb to first tab of w\n"
        "  set t to first terminal of tb\n"
        '  return (id of w as string) & "|" & (id of t as string)\n'
        "end tell"
    )


def script_split(terminal_id: str, direction: str) -> str:
    quoted = _as_applescript_str(terminal_id)
    return (
        'tell application "Ghostty"\n'
        f"  set t to first terminal whose id is {quoted}\n"
        f"  set nt to split t direction {direction}\n"
        "  return id of nt as string\n"
        "end tell"
    )


def script_focus_terminal(terminal_id: str) -> str:
    quoted = _as_applescript_str(terminal_id)
    return f'tell application "Ghostty" to focus (first terminal whose id is {quoted})'


def script_select_window(window_id: str) -> str:
    quoted = _as_applescript_str(window_id)
    return f'tell application "Ghostty" to select (first window whose id is {quoted})'


def script_input_text(terminal_id: str, text: str) -> str:
    quoted_id = _as_applescript_str(terminal_id)
    quoted_text = _as_applescript_str(text)
    return (
        f'tell application "Ghostty" to input text {quoted_text} to '
        f"(first terminal whose id is {quoted_id})"
    )


def script_send_return(terminal_id: str) -> str:
    quoted = _as_applescript_str(terminal_id)
    return (
        f'tell application "Ghostty" to send key return to '
        f"(first terminal whose id is {quoted})"
    )


def script_read_name(terminal_id: str) -> str:
    quoted = _as_applescript_str(terminal_id)
    return f'tell application "Ghostty" to return name of (first terminal whose id is {quoted})'


def script_exists(terminal_id: str) -> str:
    quoted = _as_applescript_str(terminal_id)
    return f'tell application "Ghostty" to return exists (first terminal whose id is {quoted})'


def script_close_window(window_id: str) -> str:
    quoted = _as_applescript_str(window_id)
    return f'tell application "Ghostty" to close (first window whose id is {quoted})'


def script_list_windows() -> str:
    return (
        'tell application "Ghostty"\n'
        "  set ids to {}\n"
        "  repeat with w in every window\n"
        "    set end of ids to (id of w as string)\n"
        "  end repeat\n"
        '  set AppleScript\'s text item delimiters to "|"\n'
        "  return ids as string\n"
        "end tell"
    )


def script_count_terminals(window_id: str) -> str:
    quoted = _as_applescript_str(window_id)
    return (
        'tell application "Ghostty" to return (count of terminals of '
        f"(first window whose id is {quoted})) as string"
    )


class GhosttyBackend(TerminalBackend):
    """``TerminalBackend`` implementation driving Ghostty via AppleScript."""

    def __init__(
        self, *, runner: Callable[[str, float], str] | None = None
    ) -> None:
        self._runner = runner or self._default_runner

    @staticmethod
    def _default_runner(script: str, timeout: float) -> str:
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendUnavailableError(
                f"Ghostty automation timed out after {timeout}s"
            ) from exc
        if result.returncode != 0:
            raise OsascriptError(
                (result.stderr or "").strip(), returncode=result.returncode
            )
        return result.stdout.strip()

    async def _run(self, script: str, *, timeout: float = OSASCRIPT_TIMEOUT_SECONDS) -> str:
        return await asyncio.to_thread(self._runner, script, timeout)

    # -- reachability -----------------------------------------------------

    def _probe_reachability(self) -> None:
        """Bounded, classified check that Ghostty can actually be driven
        before anything else runs (AC7 / FR8) - never an indefinite wait,
        never a silent fallback to a different backend.
        """
        if not utils.is_ghostty_running():
            logger.error("%s", NOT_RUNNING_HELP)
            sys.exit(1)

        try:
            version = self._runner(script_probe_version(), OSASCRIPT_TIMEOUT_SECONDS)
        except BackendUnavailableError as exc:
            logger.error("Ghostty automation timed out: %s", exc)
            sys.exit(1)
        except OsascriptError as exc:
            message = str(exc)
            if exc.returncode == 1743 or "not authorized" in message.lower():
                logger.error("%s", TCC_HELP)
            else:
                logger.error(
                    "Could not reach Ghostty's scripting dictionary: %s\n%s",
                    message,
                    _version_mismatch_help("unknown"),
                )
            sys.exit(1)

        parts = version.split(".")
        try:
            found = tuple(int(p) for p in parts[: len(MIN_GHOSTTY_VERSION)])
        except ValueError:
            found = ()
        if found < MIN_GHOSTTY_VERSION:
            logger.error("%s", _version_mismatch_help(version))
            sys.exit(1)

    def run(self, entrypoint: Callable[[TerminalBackend], Awaitable[None]]) -> None:
        if sys.platform != "darwin":
            logger.error("%s", NOT_MACOS_HELP)
            sys.exit(1)
        self._probe_reachability()
        asyncio.run(entrypoint(self))

    # -- pane/window handle helpers ---------------------------------------

    async def _pane_exists(self, pane: GhosttyPane) -> bool:
        try:
            result = await self._run(script_exists(pane.terminal_id))
        except OsascriptError:
            return False
        return result.strip() == "true"

    def _resolve_instance_entries(
        self, marker: str, instance: str | None
    ) -> list[tuple[str, ghostty_state.InstanceState]]:
        state = ghostty_state.load(marker)
        if state is None:
            return []
        instances = state.get("instances", {})
        if instance is not None:
            entry = instances.get(instance)
            return [(instance, entry)] if entry is not None else []
        return list(instances.items())

    # -- workspace/pane lifecycle ------------------------------------------

    async def _launch_pane(
        self,
        pane: GhosttyPane,
        *,
        marker: str,
        instance: str,
        root: str,
        template_name: str,
        pane_cfg: PaneConfig,
        auto_handoff: bool,
        lazy: bool,
        think: bool,
        max_items: int,
    ) -> None:
        ghostty_state.update_instance(
            marker,
            instance,
            window_id=pane.window_id,
            auto_handoff=auto_handoff,
            lazy=lazy,
            template=template_name,
        )
        ghostty_state.set_role_pane(marker, instance, pane_cfg.role, pane.terminal_id)

        banner = f"{banner_command(pane_cfg.role)} && " if pane_cfg.role in ROLE_THEMES else ""
        command = command_with_baked_persona(pane_cfg.role, pane_cfg.command)
        text = launch_command_text(
            root=root,
            role=pane_cfg.role,
            instance=instance,
            think=think,
            max_items=max_items,
            command=command,
            banner=banner,
        )
        # `input text` is paste-style (per the design's verified API facts)
        # and does not submit on its own - the trailing newline
        # launch_command_text always ends with is stripped and a real
        # `send key return` follows, mirroring iTerm2's own two-step
        # type-then-submit split in _send_role_prompt.
        await self._run(script_input_text(pane.terminal_id, text.rstrip("\n")))
        await self._run(script_send_return(pane.terminal_id))
        logger.info("Launched %s in role '%s' (Ghostty)", command, pane_cfg.role)

    async def _wait_ready(self, pane: GhosttyPane, role: str) -> bool:
        """Poll ``pane``'s title until it equals ``role`` - the Ghostty
        equivalent of iTerm2's ``❯`` screen-content poll, since Ghostty
        exposes no screen buffer to read (see the design's Edge Cases:
        Readiness detection). ``claude --name <role>`` is what drives the
        title to that value once its TUI is up.
        """
        deadline = time.monotonic() + CLAUDE_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                name = await self._run(script_read_name(pane.terminal_id))
            except OsascriptError:
                return False
            if name.strip() == role:
                return True
            await asyncio.sleep(CLAUDE_READY_POLL_INTERVAL_SECONDS)
        return False

    async def _prefill_role_command(self, role: str, pane: GhosttyPane) -> None:
        prefix = role_prompt_prefix(role)
        if not prefix:
            return
        await self.send_role_prompt(role, pane, text=prefix, submit=False)

    async def build_workspace(
        self,
        *,
        marker: str,
        root: str,
        template_name: str,
        template: Template,
        auto_handoff: bool = True,
        lazy: bool = False,
        think: bool = False,
        max_items: int = DEFAULT_MAX_ITEMS,
    ) -> GhosttyWindow:
        ids = await self._run(script_create_window())
        window_id, terminal_id = ids.split("|", 1)
        instance = str(uuid.uuid4())
        root_pane = GhosttyPane(terminal_id=terminal_id, window_id=window_id)

        if lazy:
            entry_pane = next(p for p in template.panes if p.role == template.entry_role)
            await self._launch_pane(
                root_pane,
                marker=marker,
                instance=instance,
                root=root,
                template_name=template_name,
                pane_cfg=entry_pane,
                auto_handoff=auto_handoff,
                lazy=True,
                think=think,
                max_items=max_items,
            )
            await self._prefill_role_command(entry_pane.role, root_pane)
            return GhosttyWindow(window_id=window_id)

        layout = get_layout(template.layout)
        configured_roles = {pane.role for pane in template.panes}
        if configured_roles != layout.roles:
            raise ValueError(
                f"Template panes {sorted(configured_roles)} do not match "
                f"layout '{template.layout}' roles {sorted(layout.roles)}"
            )

        panes_by_role = await layout.build(self, root_pane)

        for pane_cfg in template.panes:
            await self._launch_pane(
                panes_by_role[pane_cfg.role],
                marker=marker,
                instance=instance,
                root=root,
                template_name=template_name,
                pane_cfg=pane_cfg,
                auto_handoff=auto_handoff,
                lazy=False,
                think=think,
                max_items=max_items,
            )

        for pane_cfg in template.panes:
            await self._prefill_role_command(pane_cfg.role, panes_by_role[pane_cfg.role])

        return GhosttyWindow(window_id=window_id)

    async def find_workspace(self, marker: str) -> GhosttyWindow | None:
        for _instance, entry in self._resolve_instance_entries(marker, None):
            window_id = entry.get("window_id")
            roles = entry.get("roles", {})
            if not window_id or not roles:
                continue
            for terminal_id in roles.values():
                if await self._pane_exists(GhosttyPane(terminal_id=terminal_id, window_id=window_id)):
                    return GhosttyWindow(window_id=window_id)
        return None

    async def list_windows(self) -> list[GhosttyWindow]:
        result = await self._run(script_list_windows())
        return [GhosttyWindow(window_id=wid) for wid in result.split("|") if wid]

    async def find_role_pane(
        self, *, marker: str, role: str, instance: str | None = None
    ) -> GhosttyPane | None:
        for _instance, entry in self._resolve_instance_entries(marker, instance):
            terminal_id = entry.get("roles", {}).get(role)
            if terminal_id is None:
                continue
            pane = GhosttyPane(terminal_id=terminal_id, window_id=entry.get("window_id", ""))
            if await self._pane_exists(pane):
                return pane
        return None

    async def each_pane(
        self, *, marker: str, instance: str | None = None
    ) -> AsyncIterator[tuple[str, GhosttyPane]]:
        for _instance, entry in self._resolve_instance_entries(marker, instance):
            window_id = entry.get("window_id", "")
            for role, terminal_id in entry.get("roles", {}).items():
                pane = GhosttyPane(terminal_id=terminal_id, window_id=window_id)
                if await self._pane_exists(pane):
                    yield role, pane

    async def activate_window(self, window: GhosttyWindow) -> None:
        await self._run(script_select_window(window.window_id))

    async def activate_pane(self, pane: GhosttyPane) -> None:
        await self._run(script_focus_terminal(pane.terminal_id))

    async def send_role_prompt(
        self, role: str, pane: GhosttyPane, *, text: str, submit: bool
    ) -> None:
        ready = await self._wait_ready(pane, role)
        if not ready:
            logger.warning(
                "Gave up waiting for claude to be ready in role '%s' - "
                "skipping prompt send (Ghostty)",
                role,
            )
            return
        await self._run(script_input_text(pane.terminal_id, text))
        if not submit:
            return

        await asyncio.sleep(SUBMIT_KEYSTROKE_SETTLE_SECONDS)
        await self._run(script_send_return(pane.terminal_id))
        # No screen read means no verify-and-resend loop (see the design's
        # Edge Cases: Submission confirmation) - one settle-then-resend is
        # kept as a harmless no-op belt-and-suspenders: a bare Return on an
        # already-empty claude input does nothing.
        await asyncio.sleep(SUBMIT_KEYSTROKE_SETTLE_SECONDS)
        await self._run(script_send_return(pane.terminal_id))

    async def send_new(self, pane: GhosttyPane) -> None:
        ready = await self._wait_ready(pane, "")
        # send_new is only ever called on a pane already tagged with its
        # role, whose title should already read that role - but we don't
        # have the role here (mirrors iTerm2's send_new, which also takes
        # no role). Fall back to a fixed settle instead of a role-title
        # poll: functionally the same graceful degradation as iTerm2's own
        # ready-timeout path.
        if not ready:
            await asyncio.sleep(CLAUDE_READY_TIMEOUT_SECONDS)
        await self._run(script_input_text(pane.terminal_id, "/new"))
        await asyncio.sleep(SUBMIT_KEYSTROKE_SETTLE_SECONDS)
        await self._run(script_send_return(pane.terminal_id))

    async def get_auto_handoff(
        self, *, marker: str, instance: str | None = None
    ) -> bool:
        entries = self._resolve_instance_entries(marker, instance)
        return bool(entries[0][1].get("auto_handoff", False)) if entries else False

    async def get_lazy(self, *, marker: str, instance: str | None = None) -> bool:
        entries = self._resolve_instance_entries(marker, instance)
        return bool(entries[0][1].get("lazy", False)) if entries else False

    async def get_template_name(
        self, *, marker: str, instance: str | None = None
    ) -> str | None:
        entries = self._resolve_instance_entries(marker, instance)
        return (entries[0][1].get("template") or None) if entries else None

    async def get_run_doc(
        self, *, marker: str, instance: str | None = None
    ) -> tuple[str | None, float | None]:
        entries = self._resolve_instance_entries(marker, instance)
        if not entries:
            return None, None
        entry = entries[0][1]
        doc = entry.get("run_doc") or None
        started = entry.get("run_started")
        return doc, float(started) if started else None

    async def set_run_doc(
        self, *, marker: str, instance: str | None = None, doc: str, started_at: float
    ) -> None:
        state = ghostty_state.load(marker)
        if state is None:
            return
        targets = [instance] if instance is not None else list(state.get("instances", {}))
        for target in targets:
            ghostty_state.update_instance(marker, target, run_doc=doc, run_started=started_at)

    async def close_window_if_empty(self, window: GhosttyWindow) -> None:
        """Best-effort stray-window cleanup (see ``workspace.py``'s
        cold-launch handling). Ghostty's read-only properties (AD3) mean a
        pane can't be tagged the way ``WORKSPACE_VAR`` tags an iTerm2
        session, so this approximates "empty" as "still a single, unsplit
        terminal" rather than checking a tag - a documented, minor
        degradation versus iTerm2's exact check.
        """
        try:
            count = await self._run(script_count_terminals(window.window_id))
        except OsascriptError:
            return
        if count.strip() != "1":
            return
        try:
            await self._run(script_close_window(window.window_id))
        except OsascriptError:
            logger.warning("Failed to close stray Ghostty window %s", window.window_id)

    async def split_pane(self, pane: GhosttyPane, *, vertical: bool) -> GhosttyPane:
        direction = "right" if vertical else "down"
        new_id = await self._run(script_split(pane.terminal_id, direction))
        return GhosttyPane(terminal_id=new_id.strip(), window_id=pane.window_id)

    async def reveal_role(
        self,
        *,
        marker: str,
        instance: str,
        root: str,
        template: Template,
        role: str,
        source: GhosttyPane,
    ) -> GhosttyPane | None:
        pane_cfg = next((p for p in template.panes if p.role == role), None) or CANONICAL_PANES[role]
        entry = ghostty_state.get_instance(marker, instance) or {}
        auto_handoff = bool(entry.get("auto_handoff", False))
        template_name = entry.get("template") or ""
        depth = int(entry.get("split_depth", 0) or 0)
        vertical = depth % 2 == 0

        new_pane = await self.split_pane(source, vertical=vertical)
        ghostty_state.update_instance(marker, instance, split_depth=depth + 1)
        await self._launch_pane(
            new_pane,
            marker=marker,
            instance=instance,
            root=root,
            template_name=template_name,
            pane_cfg=pane_cfg,
            auto_handoff=auto_handoff,
            lazy=True,
            think=os.path.isfile(think_marker_path(root)),
            max_items=DEFAULT_MAX_ITEMS,
        )
        return new_pane

    async def check_pane_stall(
        self,
        pane: GhosttyPane,
        *,
        role: str,
        previous: dict[str, Any] | None,
        now: float,
        stall_after_seconds: float,
    ) -> tuple[dict[str, Any], bool]:
        """AD6: no screen buffer to diff, so this detects only a pane's
        crash/disappearance - the reliably-detectable subset. ``notified``
        avoids re-flagging the same dead pane on every subsequent poll.
        """
        exists = await self._pane_exists(pane)
        if exists:
            return {"notified": False}, False
        already_notified = bool(previous) and previous.get("notified")
        return {"notified": True}, not already_notified
