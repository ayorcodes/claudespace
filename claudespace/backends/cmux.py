"""cmux ``TerminalBackend`` implementation (D1-D5).

A GUI-app backend (like iTerm2 - cmux must be running and driven through
its Unix-socket automation surface) but driven the way ``TmuxBackend`` is:
entirely through a CLI subprocess boundary (``backends/cmux_cli.py``), never
an in-process client library.

cmux exposes no arbitrary per-pane/-workspace key/value store (unlike
iTerm2's user-variables or tmux's ``@cs_*`` options) - the spike's central
finding. Identity instead rides the one field the spike proved writable and
readable back, a pane's tab title (D2): ``cs:<instance>:<role>``, both keys
the ``@cs_*`` substitute needed. Every other piece of mutable workspace
state (``auto_handoff``, ``lazy``, ``template``, ``run_doc``/``run_started``)
is file-homed instead (D3), under the same per-session marker directory
every pipeline marker already uses - a fresh Stop-hook process rediscovers
it with zero cmux calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from claudespace import environment
from claudespace.backends import cmux_cli
from claudespace.backends.base import TerminalBackend
from claudespace.backends.common import (
    CLAUDE_PROMPT_MARKER,
    CLAUDE_READY_POLL_INTERVAL_SECONDS,
    CLAUDE_READY_TIMEOUT_SECONDS,
    DEFAULT_MAX_ITEMS,
    SUBMIT_CONFIRM_POLL_INTERVAL_SECONDS,
    SUBMIT_CONFIRM_TIMEOUT_SECONDS,
    SUBMIT_KEYSTROKE_SETTLE_SECONDS,
    SUBMIT_MAX_ATTEMPTS,
    command_with_baked_persona,
    launch_command_text,
    role_prompt_prefix,
    screen_signature,
    stall_decision,
)
from claudespace.config import CANONICAL_PANES, PaneConfig, Template
from claudespace.layouts import get_layout
from claudespace.pipeline import resolve_root, think_active
from claudespace.themes import ROLE_THEMES, banner_command

logger = logging.getLogger(__name__)

NOT_MACOS_HELP = (
    "The cmux backend is macOS-only, matching claudespace's current platform "
    "scope."
)

CMUX_NOT_FOUND_HELP = (
    "cmux is required for the cmux backend and was not found on PATH.\n"
    "Install it (brew install --cask cmux) or set terminal.backend = "
    '"iterm2" in ~/.config/claudespace/config.toml.'
)

# D2: only a title matching this exactly is treated as ours - anything else
# (user-renamed, a foreign workspace's own tab) degrades to "not found"
# rather than a crash or misroute (Validation).
#
# Deviation from D2 as written: the design's own text shortens this to the
# first 8 hex chars of the instance ("matching the tmux session-name
# convention"), but tmux's *session name* is cosmetic there - identity/
# lookup on that backend keys off the full, untruncated `@cs_instance` pane
# option. cmux has no such second channel: the tab title is simultaneously
# the only display label and the only identity mechanism, and a widely-shared
# caller outside this backend (`workspace.py`'s `--think` toggle ->
# `pipeline.think_marker_path`) needs `Window.instance`/`CmuxPane`'s instance
# back in *full* to build `session_marker_dir(marker, instance)` correctly -
# an 8-char prefix there points at a directory no pane's env actually uses,
# silently breaking `--think` on an already-open cmux workspace. Carrying
# the full UUID costs nothing the spike's A6 didn't already prove (any
# string round-trips through set/list) and removes the bug entirely.
_TITLE_RE = re.compile(
    r"^cs:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):([A-Za-z0-9_-]+)$"
)

STATE_FILE_NAME = "workspace-state.json"


def _pane_title(instance: str, role: str) -> str:
    return f"cs:{instance}:{role}"


def _parse_title(title: str) -> tuple[str, str] | None:
    """``(instance, role)`` if ``title`` matches the ``cs:<uuid>:<role>``
    shape this backend owns, else ``None``."""
    match = _TITLE_RE.match(title)
    return (match.group(1), match.group(2)) if match else None


@dataclass(frozen=True, slots=True)
class CmuxPane:
    """Opaque pane handle: a surface ref plus the workspace ref it lives in
    - most cmux CLI calls need both to target a specific pane."""

    surface_ref: str
    workspace_ref: str


@dataclass(frozen=True, slots=True)
class CmuxWindow:
    """One claudespace workspace = one cmux workspace."""

    workspace_ref: str
    instance: str


def _state_dir() -> str:
    """User-level directory holding the cmux backend's per-session runtime
    state, honouring ``XDG_STATE_HOME`` (default ``~/.local/state``)."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "claudespace", "cmux")


