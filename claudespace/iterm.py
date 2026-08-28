"""iTerm2 Python API integration.

This is the only module that talks to ``iterm2`` directly. Everything else
in the package works with plain config objects and dicts, which keeps the
rest of the codebase testable without a running iTerm2 instance.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import uuid

import iterm2

from claudespace.assets_sync import PROMPTS_DEST
from claudespace.config import CANONICAL_PANES, PaneConfig, Template
from claudespace.layouts import get_layout
from claudespace.pipeline import think_marker_path
from claudespace.themes import ROLE_THEMES, banner_command, build_role_profile

logger = logging.getLogger(__name__)

# User-defined session variable used to tag panes so a later run can find
# an existing workspace without relying on window/tab titles (which the
# user is free to rename). Its value is the workspace "marker": the
# resolved absolute root path.
WORKSPACE_VAR = "user.workspaceLauncherWorkspace"
ROLE_VAR = "user.workspaceLauncherRole"

# A UUID minted per build_workspace call (i.e. per physical window),
# stamped on every pane and exported as CLAUDESPACE_INSTANCE. The root path
# alone can't distinguish two *separate* windows on the same root (a stale
# window plus a fresh --new one, or two worktrees resolving to the same real
# path), and the session lookups return whichever match they hit first - so
# without this a handoff could silently land in the wrong terminal.
INSTANCE_VAR = "user.workspaceLauncherInstance"

# Whether handoffs auto-submit or only prefill. Applies to forward and
# backward (blocked/rejected) handoffs alike.
AUTO_HANDOFF_VAR = "user.workspaceLauncherAutoHandoff"

# Whether --lazy was used, i.e. non-entry panes were never launched and must
# be revealed on first handoff. Lets handoff.py read a missing destination
# pane as "reveal it" rather than "the window was closed".
LAZY_VAR = "user.workspaceLauncherLazy"

# The template the workspace was built with, so handoff.py can look up a
# destination role's command when revealing a lazy pane. The
# CLAUDESPACE_ROLE/ROOT env vars don't carry it.
TEMPLATE_VAR = "user.workspaceLauncherTemplate"

# Fallback for CLAUDESPACE_MAX_ITEMS (see --max-items) when a caller has
# none - notably reveal_role. Unlike the vars above, only the conductor pane
# reads it, so there is no cross-pane lookup to support and it isn't tracked
# as a session variable; a --lazy workspace whose conductor pane is revealed
# late falls back to this rather than the flag it was built with.
DEFAULT_MAX_ITEMS = 5

# Identity of the pipeline run currently occupying this workspace, plus when
# it started - stamped on every pane when a researcher.done (human) or
# conductor.done (dispatched) kicks off a run, so handoff.py can tell a
# fresh dispatch of a *different* run from a continuation. Only ever
# compared for equality, never parsed, so the conductor case (free-text
# backlog description rather than a path) works identically.
RUN_DOC_VAR = "user.workspaceLauncherRunDoc"
RUN_STARTED_VAR = "user.workspaceLauncherRunStarted"

# Printed by claude's input box once its TUI accepts text. Polled after
# launch so a prefill lands in claude rather than the shell that started it,
# or an intervening dialog (e.g. the first-run "trust this folder" prompt).
CLAUDE_PROMPT_MARKER = "❯"

# Give up prefilling a pane after this long - claude may be stuck behind a
# dialog. The user still gets a normal session once they clear it.
CLAUDE_READY_TIMEOUT_SECONDS = 15
CLAUDE_READY_POLL_INTERVAL_SECONDS = 0.25

# After sending Enter, how long to poll for the input box actually clearing,
# and how many times to resend. Closes the race where a handoff's prompt is
# typed but never submitted, stalling the pipeline until someone notices.
SUBMIT_CONFIRM_TIMEOUT_SECONDS = 3
SUBMIT_CONFIRM_POLL_INTERVAL_SECONDS = 0.2
SUBMIT_MAX_ATTEMPTS = 3

# Pause between typing text and sending "\r". Claude Code's TUI reads a fast
# keystroke burst as an in-progress paste, and during that window "\r" is
# inserted as a literal newline instead of submitting - the "Enter just adds
# a new line" failure. It clears within one repaint; the confirm/retry loop
# backstops whatever this doesn't cover.
SUBMIT_KEYSTROKE_SETTLE_SECONDS = 0.3


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
    """Prefill ``session``'s input with the role's slash command, if it needs one.

    A pane whose persona is already baked into its system prompt at launch
    (see ``_command_with_baked_persona``) needs no prefill at all: the
    slash command's only job is to make the model read
    ``~/.ai/prompts/<role>.prompt.md``, and that file is already *in* the
    system prompt. Prefilling it anyway made every pane read its own
    persona a second time - roughly 5k tokens - into conversation history,
    where it is resent on every subsequent turn. ``handoff.py`` already
    skipped the prefix for this reason on pipeline handoffs (see
    ``role_prompt_prefix``); the launch-time prefill was missed.
    """
    prefix = role_prompt_prefix(role)
    if not prefix:
        return
    await send_role_prompt(role, session, text=prefix, submit=False)


async def _screen_contains(session: "iterm2.Session", needle: str) -> bool:
    """Whether ``needle`` appears anywhere in ``session``'s visible screen,
    checked against the logical (unwrapped) text rather than per-line.

    A long handoff prompt (e.g. ``/planner read <path> from researcher and
    continue``) very plausibly exceeds one terminal line's width, especially
    in a multi-pane grid where each pane is a fraction of the window - it
    soft-wraps across two or more screen lines. Checking each line in
    isolation for the *whole* needle would never match a wrapped string even
    though it's sitting right there on screen, split across lines - which
    silently broke the caller this function exists for (see
    ``_confirm_submitted``): a false "not found" was being read as
    "successfully submitted," so the retry logic never even engaged and a
    still-unsubmitted, wrapped prompt was reported as submitted. Joining
    consecutive lines whenever iTerm2 reports a soft wrap (``not
    line.hard_eol``) reconstructs the actual logical line before searching,
    so a match spanning a wrap point is still found. Lines are joined with
    no separator on a soft wrap (that's what "wrap" means - no space was
    actually rendered there) and with a newline on a hard wrap, so unrelated
    text on genuinely separate lines can't accidentally concatenate into a
    false match.
    """
    contents = await session.async_get_screen_contents()
    parts: list[str] = []
    joined: list[str] = []
    for i in range(contents.number_of_lines):
        line = contents.line(i)
        parts.append(line.string)
        if line.hard_eol:
            joined.append("".join(parts))
            parts = []
    if parts:
        joined.append("".join(parts))
    return needle in "\n".join(joined)


async def _confirm_submitted(session: "iterm2.Session", *, text: str) -> bool:
    """Poll ``session``'s screen for up to ``SUBMIT_CONFIRM_TIMEOUT_SECONDS``
    to check that a submitted ``text`` no longer sits verbatim in the input
    box - i.e. the "\\r" was registered rather than lost to a mid-repaint of
    claude's TUI.

    This is a heuristic, not a guarantee (the text could in principle still
    be present as part of the conversation transcript above the input box),
    but in practice claude's input box is the only place a long, freshly
    typed prompt string appears verbatim right after submission - once
    submitted it's replaced by a spinner/status line, not echoed back
    unchanged. Returns ``True`` once the text is gone from the screen.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + SUBMIT_CONFIRM_TIMEOUT_SECONDS
    probe = text.strip()
    if not probe:
        return True
    while loop.time() < deadline:
        if not await _screen_contains(session, probe):
            return True
        await asyncio.sleep(SUBMIT_CONFIRM_POLL_INTERVAL_SECONDS)
    return not await _screen_contains(session, probe)


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

    When ``submit`` is ``True``, the "\\r" is verified rather than
    fire-and-forget: if the destination pane's TUI is mid-repaint (busy
    finishing its own turn, animating a spinner, etc.) the keystroke can
    land before claude registers it as the active input, leaving the text
    prefilled but never submitted - exactly the "handoff silently stalled,
    had to press Enter myself" failure. ``_confirm_submitted`` checks the
    input actually cleared and resends "\\r" (up to ``SUBMIT_MAX_ATTEMPTS``
    total) if it didn't.
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
    if not submit:
        return

    await asyncio.sleep(SUBMIT_KEYSTROKE_SETTLE_SECONDS)
    for attempt in range(1, SUBMIT_MAX_ATTEMPTS + 1):
        await session.async_send_text("\r")
        if await _confirm_submitted(session, text=text):
            return
        logger.warning(
            "Submit for role '%s' did not register on attempt %d/%d - retrying",
            role,
            attempt,
            SUBMIT_MAX_ATTEMPTS,
        )
        await asyncio.sleep(SUBMIT_KEYSTROKE_SETTLE_SECONDS)
    logger.error(
        "Submit for role '%s' never registered after %d attempts - prompt "
        "is left prefilled; user will need to press Enter manually",
        role,
        SUBMIT_MAX_ATTEMPTS,
    )


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


