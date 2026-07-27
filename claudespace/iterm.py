"""iTerm2 Python API integration.

This is the only module that talks to ``iterm2`` directly. Everything else
in the package works with plain config objects and dicts, which keeps the
rest of the codebase testable without a running iTerm2 instance.
"""

from __future__ import annotations

import asyncio
import logging

import iterm2

from claudespace.config import PaneConfig, Template
from claudespace.layouts import get_layout
from claudespace.themes import ROLE_THEMES, banner_command, build_role_profile

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

# Whether this workspace was built with --lazy, i.e. non-entry panes were
# never launched at build time and must be revealed (split + launched) by
# handoff.py the first time a pipeline handoff targets them. Read by
# handoff.py to decide whether a missing destination pane means "reveal it"
# (lazy) vs. "the window was closed" (non-lazy - see find_role_session).
LAZY_VAR = "user.workspaceLauncherLazy"

# The template name the workspace was built with, so a later process (e.g.
# handoff.py revealing a lazy pane) can look the template back up to find
# the destination role's command - CLAUDESPACE_ROLE/ROOT env vars alone
# don't carry this.
TEMPLATE_VAR = "user.workspaceLauncherTemplate"

# The doc path (a done-marker's contents) that identifies the pipeline run
# currently occupying this workspace, and the unix timestamp it started at -
# both set on every pane the moment a researcher.done kicks off a run. Used
# by handoff.py to detect when a fresh researcher.done names a *different*
# doc, meaning a new topic is starting in an already-used workspace. Unset
# until the first run's researcher hands off.
RUN_DOC_VAR = "user.workspaceLauncherRunDoc"
RUN_STARTED_VAR = "user.workspaceLauncherRunStarted"

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


async def _launch_pane(
    session: "iterm2.Session",
    *,
    marker: str,
    root: str,
    template_name: str,
    pane: PaneConfig,
    auto_handoff: bool,
    lazy: bool,
) -> None:
    """Tag ``session`` for ``pane``'s role and launch its command in it.

    Shared by eager ``build_workspace`` (all panes at once) and lazy
    reveal (one pane at a time, from ``handoff.py``) - both need identical
    tagging/theming/launch behavior, just triggered at different times.
    """
    await session.async_set_variable(WORKSPACE_VAR, marker)
    await session.async_set_variable(ROLE_VAR, pane.role)
    await session.async_set_variable(AUTO_HANDOFF_VAR, auto_handoff)
    await session.async_set_variable(LAZY_VAR, lazy)
    await session.async_set_variable(TEMPLATE_VAR, template_name)
    banner = ""
    if pane.role in ROLE_THEMES:
        await session.async_set_profile_properties(build_role_profile(pane.role))
        banner = f"{banner_command(pane.role)} && "
    await session.async_send_text(
        f"cd {root} && export CLAUDESPACE_ROOT={root} && "
        f"export CLAUDESPACE_ROLE={pane.role} && {banner}{pane.command}\n"
    )
    logger.info("Launched %s (%s) in role '%s'", pane.command, root, pane.role)


async def build_workspace(
    connection: iterm2.Connection,
    *,
    marker: str,
    root: str,
    template_name: str,
    template: Template,
    auto_handoff: bool = False,
    lazy: bool = False,
) -> iterm2.Window:
    """Create a new window and launch either every pane or just the entry pane.

    Every pane is tagged with ``WORKSPACE_VAR``/``ROLE_VAR`` so future runs
    can detect this workspace and identify individual panes. ``marker``
    identifies the workspace for dedup purposes - the resolved root path.
    ``auto_handoff``/``lazy``/``template_name`` are stored on every pane
    launched so ``handoff.py`` can look them up from any one of them
    without needing to enumerate the whole window.

    In lazy mode (``lazy=True``), only ``template.entry_role``'s pane is
    launched here, in the window's single starting session - no splitting
    happens yet, so no other pane exists at all (not even empty) until
    ``handoff.py`` reveals a destination role by splitting it directly off
    of whichever pane just handed off to it (see ``reveal_role``). This
    means the template's ``layout`` (a fixed grid, see ``layouts.py``) is
    irrelevant in lazy mode - it only governs eager mode's up-front grid.
    """
    window = await iterm2.Window.async_create(connection)
    if window is None:
        raise RuntimeError("iTerm2 refused to create a new window")

    root_session = window.current_tab.current_session

    if lazy:
        entry_pane = next(p for p in template.panes if p.role == template.entry_role)
        await _launch_pane(
            root_session,
            marker=marker,
            root=root,
            template_name=template_name,
            pane=entry_pane,
            auto_handoff=auto_handoff,
            lazy=True,
        )
        await _prefill_role_command(entry_pane.role, root_session)
        return window

    layout = get_layout(template.layout)

    configured_roles = {pane.role for pane in template.panes}
    if configured_roles != layout.roles:
        raise ValueError(
            f"Template panes {sorted(configured_roles)} do not match "
            f"layout '{template.layout}' roles {sorted(layout.roles)}"
        )

    sessions_by_role = await layout.build(root_session)

    for pane in template.panes:
        await _launch_pane(
            sessions_by_role[pane.role],
            marker=marker,
            root=root,
            template_name=template_name,
            pane=pane,
            auto_handoff=auto_handoff,
            lazy=False,
        )

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