def _state_path(instance: str) -> str:
    """Path to one session's runtime state (``auto_handoff``/``lazy``/
    ``template``/``run_doc``), keyed on the session ``instance`` alone at a
    fixed user-level location - deliberately NOT under the repo's
    ``.claudespace/`` tree.

    This state used to live at ``session_marker_dir(marker, instance)/...``,
    i.e. under the repo root addressed by ``CLAUDESPACE_ROOT``. That silently
    broke lazy reveal across a git worktree: ``build_workspace`` writes this
    state before any worktree exists (under the original checkout), but once a
    role follows a worktree it re-exports ``CLAUDESPACE_ROOT`` into the
    worktree, so the handoff hook then reads
    ``<worktree>/.claudespace/s/<instance>/...`` where nothing was ever
    written. ``get_template_name`` returned ``None`` and
    ``handoff._reveal_destination`` bailed, so the next role's pane never
    appeared. The tmux/iTerm2 backends never hit this because they keep the
    same state as pane options that travel with the pane, off the repo tree
    entirely. The ``instance`` is worktree-invariant too (it is in every
    pane's env and every surface title), so keying on it alone is reachable
    from any pane regardless of cwd or worktree.
    """
    return os.path.join(_state_dir(), f"{instance}.json")


def _read_state(instance: str) -> dict[str, Any]:
    try:
        with open(_state_path(instance)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_state(instance: str, state: dict[str, Any]) -> None:
    path = _state_path(instance)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f)