def role_prompt_file(role: str) -> str:
    """Path a role's bundled prompt would be synced to by ``assets_sync``,
    regardless of whether it's actually present."""
    return str(PROMPTS_DEST / f"{role}.prompt.md")


def role_prompt_prefix(role: str) -> str:
    """``""`` if ``role``'s pane has its persona baked into its system prompt
    at launch, else ``"/{role} "`` to fall back to the slash command's own
    "read the prompt file" step.

    Baking is unconditional whenever a prompt file exists (see
    ``_command_with_baked_persona``), so this reduces to a file check - no
    need to inspect the workspace's template or launch command. A role with
    no prompt file (an unrecognized name from a user's own template) still
    needs the slash command.

    Used both for the launch-time prefill (``_prefill_role_command``) and
    for every pipeline handoff (``handoff.py``), so the two can't drift.
    """
    if os.path.isfile(role_prompt_file(role)):
        return ""
    return f"/{role} "


def _command_with_baked_persona(role: str, command: str) -> str:
    """Append ``--append-system-prompt-file`` and ``--name`` for ``role`` onto
    ``command``, unless no prompt file exists for ``role`` (an unrecognized
    role name from a user's own custom template).

    ``--name`` labels the session in Claude Code's own prompt box and the
    terminal title. It matters more than it looks: panes are no longer
    prefilled with ``/<role>``, so without it a pane running the TUI has no
    in-band indication of which role it is (the launch banner scrolls out of
    the alt-screen immediately, and the theme background is painted over).
    See ``themes.build_role_profile`` for the badge, which is the label that
    stays visible.

    This applies to a custom command from a user's own template
    (``~/.config/claudespace/templates.toml`` pointing a pane at a wrapper
    around a different model/CLI) as much as to the built-in ones. That
    wrapper isn't guaranteed to forward an unrecognized flag through to a
    real Claude Code process - but the existing pane-readiness check
    (``_wait_for_claude_prompt``) already tolerates a pane that never
    reaches claude's prompt (it just skips the prefill and logs a warning,
    see ``_prefill_role_command``), so the failure mode for a genuinely
    incompatible wrapper is a visible, debuggable startup error in that one
    pane - not silent breakage.
    """
    prompt_file = role_prompt_file(role)
    if not os.path.isfile(prompt_file):
        return command
    return (
        f"{command} --append-system-prompt-file {shlex.quote(prompt_file)} "
        f"--name {shlex.quote(role)}"
    )


