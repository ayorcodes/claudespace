"""Attach-or-build orchestration.

Every workspace is opened by ``root`` folder + ``template`` name. The
resolved absolute root path is the dedup marker: re-running against the
same folder attaches to its existing window instead of creating a
duplicate, regardless of which relative path or cwd you ran it from.
"""

from __future__ import annotations

import logging
import os

import iterm2

from claudespace import iterm as iterm_ops
from claudespace.config import get_template
from claudespace.pipeline import MARKER_DIR, think_marker_path

logger = logging.getLogger(__name__)


def _set_think(root: str, think: bool) -> None:
    """Create or remove ``root``'s ``think`` marker (see
    ``think_marker_path``) so the flag's state is a property of the
    workspace folder, readable by any role's pane at any point in the run.
    """
    os.makedirs(os.path.join(root, MARKER_DIR), exist_ok=True)
    marker = think_marker_path(root)
    if think:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("autonomous\n")
        logger.info("Autonomous (--think) mode on for '%s'", root)
        return
    try:
        os.remove(marker)
    except FileNotFoundError:
        return
    logger.info("Autonomous (--think) mode off for '%s'", root)


async def open_workspace(
    connection: iterm2.Connection,
    root: str,
    template_name: str,
    force_new: bool,
    auto_handoff: bool = True,
    lazy: bool = False,
    think: bool = False,
    max_items: int = iterm_ops.DEFAULT_MAX_ITEMS,
    just_launched_iterm: bool = False,
) -> None:
    """Attach to or build a workspace for ``root`` using ``template_name``."""
    resolved_root = os.path.abspath(os.path.expanduser(root))
    template = get_template(template_name)
    app = await iterm2.async_get_app(connection)

    # Written before the attach check, not just on build, so `claudespace
    # --think` on an already-open workspace switches it into autonomous
    # mode (and a plain re-run switches it back out) - the roles read the
    # marker at handoff time, so it takes effect from the next stage on.
    _set_think(resolved_root, think)

    if not force_new:
        existing = await iterm_ops.find_workspace_window(app, resolved_root)
        if existing is not None:
            logger.info("Workspace '%s' already exists - attaching", resolved_root)
            await iterm_ops.activate_window(existing)
            return

    # iTerm2 opens its own default empty window on launch, before we ever
    # get a connection - if we just cold-launched it (see cli.py), that
    # window is stray chrome the user never asked for, not a workspace
    # dedup target. Remember it now so build_workspace can close it once
    # the real workspace window is up (never before - closing it first
    # risks quitting iTerm2 entirely if it was the app's only window).
    stray_windows = list(app.windows) if just_launched_iterm else []

    logger.info("Building workspace '%s' (template '%s')", resolved_root, template_name)
    os.makedirs(os.path.join(resolved_root, MARKER_DIR), exist_ok=True)
    window = await iterm_ops.build_workspace(
        connection,
        marker=resolved_root,
        root=resolved_root,
        template_name=template_name,
        template=template,
        auto_handoff=auto_handoff,
        lazy=lazy,
        think=think,
        max_items=max_items,
    )
    await iterm_ops.activate_window(window)

    for stray in stray_windows:
        await iterm_ops.close_window_if_empty(stray)
