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

# Whether forward (success) handoffs auto-submit into the next pane or only
# prefill it. Backward (blocked/rejected) handoffs always prefill-only,
# regardless of this setting - see handoff.py.
AUTO_HANDOFF_VAR = "user.workspaceLauncherAutoHandoff"

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
    await send_role_prompt(role, session, text=f"/{role} ", submit=False)


async def send_role_prompt(
    role: str, session: "iterm2.Session", *, text: str, submit: bool
) -> None:
    """Wait for claude to be ready in ``session``, then type ``text`` into it.

    ``submit`` controls whether the input is submitted afterwards - ``False``
    leaves the text sitting in the input box for the user to review and
    press enter themselves, ``True`` submits it immediately. Used both for
    the initial role-command prefill at workspace build time and for
    pipeline handoffs between panes.

    The submit keystroke is sent as its own ``async_send_text`` call rather
    than appended to ``text`` - claude's TUI does not treat a trailing "\\n"
    within the same call as pressing enter, so appending it silently leaves
    the prompt typed but unsubmitted.
    """
    ready = await _wait_for_claude_prompt(session)
    if not ready:
        logger.warning(
            "Gave up waiting for claude to be ready in role '%s' - skipping "
            "prompt send",
            role,
        )
        return
    await session.async_send_text(text)
    if submit:
        await session.async_send_text("\r")


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
    auto_handoff: bool = False,
) -> iterm2.Window:
    """Create a new window, lay out its panes, and launch each pane's command.

    Every pane is tagged with ``WORKSPACE_VAR``/``ROLE_VAR`` so future runs
    can detect this workspace and identify individual panes. ``marker``
    identifies the workspace for dedup purposes - the resolved root path.
    ``auto_handoff`` is stored on every pane so ``handoff.py`` can look it up
    from any one of them without needing to enumerate the whole window.
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
        await session.async_set_variable(AUTO_HANDOFF_VAR, auto_handoff)
        await session.async_send_text(
            f"cd {root} && export CLAUDESPACE_ROOT={root} && "
            f"export CLAUDESPACE_ROLE={pane.role} && {pane.command}\n"
        )
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


async def activate_session(session: "iterm2.Session") -> None:
    """Select ``session``'s tab and focus it, so the active-pane highlight
    follows a handoff to its destination pane instead of staying on
    whichever pane the user last had focused.
    """
    await session.async_activate()


async def find_role_session(
    app: iterm2.App, *, marker: str, role: str
) -> iterm2.Session | None:
    """Find the session tagged with ``role`` inside the workspace ``marker``.

    Used by ``handoff.py`` to locate the destination pane for a pipeline
    handoff. Returns ``None`` if the workspace or role isn't found (e.g. the
    window was closed, or a template without that role was used).
    """
    for window in app.windows:
        for tab in window.tabs:
            for session in tab.sessions:
                workspace_value = await session.async_get_variable(WORKSPACE_VAR)
                if workspace_value != marker:
                    continue
                role_value = await session.async_get_variable(ROLE_VAR)
                if role_value == role:
                    return session
    return None


async def get_auto_handoff(app: iterm2.App, *, marker: str) -> bool:
    """Read the auto-handoff toggle for the workspace tagged ``marker``.

    Defaults to ``False`` (prefill-only) if the workspace can't be found.
    """
    for window in app.windows:
        for tab in window.tabs:
            for session in tab.sessions:
                workspace_value = await session.async_get_variable(WORKSPACE_VAR)
                if workspace_value == marker:
                    value = await session.async_get_variable(AUTO_HANDOFF_VAR)
                    return bool(value)
    return False
