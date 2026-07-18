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

logger = logging.getLogger(__name__)


async def open_workspace(
    connection: iterm2.Connection,
    root: str,
    template_name: str,
    force_new: bool,
    auto_handoff: bool = False,
) -> None:
    """Attach to or build a workspace for ``root`` using ``template_name``."""
    resolved_root = os.path.abspath(os.path.expanduser(root))
    template = get_template(template_name)
    app = await iterm2.async_get_app(connection)

    if not force_new:
        existing = await iterm_ops.find_workspace_window(app, resolved_root)
        if existing is not None:
            logger.info("Workspace '%s' already exists - attaching", resolved_root)
            await iterm_ops.activate_window(existing)
            return

    logger.info("Building workspace '%s' (template '%s')", resolved_root, template_name)
    window = await iterm_ops.build_workspace(
        connection,
        marker=resolved_root,
        root=resolved_root,
        template=template,
        auto_handoff=auto_handoff,
    )
    await iterm_ops.activate_window(window)