async def _launch_pane(
    session: "iterm2.Session",
    *,
    marker: str,
    instance: str,
    root: str,
    template_name: str,
    pane: PaneConfig,
    auto_handoff: bool,
    lazy: bool,
    think: bool,
    max_items: int,
) -> None:
    """Tag ``session`` for ``pane``'s role and launch its command in it.

    Shared by eager ``build_workspace`` (all panes at once) and lazy
    reveal (one pane at a time, from ``handoff.py``) - both need identical
    tagging/theming/launch behavior, just triggered at different times.
    """
    await session.async_set_variable(WORKSPACE_VAR, marker)
    await session.async_set_variable(INSTANCE_VAR, instance)
    await session.async_set_variable(ROLE_VAR, pane.role)
    await session.async_set_variable(AUTO_HANDOFF_VAR, auto_handoff)
    await session.async_set_variable(LAZY_VAR, lazy)
    await session.async_set_variable(TEMPLATE_VAR, template_name)
    banner = ""
    if pane.role in ROLE_THEMES:
        await session.async_set_profile_properties(build_role_profile(pane.role))
        banner = f"{banner_command(pane.role)} && "
    # Deliberately no async_set_name here: the session title is driven by the
    # running process, so the shell resets it to "-zsh" before claude even
    # starts. The pane's identity comes from the profile badge (set in
    # build_role_profile, drawn over the TUI) and `claude --name` (see
    # _command_with_baked_persona), which sets the title from inside claude.
    command = _command_with_baked_persona(pane.role, pane.command)
    await session.async_send_text(
        f"cd {root} && export CLAUDESPACE_ROOT={root} && "
        f"export CLAUDESPACE_ROLE={pane.role} && "
        f"export CLAUDESPACE_INSTANCE={instance} && "
        f"export CLAUDESPACE_MAX_ITEMS={max_items} && "
        f"export CLAUDESPACE_THINK={int(think)} && {banner}{command}\n"
    )
    logger.info("Launched %s (%s) in role '%s'", command, root, pane.role)


