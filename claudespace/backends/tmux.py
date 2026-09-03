"""tmux ``TerminalBackend`` implementation (AD3-AD6).

The workspace is a **detached tmux session**, built and driven entirely
through the ``tmux`` CLI (``backends/tmux_cli.py``) - no terminal needs to
be scriptable, or even running, for any of this to work. A terminal
("viewer", default Ghostty - see ``utils.launch_viewer``) is only spawned to
make the session *visible*; closing it doesn't end the workspace.

Per-pane state lives on the pane itself as tmux user options (``@cs_*``),
the direct equivalent of iTerm2's session user-variables - no file store,
no liveness cross-check (AD4): a pane that no longer exists simply doesn't
appear in ``tmux list-panes`` output, so "does this pane still exist" is
free. Watchdog stall detection reuses the exact same content-diff algorithm
iTerm2 uses (``backends/common.py``'s ``screen_signature``/
``stall_decision``), fed from ``tmux capture-pane`` instead of iTerm2's
screen-content API - full fidelity, not a reduced crash-only signal (AD6).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from claudespace import utils
from claudespace.backends import tmux_cli, tmux_persist
from claudespace.backends.base import BackendUnavailableError, TerminalBackend
from claudespace.backends.common import (
    CLAUDE_READY_POLL_INTERVAL_SECONDS,
    CLAUDE_READY_TIMEOUT_SECONDS,
    DEFAULT_MAX_ITEMS,
    SUBMIT_CONFIRM_POLL_INTERVAL_SECONDS,
    SUBMIT_CONFIRM_TIMEOUT_SECONDS,
    SUBMIT_KEYSTROKE_SETTLE_SECONDS,
    SUBMIT_MAX_ATTEMPTS,
    CLAUDE_PROMPT_MARKER,
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
    "The tmux backend is macOS-only, matching claudespace's current platform "
    "scope."
)

TMUX_NOT_FOUND_HELP = (
    "tmux is required for the tmux backend and was not found on PATH.\n"
    "Install it (brew install tmux) or set terminal.backend = \"iterm2\" in "
    "~/.config/claudespace/config.toml."
)


# Bounded window to wait for continuum's *backgrounded* autorestore to
# finish (AD12's "autorestore races the viewer launch" edge case) before
# trusting a "no matching pane" result as "nothing to attach to, build
# fresh." Continuum's own restore script sleeps 1s then runs resurrect's
# restore - a handful of panes typically finishes well under this. Bounded,
# not indefinite: a workspace with no saved snapshot returns almost
# immediately (rehydrate() still touches the marker even with nothing to
# do - see its docstring), and a genuinely stuck restore just means this
# attaches/builds after the full wait rather than hanging forever.
AUTORESTORE_WAIT_SECONDS = 8.0
AUTORESTORE_POLL_INTERVAL_SECONDS = 0.25


_SLUG_MAX_LEN = 30


def _slugify_run_doc(doc: str) -> str:
    """A short, tmux-session-name-safe slug naming the current run, from a
    ``set_run_doc`` artifact path (e.g.
    ``docs/research/2026-09-03-fix-kitchen-heat-link.md``) or free-text
    backlog description (conductor's own dispatches). Used to rename the
    session so ``claudespace --restore`` shows what's actually being worked
    on instead of an opaque instance id.

    tmux session names can't contain ``.`` or ``:``; kept conservative
    (alphanumeric + hyphens only) rather than relying on exactly which
    characters tmux happens to tolerate.
    """
    base = doc.strip().rsplit("/", 1)[-1]
    base = re.sub(r"\.md$", "", base, flags=re.IGNORECASE)
    base = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", base)  # strip a leading date
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return slug[:_SLUG_MAX_LEN].rstrip("-") or "run"


def _version_too_old_help(found: str) -> str:
    major, minor = tmux_cli.MIN_TMUX_VERSION
    return (
        f"tmux {found!r} is older than the version the tmux backend needs "
        f"(>= {major}.{minor}, for pane-scoped user options and "
        "capture-pane). Upgrade tmux (brew upgrade tmux) or set "
        "terminal.backend = \"iterm2\"."
    )


# Every pane user option this backend reads/writes, in one place so
# `_matching_rows`'s single bulk `list-panes` call always asks for exactly
# what every getter/setter below expects.
_PANE_FIELDS: tuple[str, ...] = (
    "pane_id",
    "session_name",
    "@cs_workspace",
    "@cs_instance",
    "@cs_role",
    "@cs_auto_handoff",
    "@cs_lazy",
    "@cs_template",
    "@cs_run_doc",
    "@cs_run_started",
)


@dataclass(frozen=True, slots=True)
class TmuxPane:
    """Opaque pane handle: a pane id is unique across the whole tmux
    server for the pane's life, but its session name is kept alongside it
    since several operations (largest-sibling, viewer attach) are
    session-scoped."""

    pane_id: str
    session: str


@dataclass(frozen=True, slots=True)
class TmuxWindow:
    """One claudespace workspace = one tmux session (AD3) - "window" here
    names the same concept the rest of the codebase uses for "the thing
    build_workspace hands back and activate_window brings to front"."""

    session: str
    instance: str


class TmuxBackend(TerminalBackend):
    """``TerminalBackend`` implementation driving a detached tmux server."""

    # See ItermBackend.BACKEND_NAME's docstring - exported into every
    # pane's environment so a later claudespace-handoff/claudespace-msg
    # process (its own separate invocation, with no visibility into how
    # this workspace's own `claudespace --tmux`/config.toml resolved its
    # backend) resolves the same one.
    BACKEND_NAME = "tmux"

    def __init__(
        self,
        *,
        viewer: str = "ghostty",
        persist: bool = True,
        persist_interval_minutes: int = 15,
    ) -> None:
        self._viewer = viewer
        self._persist = persist
        self._persist_interval_minutes = persist_interval_minutes

    # -- preflight / run ----------------------------------------------------

    def run(self, entrypoint: Callable[[TerminalBackend], Awaitable[None]]) -> None:
        if sys.platform != "darwin":
            logger.error("%s", NOT_MACOS_HELP)
            sys.exit(1)
        if not tmux_cli.is_tmux_available():
            logger.error("%s", TMUX_NOT_FOUND_HELP)
            sys.exit(1)

        # Cheap and idempotent (Increment 2, AD12) - keeps the private
        # config in sync with current persist/interval settings even if
        # the user never re-ran claudespace-sync-assets. Must happen before
        # `entrypoint` runs: its first real tmux command is what may start
        # a fresh server and load this file (-V below never touches a
        # server, so it doesn't need this first, but there's no reason to
        # delay it either).
        tmux_persist.write_conf(
            persist=self._persist, interval_minutes=self._persist_interval_minutes
        )

        try:
            raw_version = asyncio.run(tmux_cli.version())
        except BackendUnavailableError as exc:
            logger.error("tmux preflight timed out: %s", exc)
            sys.exit(1)
        except tmux_cli.TmuxCommandError as exc:
            logger.error("Could not run 'tmux -V': %s", exc)
            sys.exit(1)

        if tmux_cli.parse_version(raw_version) < tmux_cli.MIN_TMUX_VERSION:
            logger.error("%s", _version_too_old_help(raw_version))
            sys.exit(1)

        asyncio.run(entrypoint(self))

    # -- naming / state helpers ----------------------------------------------

    @staticmethod
    def _session_name(marker: str, instance: str) -> str:
        digest = hashlib.sha1(marker.encode("utf-8")).hexdigest()[:8]
        return f"cs-{digest}-{instance[:8]}"

    @staticmethod
    def _session_prefix(session: str) -> str:
        """The stable ``cs-<hash8>`` part of a session name, whatever
        currently follows it (the original instance suffix, or an
        already-applied task slug) - what ``rename_session_for_task``
        re-derives the target name from, so repeated renames of the same
        session never lose the marker's own identity prefix."""
        parts = session.split("-", 2)
        return "-".join(parts[:2]) if len(parts) >= 2 else session

    async def _matching_rows(
        self, marker: str, instance: str | None
    ) -> list[dict[str, str]]:
        rows = await tmux_cli.list_panes_all(_PANE_FIELDS)
        matches = []
        for row in rows:
            if row.get("@cs_workspace") != marker:
                continue
            if instance is not None and row.get("@cs_instance") != instance:
                continue
            matches.append(row)
        return matches

    # -- pane launch / readiness / prompt delivery ---------------------------

    async def _launch_pane(
        self,
        pane: TmuxPane,
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
        for key, value in (
            ("@cs_workspace", marker),
            ("@cs_instance", instance),
            ("@cs_role", pane_cfg.role),
            ("@cs_auto_handoff", "1" if auto_handoff else "0"),
            ("@cs_lazy", "1" if lazy else "0"),
            ("@cs_template", template_name),
        ):
            await tmux_cli.set_pane_option(pane.pane_id, key, value)

        await tmux_cli.pane_border_title(pane.pane_id, pane_cfg.role)

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
        # send-keys -l types literally and never submits on its own; the
        # trailing newline launch_command_text always ends with is stripped
        # in favor of an explicit Enter, mirroring the type-then-submit
        # split every launch/prompt path in this backend uses.
        await tmux_cli.send_keys_literal(pane.pane_id, text.rstrip("\n"))
        await tmux_cli.send_enter(pane.pane_id)
        logger.info("Launched %s in role '%s' (tmux)", command, pane_cfg.role)

    async def _wait_ready(self, pane: TmuxPane) -> bool:
        deadline = time.monotonic() + CLAUDE_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            text = await tmux_cli.capture_pane(pane.pane_id)
            for line in text.split("\n"):
                if line.strip().startswith(CLAUDE_PROMPT_MARKER):
                    return True
            await asyncio.sleep(CLAUDE_READY_POLL_INTERVAL_SECONDS)
        return False

    async def _confirm_submitted(self, pane: TmuxPane, *, text: str) -> bool:
        probe = text.strip()
        if not probe:
            return True
        deadline = time.monotonic() + SUBMIT_CONFIRM_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            captured = await tmux_cli.capture_pane(pane.pane_id)
            if probe not in captured:
                return True
            await asyncio.sleep(SUBMIT_CONFIRM_POLL_INTERVAL_SECONDS)
        captured = await tmux_cli.capture_pane(pane.pane_id)
        return probe not in captured

    async def _prefill_role_command(self, role: str, pane: TmuxPane) -> None:
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
    ) -> TmuxWindow:
        instance = str(uuid.uuid4())
        session = self._session_name(marker, instance)
        root_pane_id = await tmux_cli.new_session(session, cwd=resolve_root(root))
        root_pane = TmuxPane(pane_id=root_pane_id, session=session)

        # Edge Cases: Theming - a pane border showing its role title is
        # tmux's cosmetic stand-in for iTerm2's tab color/badge, since
        # `select-pane -T` (pane_border_title) only renders when these are
        # on. Best-effort: a user's own tmux config may already set these
        # differently, and that's fine - role identity still comes through
        # `claude --name` in the title either way.
        await tmux_cli.set_session_option(session, "pane-border-status", "top")
        await tmux_cli.set_session_option(session, "pane-border-format", "#{pane_title}")

        # `mouse` is off by default in stock tmux, so clicking a pane does
        # nothing - the only way to switch is the prefix key, which is not
        # how iTerm2 users expect pane switching to work. `focus-events`
        # (also off by default) is what lets the program running inside a
        # pane (claude's TUI included) know when it gains/loses focus at
        # all - without it every pane looks permanently unfocused to the
        # app inside, regardless of which one is actually active.
        await tmux_cli.set_session_option(session, "mouse", "on")
        await tmux_cli.set_session_option(session, "focus-events", "on")

        if lazy:
            entry_pane_cfg = next(p for p in template.panes if p.role == template.entry_role)
            await self._launch_pane(
                root_pane,
                marker=marker,
                instance=instance,
                root=root,
                template_name=template_name,
                pane_cfg=entry_pane_cfg,
                auto_handoff=auto_handoff,
                lazy=True,
                think=think,
                max_items=max_items,
            )
            await self._prefill_role_command(entry_pane_cfg.role, root_pane)
            return TmuxWindow(session=session, instance=instance)

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

        return TmuxWindow(session=session, instance=instance)

    # -- lookups ---------------------------------------------------------------

    async def _await_autorestore_if_needed(self) -> None:
        """Give continuum's backgrounded autorestore a bounded chance to
        finish before any lookup trusts an empty result (AD12).

        Only relevant when the server *doesn't already exist* - an
        already-running server has already gone through continuum's
        startup check (or persistence is off and never will), so there is
        nothing in flight to wait for. tmux's default ``exit-empty``
        means "no server" and "not yet autorestored" are indistinguishable
        from here, which is exactly the case this needs to cover.

        A read-only lookup (``list-panes``/``has-session``) does **not**
        itself start a tmux server if none exists - only a command like
        ``new-session`` does, and the private config (with continuum's
        autorestore check) only loads at that moment. So this boots the
        server itself, via a disposable probe session, purely to make that
        loading happen deterministically *before* any real lookup runs -
        otherwise `find_workspace` finding nothing would be genuinely
        ambiguous between "no server" and "server just started, restore
        still in flight."
        """
        if not self._persist:
            return
        if await tmux_cli.server_running():
            return
        baseline = tmux_persist.marker_mtime()
        probe = f"cs-probe-{uuid.uuid4().hex[:8]}"
        try:
            await tmux_cli.new_session(probe)
        except Exception:
            logger.warning("Could not boot the tmux server to check for an autorestore", exc_info=True)
            return
        try:
            deadline = time.monotonic() + AUTORESTORE_WAIT_SECONDS
            while time.monotonic() < deadline:
                if tmux_persist.marker_mtime() != baseline:
                    return
                await asyncio.sleep(AUTORESTORE_POLL_INTERVAL_SECONDS)
        finally:
            await tmux_cli.kill_session(probe)

    async def find_workspace(self, marker: str) -> TmuxWindow | None:
        await self._await_autorestore_if_needed()
        rows = await self._matching_rows(marker, None)
        if not rows:
            return None
        return TmuxWindow(
            session=rows[0]["session_name"], instance=rows[0].get("@cs_instance", "")
        )

    async def list_all_workspaces(self) -> list[dict[str, Any]]:
        """Every claudespace-tagged session currently known to this
        backend's server, grouped by session, for ``claudespace --restore``.

        Waits for an in-flight autorestore first, same as ``find_workspace``
        - a session that only just autorestored should still show up here,
        not be missed because it hadn't finished settling yet.
        """
        await self._await_autorestore_if_needed()
        rows = await tmux_cli.list_panes_all(_PANE_FIELDS)
        by_session: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not row.get("@cs_workspace"):
                continue
            session = row["session_name"]
            entry = by_session.setdefault(
                session,
                {
                    "session": session,
                    "workspace": row["@cs_workspace"],
                    "instance": row.get("@cs_instance", ""),
                    "roles": [],
                },
            )
            role = row.get("@cs_role")
            if role:
                entry["roles"].append(role)
        return sorted(by_session.values(), key=lambda e: e["session"])

    async def list_windows(self) -> list[TmuxWindow]:
        # No terminal needs to be running for a tmux workspace to exist
        # (AD3) - there is no "stray default window from a cold app
        # launch" to clean up the way iTerm2's cold-launch handling does,
        # so this has nothing to report.
        return []

    async def find_role_pane(
        self, *, marker: str, role: str, instance: str | None = None
    ) -> TmuxPane | None:
        for row in await self._matching_rows(marker, instance):
            if row.get("@cs_role") == role:
                return TmuxPane(pane_id=row["pane_id"], session=row["session_name"])
        return None

    async def each_pane(
        self, *, marker: str, instance: str | None = None
    ) -> AsyncIterator[tuple[str, TmuxPane]]:
        for row in await self._matching_rows(marker, instance):
            role = row.get("@cs_role")
            if role:
                yield role, TmuxPane(pane_id=row["pane_id"], session=row["session_name"])

    # -- activation --------------------------------------------------------------

    async def activate_window(self, window: TmuxWindow) -> None:
        clients = await tmux_cli.list_clients(window.session)
        if clients:
            return
        try:
            utils.launch_viewer(window.session, viewer=self._viewer)
        except Exception as exc:
            # Never fatal (Error Handling / Edge Cases: Viewer closed/
            # detached mid-run): the tmux session itself is already built
            # and running detached, untouched by a failed viewer spawn -
            # name the viewer and the manual fallback instead of crashing
            # the whole command over what is, at worst, a visibility
            # problem.
            logger.error(
                "Could not launch viewer '%s' for tmux session '%s' (%s). "
                "The workspace is still running, detached - reattach with: "
                "tmux attach -t %s\n"
                "Change the viewer via [terminal.tmux] viewer in "
                "~/.config/claudespace/config.toml.",
                self._viewer,
                window.session,
                exc,
                window.session,
            )

    async def activate_pane(self, pane: TmuxPane) -> None:
        await tmux_cli.select_pane(pane.pane_id)

    async def close_window_if_empty(self, window: TmuxWindow) -> None:
        # list_windows() never returns anything for this backend, so
        # nothing calls this in practice; kept as a harmless no-op to
        # satisfy the interface rather than special-cased away.
        return None

    # -- prompt delivery ----------------------------------------------------------

    async def send_role_prompt(
        self, role: str, pane: TmuxPane, *, text: str, submit: bool
    ) -> None:
        ready = await self._wait_ready(pane)
        if not ready:
            logger.warning(
                "Gave up waiting for claude to be ready in role '%s' - "
                "skipping prompt send (tmux)",
                role,
            )
            return
        # Paste rather than send-keys: a handoff prompt can carry a large
        # inline dispatch, and send-keys -l drops the leading portion of a
        # multi-KB burst (see tmux_cli.send_text_paste).
        await tmux_cli.send_text_paste(pane.pane_id, text)
        if not submit:
            return

        await asyncio.sleep(SUBMIT_KEYSTROKE_SETTLE_SECONDS)
        for attempt in range(1, SUBMIT_MAX_ATTEMPTS + 1):
            await tmux_cli.send_enter(pane.pane_id)
            if await self._confirm_submitted(pane, text=text):
                return
            logger.warning(
                "Submit for role '%s' did not register on attempt %d/%d - "
                "retrying (tmux)",
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

    async def send_new(self, pane: TmuxPane) -> None:
        ready = await self._wait_ready(pane)
        if not ready:
            logger.warning("Gave up waiting for claude to be ready - skipping /new")
            return
        await tmux_cli.send_keys_literal(pane.pane_id, "/new")
        await asyncio.sleep(SUBMIT_KEYSTROKE_SETTLE_SECONDS)
        await tmux_cli.send_enter(pane.pane_id)

    # -- state getters/setters -----------------------------------------------------

    async def get_auto_handoff(
        self, *, marker: str, instance: str | None = None
    ) -> bool:
        rows = await self._matching_rows(marker, instance)
        return bool(rows) and rows[0].get("@cs_auto_handoff") == "1"

    async def get_lazy(self, *, marker: str, instance: str | None = None) -> bool:
        rows = await self._matching_rows(marker, instance)
        return bool(rows) and rows[0].get("@cs_lazy") == "1"

    async def get_template_name(
        self, *, marker: str, instance: str | None = None
    ) -> str | None:
        rows = await self._matching_rows(marker, instance)
        if not rows:
            return None
        return rows[0].get("@cs_template") or None

    async def get_run_doc(
        self, *, marker: str, instance: str | None = None
    ) -> tuple[str | None, float | None]:
        rows = await self._matching_rows(marker, instance)
        if not rows:
            return None, None
        doc = rows[0].get("@cs_run_doc") or None
        started = rows[0].get("@cs_run_started")
        return doc, float(started) if started else None

    async def set_run_doc(
        self, *, marker: str, instance: str | None = None, doc: str, started_at: float
    ) -> None:
        rows = await self._matching_rows(marker, instance)
        for row in rows:
            await tmux_cli.set_pane_option(row["pane_id"], "@cs_run_doc", doc)
            await tmux_cli.set_pane_option(
                row["pane_id"], "@cs_run_started", str(started_at)
            )
        await self._rename_sessions_for_task(rows, doc)

    async def _rename_sessions_for_task(
        self, rows: list[dict[str, str]], doc: str
    ) -> None:
        """Rename every distinct session in ``rows`` to reflect ``doc`` -
        makes ``claudespace --restore``/`tmux list-sessions` show what's
        actually being worked on instead of an opaque instance id. Renamed
        again on the next ``set_run_doc`` (a fresh topic starting in an
        already-used workspace), so the name tracks the *current* task, not
        just the first one. Best-effort throughout: a rename failing never
        blocks the state write it follows.
        """
        slug = _slugify_run_doc(doc)
        sessions = {row["session_name"]: row.get("@cs_instance", "") for row in rows}
        for session, instance in sessions.items():
            target = f"{self._session_prefix(session)}-{slug}"
            if target == session:
                continue
            renamed = await tmux_cli.rename_session(session, target)
            if not renamed and instance:
                # Target name already taken by an unrelated session -
                # disambiguate with a short instance suffix rather than
                # silently keeping the old (equally opaque) name.
                await tmux_cli.rename_session(session, f"{target}-{instance[:4]}")

    # -- layout / reveal -----------------------------------------------------------

    async def split_pane(self, pane: TmuxPane, *, vertical: bool) -> TmuxPane:
        new_id = await tmux_cli.split_window(pane.pane_id, vertical=vertical, session=pane.session)
        return TmuxPane(pane_id=new_id, session=pane.session)

    async def _largest_sibling(self, source: TmuxPane) -> TmuxPane:
        rows = await tmux_cli.list_panes(
            source.session, ("pane_id", "pane_width", "pane_height")
        )
        if not rows:
            return source

        def _area(row: dict[str, str]) -> float:
            return (int(row["pane_width"]) / 2) * int(row["pane_height"])

        best = max(rows, key=_area)
        return TmuxPane(pane_id=best["pane_id"], session=source.session)

    async def reveal_role(
        self,
        *,
        marker: str,
        instance: str,
        root: str,
        template: Template,
        role: str,
        source: TmuxPane,
    ) -> TmuxPane | None:
        pane_cfg = next((p for p in template.panes if p.role == role), None) or CANONICAL_PANES[role]
        auto_handoff = await self.get_auto_handoff(marker=marker, instance=instance)
        template_name = await self.get_template_name(marker=marker, instance=instance) or ""

        split_target = await self._largest_sibling(source)
        width, height = await tmux_cli.pane_dims(split_target.pane_id)
        vertical = (width / 2) >= height

        new_pane = await self.split_pane(split_target, vertical=vertical)
        await self._launch_pane(
            new_pane,
            marker=marker,
            instance=instance,
            root=root,
            template_name=template_name,
            pane_cfg=pane_cfg,
            auto_handoff=auto_handoff,
            lazy=True,
            think=think_active(root, instance),
            max_items=DEFAULT_MAX_ITEMS,
        )
        return new_pane

    # -- watchdog --------------------------------------------------------------------

    async def check_pane_stall(
        self,
        pane: TmuxPane,
        *,
        role: str,
        previous: dict[str, Any] | None,
        now: float,
        stall_after_seconds: float,
    ) -> tuple[dict[str, Any], bool]:
        captured = await tmux_cli.capture_pane(pane.pane_id)
        text, ready = screen_signature(captured)
        return stall_decision(
            previous, text=text, ready=ready, now=now, stall_after_seconds=stall_after_seconds
        )