async def close_window_if_empty(window: iterm2.Window) -> None:
    """Close ``window`` if none of its sessions are tagged as a workspace pane.

    Used to clean up the default empty window iTerm2 opens on a cold
    launch (see ``workspace.py``'s ``just_launched_iterm`` handling) - it
    isn't a workspace of ours, so it's stray chrome rather than something a
    user was using. Checking ``WORKSPACE_VAR`` rather than just closing the
    window unconditionally guards against the (unlikely but possible) case
    where the user typed something into it in the brief window between
    launch and our workspace window appearing.
    """
    for tab in window.tabs:
        for session in tab.sessions:
            if await session.async_get_variable(WORKSPACE_VAR):
                return
    await window.async_close(force=True)


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


async def get_lazy(app: iterm2.App, *, marker: str) -> bool:
    """Read the ``--lazy`` toggle for the workspace tagged ``marker``.

    Defaults to ``False`` if the workspace can't be found.
    """
    for window in app.windows:
        for tab in window.tabs:
            for session in tab.sessions:
                workspace_value = await session.async_get_variable(WORKSPACE_VAR)
                if workspace_value == marker:
                    value = await session.async_get_variable(LAZY_VAR)
                    return bool(value)
    return False


async def get_template_name(app: iterm2.App, *, marker: str) -> str | None:
    """Read the template name the workspace tagged ``marker`` was built with.

    ``None`` if the workspace can't be found.
    """
    for window in app.windows:
        for tab in window.tabs:
            for session in tab.sessions:
                workspace_value = await session.async_get_variable(WORKSPACE_VAR)
                if workspace_value == marker:
                    value = await session.async_get_variable(TEMPLATE_VAR)
                    return value or None
    return None


async def reveal_role(
    app: iterm2.App,
    *,
    marker: str,
    root: str,
    template: Template,
    role: str,
    source: "iterm2.Session",
) -> "iterm2.Session | None":
    """Split ``role``'s pane directly off of ``source`` and launch it.

    Called by ``handoff.py`` when a handoff's destination pane doesn't
    exist yet, with ``source`` being the session that just handed off (the
    role whose Stop hook fired). Growing outward from whoever triggered the
    reveal - rather than a fixed grid position - is what keeps a lazy
    workspace free of empty panes: splitting a fixed grid cell into
    existence unavoidably creates its sibling cells too (there is no way to
    carve one rectangle out of a grid without the others appearing), which
    is exactly the empty-pane problem lazy mode exists to avoid. See
    ``build_workspace``'s lazy branch, which likewise never touches
    ``layouts.py`` - the fixed-grid ``Layout`` tree is only used in eager
    mode.

    Returns the newly launched session. Splits vertically (left/right)
    when ``source``'s pane is wider than tall, horizontally otherwise, so
    a chain of reveals doesn't end up slicing one dimension into slivers.
    """
    pane = next(p for p in template.panes if p.role == role)
    auto_handoff = await get_auto_handoff(app, marker=marker)
    template_name = await get_template_name(app, marker=marker) or ""

    # grid_size is in character cells, which are taller than wide, so a
    # naive width>=height comparison would call most panes "tall" - halve
    # the width to roughly correct for cell aspect ratio before comparing.
    size = source.grid_size
    vertical = (size.width / 2) >= size.height

    session = await source.async_split_pane(vertical=vertical)
    await _launch_pane(
        session,
        marker=marker,
        root=root,
        template_name=template_name,
        pane=pane,
        auto_handoff=auto_handoff,
        lazy=True,
    )
    return session


async def _each_workspace_session(app: iterm2.App, *, marker: str):
    """Yield every session tagged with workspace ``marker``."""
    for window in app.windows:
        for tab in window.tabs:
            for session in tab.sessions:
                workspace_value = await session.async_get_variable(WORKSPACE_VAR)
                if workspace_value == marker:
                    yield session


async def get_run_doc(app: iterm2.App, *, marker: str) -> tuple[str | None, float | None]:
    """Read the workspace's current run doc path and start timestamp.

    Both are ``None`` if the workspace can't be found or no run has started
    yet in it (a freshly built workspace, before researcher's first
    handoff).
    """
    async for session in _each_workspace_session(app, marker=marker):
        doc = await session.async_get_variable(RUN_DOC_VAR)
        started = await session.async_get_variable(RUN_STARTED_VAR)
        return doc or None, float(started) if started else None
    return None, None


async def set_run_doc(
    app: iterm2.App, *, marker: str, doc: str, started_at: float
) -> None:
    """Stamp every pane in workspace ``marker`` with the active run's doc
    path and start time, so the next researcher.done can tell whether it's
    continuing this run or starting a new one.
    """
    async for session in _each_workspace_session(app, marker=marker):
        await session.async_set_variable(RUN_DOC_VAR, doc)
        await session.async_set_variable(RUN_STARTED_VAR, started_at)


async def send_clear(session: "iterm2.Session") -> None:
    """Wait for claude to be ready in ``session``, then submit ``/clear``.

    Used to reset a pane's conversation when a new pipeline run starts in
    an already-used workspace - see handoff.py's new-topic detection.
    """
    ready = await _wait_for_claude_prompt(session)
    if not ready:
        logger.warning("Gave up waiting for claude to be ready - skipping /clear")
        return
    await session.async_send_text("/clear")
    await session.async_send_text("\r")
