"""iTerm2 Python API integration.

This is the only module that talks to ``iterm2`` directly. Everything else
in the package works with plain config objects and dicts, which keeps the
rest of the codebase testable without a running iTerm2 instance.
"""

from __future__ import annotations

import asyncio
import logging

import iterm2

from claudespace.config import Template
from claudespace.layouts import get_layout

logger = logging.getLogger(__name__)

# User-defined session variable used to tag panes so a later run can find
# an existing workspace without relying on window/tab titles (which the
# user is free to rename). Its value is the workspace "marker": the
# resolved absolute root path.
WORKSPACE_VAR = "user.workspaceLauncherWorkspace"
ROLE_VAR = "user.workspaceLauncherRole"

# Marker printed by claude's own input box once its TUI is ready to accept
# text - polled for after launch so the role prefill lands in claude's
# input rather than the shell that launched it (or an intervening dialog,
# e.g. the first-run "trust this folder" prompt).
CLAUDE_PROMPT_MARKER = "❯"

# Ceiling on how long to poll for claude's prompt before giving up on
# prefilling a given pane. If claude is stuck behind a dialog (e.g. trust
# prompt) past this point, prefill is skipped for that pane - the user
# still gets a normal, unprefixed session to interact with once they clear
# the dialog themselves.
CLAUDE_READY_TIMEOUT_SECONDS = 15
CLAUDE_READY_POLL_INTERVAL_SECONDS = 0.25


async def _wait_for_claude_prompt(session: "iterm2.Session") -> bool:
    """Poll ``session``'s screen until claude's input prompt appears.

    Returns ``True`` once seen, or ``False`` if ``CLAUDE_READY_TIMEOUT_SECONDS``
    elapses first (e.g. the session is stuck on a trust-folder dialog).
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + CLAUDE_READY_TIMEOUT_SECONDS
    while loop.time() < deadline:
        contents = await session.async_get_screen_contents()
        for i in range(contents.number_of_lines):
            if contents.line(i).string.strip().startswith(CLAUDE_PROMPT_MARKER):
                return True
        await asyncio.sleep(CLAUDE_READY_POLL_INTERVAL_SECONDS)
    return False


async def _prefill_role_command(role: str, session: "iterm2.Session") -> None:
    """Wait for claude to be ready in ``session``, then prefill its input."""
    ready = await _wait_for_claude_prompt(session)
    if not ready:
        logger.warning(
            "Gave up waiting for claude to be ready in role '%s' - skipping "
            "command prefill",
            role,
        )
        return
    await session.async_send_text(f"/{role} ")


async def find_workspace_window(app: iterm2.App, marker: str) -> iterm2.Window | None:
    """Return the window tagged with ``marker``, if one exists.

    Scans every session of every tab of every window for the workspace
    marker variable. Returns the first match's window - a workspace is
    treated as a single window, so any tagged session identifies it.
    """
    for window in app.windows:
        for tab in window.tabs:
            for session in tab.sessions:
                value = await session.async_get_variable(WORKSPACE_VAR)
                if value == marker:
                    return window
    return None


async def build_workspace(
    connection: iterm2.Connection,
    *,
    marker: str,
    root: str,
    template: Template,
) -> iterm2.Window:
    """Create a new window, lay out its panes, and launch each pane's command.

    Every pane is tagged with ``WORKSPACE_VAR``/``ROLE_VAR`` so future runs
    can detect this workspace and identify individual panes. ``marker``
    identifies the workspace for dedup purposes - the resolved root path.
    """
    layout = get_layout(template.layout)

    configured_roles = {pane.role for pane in template.panes}
    if configured_roles != layout.roles:
        raise ValueError(
            f"Template panes {sorted(configured_roles)} do not match "
            f"layout '{template.layout}' roles {sorted(layout.roles)}"
        )

    window = await iterm2.Window.async_create(connection)
    if window is None:
        raise RuntimeError("iTerm2 refused to create a new window")

    root_session = window.current_tab.current_session
    sessions_by_role = await layout.build(root_session)

    for pane in template.panes:
        session = sessions_by_role[pane.role]
        await session.async_set_variable(WORKSPACE_VAR, marker)
        await session.async_set_variable(ROLE_VAR, pane.role)
        await session.async_send_text(f"cd {root} && {pane.command}\n")
        logger.info("Launched %s (%s) in role '%s'", pane.command, root, pane.role)

    await asyncio.gather(
        *(
            _prefill_role_command(pane.role, sessions_by_role[pane.role])
            for pane in template.panes
        )
    )

    return window


async def activate_window(window: iterm2.Window) -> None:
    """Bring an existing workspace window to the foreground."""
    await window.async_activate()