async def _wait_for_current_session(
    window: iterm2.Window, *, timeout: float = 5.0
) -> iterm2.Session:
    """Poll ``window.current_tab`` until iTerm2's App state catches up with
    a just-created window, then return its current session.

    ``Window.async_create`` can return a ``Window`` whose tab list hasn't
    caught up yet - ``current_tab`` legitimately returns ``None`` right
    after creation (see its own docstring: "or ``None`` if it could not be
    determined"), since the App's live state is updated by a separate
    async notification stream that can lag slightly behind the creation
    RPC's response. Racing straight into ``.current_session`` on that
    ``None`` crashes with an ``AttributeError``; poll briefly instead of
    failing on the very first check.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        tab = window.current_tab
        if tab is not None:
            return tab.current_session
        await asyncio.sleep(0.05)
    raise RuntimeError(
        "Timed out waiting for the new iTerm2 window's tab to appear"
    )


async def build_workspace(
    connection: iterm2.Connection,
    *,
    marker: str,
    root: str,
    template_name: str,
    template: Template,
    auto_handoff: bool = True,
    lazy: bool = False,
    think: bool = False,
    max_items: int = DEFAULT_MAX_ITEMS,
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

    instance = str(uuid.uuid4())
    root_session = await _wait_for_current_session(window)

    if lazy:
        entry_pane = next(p for p in template.panes if p.role == template.entry_role)
        await _launch_pane(
            root_session,
            marker=marker,
            instance=instance,
            root=root,
            template_name=template_name,
            pane=entry_pane,
            auto_handoff=auto_handoff,
            lazy=True,
            think=think,
            max_items=max_items,
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
            instance=instance,
            root=root,
            template_name=template_name,
            pane=pane,
            auto_handoff=auto_handoff,
            lazy=False,
            think=think,
            max_items=max_items,
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


async def _matches_workspace(
    session: "iterm2.Session", *, marker: str, instance: str | None
) -> bool:
    """Whether ``session`` belongs to the workspace ``marker`` names.

    When ``instance`` is given, the session must also carry that exact
    ``INSTANCE_VAR`` - not just the same root-path ``marker`` - before it's
    considered a match. This is what stops two separate windows opened
    against the same root (a stale leftover window plus a fresh ``--new``
    one, or two worktrees resolving to the same real path) from being
    treated as interchangeable: without it, a Stop hook running in one
    window could silently address a pane in a *different* window that
    happens to share the same root, misrouting a handoff into the wrong
    terminal. ``instance=None`` preserves the old root-only matching, used
    by callers (like ``workspace.py``'s attach-or-build check) that don't
    yet know an instance ID because there may be no workspace at all yet.
    """
    workspace_value = await session.async_get_variable(WORKSPACE_VAR)
    if workspace_value != marker:
        return False
    if instance is None:
        return True
    instance_value = await session.async_get_variable(INSTANCE_VAR)
    return instance_value == instance


async def find_role_session(
    app: iterm2.App, *, marker: str, role: str, instance: str | None = None
) -> iterm2.Session | None:
    """Find the session tagged with ``role`` inside the workspace ``marker``.

    Used by ``handoff.py`` to locate the destination pane for a pipeline
    handoff. Returns ``None`` if the workspace or role isn't found (e.g. the
    window was closed, or a template without that role was used).

    Pass ``instance`` (the caller's own ``CLAUDESPACE_INSTANCE``) to restrict
    the match to panes in the same physical window - see
    ``_matches_workspace``. Without it, two windows sharing a root are
    indistinguishable and the first match wins, which is the bug this
    parameter exists to close.
    """
    for window in app.windows:
        for tab in window.tabs:
            for session in tab.sessions:
                if not await _matches_workspace(
                    session, marker=marker, instance=instance
                ):
                    continue
                role_value = await session.async_get_variable(ROLE_VAR)
                if role_value == role:
                    return session
    return None


async def _get_workspace_var(
    app: iterm2.App, *, marker: str, instance: str | None, name: str
):
    """Read a session variable off any pane in the workspace tagged ``marker``.

    Every pane in a workspace carries the same value for these, so the first
    match answers for all of them. ``None`` if the workspace can't be found.
    """
    async for session in each_workspace_session(app, marker=marker, instance=instance):
        return await session.async_get_variable(name)
    return None


async def get_auto_handoff(
    app: iterm2.App, *, marker: str, instance: str | None = None
) -> bool:
    """Auto-handoff toggle; ``False`` (prefill-only) if not found."""
    return bool(
        await _get_workspace_var(
            app, marker=marker, instance=instance, name=AUTO_HANDOFF_VAR
        )
    )


async def get_lazy(app: iterm2.App, *, marker: str, instance: str | None = None) -> bool:
    """``--lazy`` toggle; ``False`` if not found."""
    return bool(
        await _get_workspace_var(app, marker=marker, instance=instance, name=LAZY_VAR)
    )


async def get_template_name(
    app: iterm2.App, *, marker: str, instance: str | None = None
) -> str | None:
    """Template the workspace was built with; ``None`` if not found."""
    value = await _get_workspace_var(
        app, marker=marker, instance=instance, name=TEMPLATE_VAR
    )
    return value or None


def _cell_area(session: "iterm2.Session") -> float:
    """Approximate on-screen area of ``session``'s pane, in character cells.

    Character cells are taller than wide, so raw width*height would
    over-weight wide panes relative to tall ones - halve the width first to
    roughly correct for cell aspect ratio before comparing panes against
    each other (mirrors the same correction in ``reveal_role``).
    """
    size = session.grid_size
    return (size.width / 2) * size.height


def _largest_sibling(source: "iterm2.Session") -> "iterm2.Session":
    """Return the biggest pane in ``source``'s tab, ``source`` itself if tied.

    Used by ``reveal_role`` so each new pane splits off of whichever pane
    currently has the most room, instead of always splitting the handoff
    source - which, in a chained pipeline, is typically the pane created by
    the *previous* reveal and thus already shrunk. Splitting the source
    every time cascades: each new pane halves an already-halved pane, so a
    five-stage pipeline ends with an unreadable sliver. Always growing out
    of the biggest pane instead keeps the tab closer to a balanced grid.
    """
    candidates = source.tab.sessions if source.tab is not None else [source]
    return max(candidates, key=_cell_area, default=source)


async def reveal_role(
    app: iterm2.App,
    *,
    marker: str,
    instance: str,
    root: str,
    template: Template,
    role: str,
    source: "iterm2.Session",
) -> "iterm2.Session | None":
    """Split off the tab's biggest pane to make room for ``role`` and launch it.

    Called by ``handoff.py`` when a handoff's destination pane doesn't
    exist yet, with ``source`` being the session that just handed off (the
    role whose Stop hook fired). The actual split point is
    ``_largest_sibling(source)`` rather than ``source`` itself - see its
    docstring for why splitting the handoff source directly cascades into
    unreadable slivers. Growing outward from the biggest pane - rather than
    a fixed grid position - is what keeps a lazy workspace free of empty
    panes: splitting a fixed grid cell into existence unavoidably creates
    its sibling cells too (there is no way to carve one rectangle out of a
    grid without the others appearing), which is exactly the empty-pane
    problem lazy mode exists to avoid. See ``build_workspace``'s lazy
    branch, which likewise never touches ``layouts.py`` - the fixed-grid
    ``Layout`` tree is only used in eager mode.

    ``role`` doesn't have to be one of ``template.panes`` - a role missing
    from this workspace's own template (e.g. conductor in a ``native``
    workspace) still gets spun up, using ``CANONICAL_PANES`` as a fallback,
    so a role a workspace wasn't originally built with can still come alive
    the moment the pipeline actually needs it. See handoff._reveal_destination,
    which is the caller that decides when that's appropriate.

    Returns the newly launched session. Splits vertically (left/right)
    when the pane being split is wider than tall, horizontally otherwise,
    so repeated reveals don't end up slicing one dimension into slivers.
    """
    pane = next((p for p in template.panes if p.role == role), None) or CANONICAL_PANES[role]
    auto_handoff = await get_auto_handoff(app, marker=marker, instance=instance)
    template_name = await get_template_name(app, marker=marker, instance=instance) or ""

    split_target = _largest_sibling(source)

    # grid_size is in character cells, which are taller than wide, so a
    # naive width>=height comparison would call most panes "tall" - halve
    # the width to roughly correct for cell aspect ratio before comparing.
    size = split_target.grid_size
    vertical = (size.width / 2) >= size.height

    session = await split_target.async_split_pane(vertical=vertical)
    await _launch_pane(
        session,
        marker=marker,
        instance=instance,
        root=root,
        template_name=template_name,
        pane=pane,
        auto_handoff=auto_handoff,
        lazy=True,
        # Read back off the workspace's own marker rather than a session
        # variable: --think is toggleable on an already-open workspace (see
        # workspace._set_think), so the folder is the source of truth.
        think=os.path.isfile(think_marker_path(root)),
        max_items=DEFAULT_MAX_ITEMS,
    )
    return session


async def each_workspace_session(
    app: iterm2.App, *, marker: str, instance: str | None = None
):
    """Yield every session tagged with workspace ``marker``."""
    for window in app.windows:
        for tab in window.tabs:
            for session in tab.sessions:
                if await _matches_workspace(session, marker=marker, instance=instance):
                    yield session


async def get_run_doc(
    app: iterm2.App, *, marker: str, instance: str | None = None
) -> tuple[str | None, float | None]:
    """Read the workspace's current run doc path and start timestamp.

    Both are ``None`` if the workspace can't be found or no run has started
    yet in it (a freshly built workspace, before researcher's first
    handoff).
    """
    async for session in each_workspace_session(app, marker=marker, instance=instance):
        doc = await session.async_get_variable(RUN_DOC_VAR)
        started = await session.async_get_variable(RUN_STARTED_VAR)
        return doc or None, float(started) if started else None
    return None, None


async def set_run_doc(
    app: iterm2.App,
    *,
    marker: str,
    instance: str | None = None,
    doc: str,
    started_at: float,
) -> None:
    """Stamp every pane in workspace ``marker`` with the active run's doc
    path and start time, so the next researcher.done can tell whether it's
    continuing this run or starting a new one.
    """
    async for session in each_workspace_session(app, marker=marker, instance=instance):
        await session.async_set_variable(RUN_DOC_VAR, doc)
        await session.async_set_variable(RUN_STARTED_VAR, started_at)


async def send_new(session: "iterm2.Session") -> None:
    """Wait for claude to be ready in ``session``, then submit ``/new``.

    ``/new`` is an alias of ``/clear`` (same underlying command: start a new
    session with empty context, previous session stays on disk resumable
    with ``/resume``) - used here instead of the ``/clear`` spelling so the
    pane's own transcript reads as "started a new session" rather than
    "history erased", even though the two are functionally identical.

    Used to reset a pane's conversation when a new pipeline run starts in
    an already-used workspace - see handoff.py's new-topic detection.
    """
    ready = await _wait_for_claude_prompt(session)
    if not ready:
        logger.warning("Gave up waiting for claude to be ready - skipping /new")
        return
    await session.async_send_text("/new")
    await asyncio.sleep(SUBMIT_KEYSTROKE_SETTLE_SECONDS)
    await session.async_send_text("\r")