class CmuxBackend(TerminalBackend):
    """``TerminalBackend`` implementation driving cmux's socket API."""

    BACKEND_NAME = "cmux"

    def run(self, entrypoint: Callable[[TerminalBackend], Awaitable[None]]) -> None:
        if sys.platform != "darwin":
            logger.error("%s", NOT_MACOS_HELP)
            sys.exit(1)
        if not cmux_cli.is_cmux_available():
            logger.error("%s", CMUX_NOT_FOUND_HELP)
            sys.exit(1)

        # D4: the spike's one real surprise - a 0600/owner-checked socket is
        # necessary but not sufficient. Fail fast here with the exact
        # remediation rather than letting the workspace half-build before
        # the first real call hits "Access denied".
        reachable, message = environment.is_cmux_reachable()
        if not reachable:
            logger.error("cmux is not reachable: %s", message)
            sys.exit(1)

        asyncio.run(entrypoint(self))

    # -- discovery helpers ---------------------------------------------------

    async def _find_workspace_ref_and_instance(
        self, marker: str
    ) -> tuple[str, str, str] | None:
        """``(workspace_ref, workspace_id, instance)`` for the workspace
        whose ``current_directory == marker``, reading its instance back
        from any ``cs:*:*`` surface title it holds (D2's instance-less
        lookup, used by the attach-or-build dedup)."""
        for ws in await cmux_cli.workspace_list():
            if ws.get("current_directory") != marker:
                continue
            for surface in await cmux_cli.surface_list(ws["id"]):
                parsed = _parse_title(surface.get("title") or "")
                if parsed:
                    return ws["ref"], ws["id"], parsed[0]
        return None

    async def _find_by_instance(self, instance: str) -> tuple[str, str] | None:
        """``(workspace_ref, workspace_id)`` for whichever workspace holds a
        surface titled ``cs:<instance>:*`` - the authoritative, instance-keyed
        lookup every handoff/watchdog call uses (D2)."""
        for ws in await cmux_cli.workspace_list():
            for surface in await cmux_cli.surface_list(ws["id"]):
                parsed = _parse_title(surface.get("title") or "")
                if parsed and parsed[0] == instance:
                    return ws["ref"], ws["id"]
        return None

    async def _resolve_instance(self, marker: str, instance: str | None) -> str | None:
        """D3's "instance-less reads" edge case: resolve via
        ``find_workspace`` first when the caller (only ``workspace.py``'s
        attach-or-build probe) doesn't yet know one."""
        if instance is not None:
            return instance
        found = await self._find_workspace_ref_and_instance(marker)
        return found[2] if found else None

    async def _workspace_id_for_ref(self, workspace_ref: str) -> str | None:
        for ws in await cmux_cli.workspace_list():
            if ws.get("ref") == workspace_ref:
                return ws["id"]
        return None

    async def _locate_fresh_workspace(self, workspace_ref: str) -> tuple[str, str]:
        """``(workspace_id, root_surface_ref)`` right after
        ``cmux_cli.workspace_create`` - the CLI's ``OK workspace:N`` doesn't
        carry the id or the workspace's single starting surface, both
        needed immediately to tag/launch it."""
        for ws in await cmux_cli.workspace_list():
            if ws.get("ref") == workspace_ref:
                surfaces = await cmux_cli.surface_list(ws["id"])
                if not surfaces:
                    raise RuntimeError(
                        f"cmux workspace {workspace_ref} has no surfaces right "
                        "after creation"
                    )
                return ws["id"], surfaces[0]["ref"]
        raise RuntimeError(f"cmux workspace {workspace_ref} not found right after creation")

    # -- pane launch / readiness / prompt delivery ---------------------------

    async def _launch_pane(
        self,
        pane: CmuxPane,
        *,
        instance: str,
        root: str,
        pane_cfg: PaneConfig,
        think: bool,
        max_items: int,
    ) -> None:
        await cmux_cli.rename_tab(
            workspace_ref=pane.workspace_ref,
            surface_ref=pane.surface_ref,
            title=_pane_title(instance, pane_cfg.role),
        )

        banner = f"{banner_command(pane_cfg.role)} && " if pane_cfg.role in ROLE_THEMES else ""
        command = command_with_baked_persona(pane_cfg.role, pane_cfg.command)
        text = launch_command_text(
            root=root,
            role=pane_cfg.role,
            instance=instance,
            think=think,
            max_items=max_items,
            command=command,
            backend_name=self.BACKEND_NAME,
            banner=banner,
        )
        # send is a single atomic write (A10 - no chunking needed); submit
        # is always its own separate send-key, mirroring both other
        # backends' type-then-submit split.
        await cmux_cli.send_text(
            workspace_ref=pane.workspace_ref, surface_ref=pane.surface_ref, text=text.rstrip("\n")
        )
        await cmux_cli.send_key(
            workspace_ref=pane.workspace_ref, surface_ref=pane.surface_ref, key="enter"
        )
        logger.info("Launched %s in role '%s' (cmux)", command, pane_cfg.role)

    async def _wait_ready(self, pane: CmuxPane) -> bool:
        deadline = time.monotonic() + CLAUDE_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            text = await cmux_cli.capture_pane(
                workspace_ref=pane.workspace_ref, surface_ref=pane.surface_ref
            )
            for line in text.split("\n"):
                if line.strip().startswith(CLAUDE_PROMPT_MARKER):
                    return True
            await asyncio.sleep(CLAUDE_READY_POLL_INTERVAL_SECONDS)
        return False

    async def _confirm_submitted(self, pane: CmuxPane, *, text: str) -> bool:
        probe = text.strip()
        if not probe:
            return True
        deadline = time.monotonic() + SUBMIT_CONFIRM_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            captured = await cmux_cli.capture_pane(
                workspace_ref=pane.workspace_ref, surface_ref=pane.surface_ref
            )
            if probe not in captured:
                return True
            await asyncio.sleep(SUBMIT_CONFIRM_POLL_INTERVAL_SECONDS)
        captured = await cmux_cli.capture_pane(
            workspace_ref=pane.workspace_ref, surface_ref=pane.surface_ref
        )
        return probe not in captured

    async def _prefill_role_command(self, role: str, pane: CmuxPane) -> None:
        prefix = role_prompt_prefix(role)
        if not prefix:
            return
        await self.send_role_prompt(role, pane, text=prefix, submit=False)

    # -- workspace build ------------------------------------------------------

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
    ) -> CmuxWindow:
        instance = str(uuid.uuid4())
        root_dir = resolve_root(root)
        workspace_ref = await cmux_cli.workspace_create(root_dir)
        _workspace_id, root_surface_ref = await self._locate_fresh_workspace(workspace_ref)
        root_pane = CmuxPane(surface_ref=root_surface_ref, workspace_ref=workspace_ref)

        if lazy:
            entry_pane_cfg = next(p for p in template.panes if p.role == template.entry_role)
            await self._launch_pane(
                root_pane,
                instance=instance,
                root=root,
                pane_cfg=entry_pane_cfg,
                think=think,
                max_items=max_items,
            )
            await self._prefill_role_command(entry_pane_cfg.role, root_pane)
            _write_state(
                instance,
                {
                    "auto_handoff": auto_handoff,
                    "lazy": True,
                    "template": template_name,
                    "run_doc": None,
                    "run_started": None,
                },
            )
            return CmuxWindow(workspace_ref=workspace_ref, instance=instance)

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
                instance=instance,
                root=root,
                pane_cfg=pane_cfg,
                think=think,
                max_items=max_items,
            )

        for pane_cfg in template.panes:
            await self._prefill_role_command(pane_cfg.role, panes_by_role[pane_cfg.role])

        _write_state(
            instance,
            {
                "auto_handoff": auto_handoff,
                "lazy": False,
                "template": template_name,
                "run_doc": None,
                "run_started": None,
            },
        )
        return CmuxWindow(workspace_ref=workspace_ref, instance=instance)

    # -- lookups ---------------------------------------------------------------

    async def find_workspace(self, marker: str) -> CmuxWindow | None:
        found = await self._find_workspace_ref_and_instance(marker)
        if found is None:
            return None
        workspace_ref, _workspace_id, instance = found
        return CmuxWindow(workspace_ref=workspace_ref, instance=instance)

    async def list_windows(self) -> list[CmuxWindow]:
        # cmux is only ever driven by explicit `workspace create` calls from
        # this backend - no cold-launch stray default workspace to clean up
        # the way iTerm2's cold-launch handling does (mirrors TmuxBackend).
        return []

    async def find_role_pane(
        self, *, marker: str, role: str, instance: str | None = None
    ) -> CmuxPane | None:
        resolved_instance = await self._resolve_instance(marker, instance)
        if resolved_instance is None:
            return None
        located = await self._find_by_instance(resolved_instance)
        if located is None:
            return None
        workspace_ref, workspace_id = located
        target_title = _pane_title(resolved_instance, role)
        for surface in await cmux_cli.surface_list(workspace_id):
            if surface.get("title") == target_title:
                return CmuxPane(surface_ref=surface["ref"], workspace_ref=workspace_ref)
        return None

    async def each_pane(
        self, *, marker: str, instance: str | None = None
    ) -> AsyncIterator[tuple[str, CmuxPane]]:
        resolved_instance = await self._resolve_instance(marker, instance)
        if resolved_instance is None:
            return
        located = await self._find_by_instance(resolved_instance)
        if located is None:
            return
        workspace_ref, workspace_id = located
        for surface in await cmux_cli.surface_list(workspace_id):
            parsed = _parse_title(surface.get("title") or "")
            if parsed and parsed[0] == resolved_instance:
                yield parsed[1], CmuxPane(surface_ref=surface["ref"], workspace_ref=workspace_ref)

    # -- activation --------------------------------------------------------------

    async def activate_window(self, window: CmuxWindow) -> None:
        await cmux_cli.workspace_select(window.workspace_ref)

    async def activate_pane(self, pane: CmuxPane) -> None:
        workspace_id = await self._workspace_id_for_ref(pane.workspace_ref)
        if workspace_id is None:
            return
        for surface in await cmux_cli.surface_list(workspace_id):
            if surface.get("ref") == pane.surface_ref:
                pane_ref = surface.get("pane_ref")
                if pane_ref:
                    await cmux_cli.focus_pane(workspace_ref=pane.workspace_ref, pane_ref=pane_ref)
                return

    async def close_window_if_empty(self, window: CmuxWindow) -> None:
        # list_windows() never returns anything for this backend, so
        # nothing calls this in practice - kept as a harmless no-op to
        # satisfy the interface (mirrors TmuxBackend).
        return None

    # -- prompt delivery ----------------------------------------------------------

    async def send_role_prompt(
        self, role: str, pane: CmuxPane, *, text: str, submit: bool
    ) -> None:
        ready = await self._wait_ready(pane)
        if not ready:
            logger.warning(
                "Gave up waiting for claude to be ready in role '%s' - "
                "skipping prompt send (cmux)",
                role,
            )
            return
        await cmux_cli.send_text(workspace_ref=pane.workspace_ref, surface_ref=pane.surface_ref, text=text)
        if not submit:
            return

        await asyncio.sleep(SUBMIT_KEYSTROKE_SETTLE_SECONDS)
        for attempt in range(1, SUBMIT_MAX_ATTEMPTS + 1):
            await cmux_cli.send_key(
                workspace_ref=pane.workspace_ref, surface_ref=pane.surface_ref, key="enter"
            )
            if await self._confirm_submitted(pane, text=text):
                return
            logger.warning(
                "Submit for role '%s' did not register on attempt %d/%d - "
                "retrying (cmux)",
                role,
                attempt,
                SUBMIT_MAX_ATTEMPTS,
            )
            await asyncio.sleep(SUBMIT_KEYSTROKE_SETTLE_SECONDS)
        logger.error(
            "Submit for role '%s' never registered after %d attempts - "
            "prompt is left prefilled; user will need to press Enter "
            "manually",
            role,
            SUBMIT_MAX_ATTEMPTS,
        )

    async def send_new(self, pane: CmuxPane) -> None:
        ready = await self._wait_ready(pane)
        if not ready:
            logger.warning("Gave up waiting for claude to be ready - skipping /new")
            return
        await cmux_cli.send_text(workspace_ref=pane.workspace_ref, surface_ref=pane.surface_ref, text="/new")
        await asyncio.sleep(SUBMIT_KEYSTROKE_SETTLE_SECONDS)
        await cmux_cli.send_key(workspace_ref=pane.workspace_ref, surface_ref=pane.surface_ref, key="enter")

    # -- state getters/setters -----------------------------------------------------

    async def get_auto_handoff(self, *, marker: str, instance: str | None = None) -> bool:
        resolved = await self._resolve_instance(marker, instance)
        if resolved is None:
            return False
        return bool(_read_state(resolved).get("auto_handoff", False))

    async def get_lazy(self, *, marker: str, instance: str | None = None) -> bool:
        resolved = await self._resolve_instance(marker, instance)
        if resolved is None:
            return False
        return bool(_read_state(resolved).get("lazy", False))

    async def get_template_name(
        self, *, marker: str, instance: str | None = None
    ) -> str | None:
        resolved = await self._resolve_instance(marker, instance)
        if resolved is None:
            return None
        return _read_state(resolved).get("template") or None

    async def get_run_doc(
        self, *, marker: str, instance: str | None = None
    ) -> tuple[str | None, float | None]:
        resolved = await self._resolve_instance(marker, instance)
        if resolved is None:
            return None, None
        state = _read_state(resolved)
        doc = state.get("run_doc") or None
        started = state.get("run_started")
        return doc, float(started) if started else None

    async def set_run_doc(
        self, *, marker: str, instance: str | None = None, doc: str, started_at: float
    ) -> None:
        resolved = await self._resolve_instance(marker, instance)
        if resolved is None:
            return
        state = _read_state(resolved)
        state["run_doc"] = doc
        state["run_started"] = started_at
        _write_state(resolved, state)

    # -- layout / reveal -----------------------------------------------------------

    async def split_pane(self, pane: CmuxPane, *, vertical: bool) -> CmuxPane:
        direction = "right" if vertical else "down"
        new_ref = await cmux_cli.new_split(
            direction, workspace_ref=pane.workspace_ref, surface_ref=pane.surface_ref
        )
        return CmuxPane(surface_ref=new_ref, workspace_ref=pane.workspace_ref)

    async def reveal_role(
        self,
        *,
        marker: str,
        instance: str,
        root: str,
        template: Template,
        role: str,
        source: CmuxPane,
    ) -> CmuxPane | None:
        # No largest-sibling selection here (unlike the other two backends):
        # cmux's surface.list carries no pane geometry (confirmed against
        # the spike's A7 field inventory), so there is nothing to compare -
        # split directly off the handoff source.
        pane_cfg = next((p for p in template.panes if p.role == role), None) or CANONICAL_PANES[role]
        new_pane = await self.split_pane(source, vertical=True)
        await self._launch_pane(
            new_pane,
            instance=instance,
            root=root,
            pane_cfg=pane_cfg,
            think=think_active(root, instance),
            max_items=DEFAULT_MAX_ITEMS,
        )
        return new_pane

    # -- watchdog --------------------------------------------------------------------

    async def check_pane_stall(
        self,
        pane: CmuxPane,
        *,
        role: str,
        previous: dict[str, Any] | None,
        now: float,
        stall_after_seconds: float,
    ) -> tuple[dict[str, Any], bool]:
        captured = await cmux_cli.capture_pane(
            workspace_ref=pane.workspace_ref, surface_ref=pane.surface_ref
        )
        text, ready = screen_signature(captured)
        return stall_decision(
            previous, text=text, ready=ready, now=now, stall_after_seconds=stall_after_seconds
        )
