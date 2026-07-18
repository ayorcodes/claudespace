"""Pipeline handoff: send the next role's prompt into its pane.

Invoked by the ``claudespace-handoff`` Stop hook after a pane finishes a
turn. Reads which role/workspace it's running in from
``CLAUDESPACE_ROLE``/``CLAUDESPACE_ROOT`` (set when the pane was launched -
see ``roles.py`` and ``iterm.py``), checks for a fresh completion marker,
and if one exists, prefills (and possibly submits) the destination pane's
prompt with a reference to the real artifact path the marker names.

Forward handoffs (a role finished successfully, ``<role>.done`` exists)
auto-submit only if the workspace's auto-handoff toggle is on; otherwise
they only prefill. Backward handoffs (a role bounced work back,
``<role>.blocked`` exists) always prefill-only, regardless of the toggle -
a rejection should never silently re-trigger work without a human looking
at it first.

This module is a no-op (exits 0 silently) whenever it can't find enough
context to act - missing env vars, no fresh marker, no destination pane -
so it's safe to wire into a *global* Stop hook that fires for every Claude
Code session on the machine, not just claudespace panes.
"""

from __future__ import annotations

import logging
import os
import sys

import iterm2

from claudespace import iterm as iterm_ops
from claudespace.pipeline import PIPELINE, blocked_marker_path, done_marker_path

logger = logging.getLogger(__name__)

# Where each role's marker files record they've already been handed off, so
# a Stop hook firing again on the same marker (e.g. the user asks the pane
# a follow-up question after it already reported completion) doesn't
# re-trigger the handoff.
HANDOFF_STATE_SUFFIX = ".handed-off"


def _read_fresh_marker(path: str) -> str | None:
    """Return the marker's content (the artifact path it names) if it
    exists and hasn't already been handed off, else ``None``.

    A marker's own content is the project-root-relative path to wherever
    the role actually persisted its document - projects define their own
    documentation conventions (see ``pipeline.py``), so this is never a
    fixed path.
    """
    if not os.path.isfile(path):
        return None
    state_path = path + HANDOFF_STATE_SUFFIX
    if os.path.isfile(state_path) and os.path.getmtime(path) <= os.path.getmtime(
        state_path
    ):
        return None
    with open(path) as f:
        return f.read().strip()


def _mark_handed_off(path: str) -> None:
    open(path + HANDOFF_STATE_SUFFIX, "w").close()


async def _send_handoff(
    connection: iterm2.Connection, *, root: str, role: str
) -> None:
    stage = PIPELINE.get(role)
    if stage is None:
        logger.debug("Unknown role '%s' - nothing to hand off", role)
        return

    blocked_path = blocked_marker_path(root, role)
    done_path = done_marker_path(root, role)

    blocked_artifact = stage.bounce_to and _read_fresh_marker(blocked_path)
    done_artifact = stage.next_role and _read_fresh_marker(done_path)

    if blocked_artifact:
        destination_role, submit = stage.bounce_to, False
        marker_path = blocked_path
        prompt_text = (
            f"/{destination_role} {role} sent this back - see "
            f"{blocked_artifact} "
        )
    elif done_artifact:
        destination_role = stage.next_role
        marker_path = done_path
        app = await iterm2.async_get_app(connection)
        submit = await iterm_ops.get_auto_handoff(app, marker=root)
        prompt_text = (
            f"/{destination_role} read {done_artifact} from {role} and continue "
        )
    else:
        return

    app = await iterm2.async_get_app(connection)
    destination = await iterm_ops.find_role_session(
        app, marker=root, role=destination_role
    )
    if destination is None:
        logger.warning(
            "No pane found for role '%s' in workspace '%s' - skipping handoff",
            destination_role,
            root,
        )
        return

    await iterm_ops.send_role_prompt(
        destination_role, destination, text=prompt_text, submit=submit
    )
    _mark_handed_off(marker_path)
    logger.info(
        "Handed off %s -> %s (submit=%s)", role, destination_role, submit
    )


def main() -> None:
    """Entrypoint installed as the ``claudespace:handoff`` console script.

    Silently exits (code 0) if this isn't running inside a claudespace pane
    or there's nothing fresh to hand off - see module docstring.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    role = os.environ.get("CLAUDESPACE_ROLE")
    root = os.environ.get("CLAUDESPACE_ROOT")
    if not role or not root:
        return

    try:
        iterm2.run_until_complete(
            lambda connection: _send_handoff(connection, root=root, role=role)
        )
    except Exception:
        logger.exception("Handoff failed for role '%s' in '%s'", role, root)
        sys.exit(0)


if __name__ == "__main__":
    main()
