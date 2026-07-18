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

If auto-handoff is on and a role's turn ends with no marker at all (it
forgot to write ``<role>.done``/``<role>.blocked`` - easy to lose track of
in a long implementation turn, since the instruction sits at the very end
of the role's prompt), this module blocks the Stop and feeds a one-shot
reminder back into the same session rather than letting the pipeline go
silent. See ``_maybe_nag_missing_marker``.
"""

from __future__ import annotations

import json
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

# Marks that this role has already been reminded once about a missing
# marker, so a role that's genuinely done with nothing to hand off (or
# stuck waiting on the user for something outside the pipeline) doesn't get
# nagged on every subsequent Stop.
NAG_STATE_SUFFIX = ".nagged"


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


def _already_nagged(done_path: str) -> bool:
    return os.path.isfile(done_path + NAG_STATE_SUFFIX)


def _mark_nagged(done_path: str) -> None:
    open(done_path + NAG_STATE_SUFFIX, "w").close()


def _clear_nag(done_path: str) -> None:
    nag_path = done_path + NAG_STATE_SUFFIX
    if os.path.isfile(nag_path):
        os.remove(nag_path)


def _print_nag_block(role: str, done_path: str) -> None:
    """Emit the Stop-hook JSON that blocks the stop and feeds ``role`` a
    one-shot reminder to write its missing completion marker.

    Claude Code treats a Stop hook's ``{"decision": "block", "reason": ...}``
    stdout as an instruction to keep going, with ``reason`` fed back into the
    session as the next turn's input - so the same pane that just forgot the
    marker gets nudged to finish the step, instead of the pipeline silently
    stalling.
    """
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    f"You reported finishing your work as '{role}', but no "
                    f"completion marker was found at {done_path} (or its "
                    ".blocked equivalent). Per the Completion section of "
                    "your instructions, create that marker now (mkdir -p "
                    "the .claudespace directory first if needed) before "
                    "stopping - this is what hands your work off to the "
                    "next role. If you deliberately have nothing to hand "
                    "off (e.g. you're waiting on the user), ignore this."
                ),
            }
        )
    )


async def _send_handoff(
    connection: iterm2.Connection, *, root: str, role: str
) -> bool:
    """Send the next role's prompt if a fresh marker exists.

    Returns ``True`` if a handoff was sent, ``False`` otherwise (unknown
    role, or no fresh marker - the caller decides whether the latter
    warrants a nag).
    """
    stage = PIPELINE.get(role)
    if stage is None:
        logger.debug("Unknown role '%s' - nothing to hand off", role)
        return False

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
        return False

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
        return False

    await iterm_ops.send_role_prompt(
        destination_role, destination, text=prompt_text, submit=submit
    )
    await iterm_ops.activate_session(destination)
    _mark_handed_off(marker_path)
    _clear_nag(done_path)
    logger.info(
        "Handed off %s -> %s (submit=%s)", role, destination_role, submit
    )
    return True


async def _maybe_nag_missing_marker(
    connection: iterm2.Connection, *, root: str, role: str
) -> bool:
    """If auto-handoff is on and ``role`` has a forward stage but left no
    fresh marker at all, print a Stop-blocking nag once and return ``True``.

    Only fires for roles that have somewhere to hand off to
    (``stage.next_role``) - reviewer's terminal PASS case is correctly
    exempt, since it has no forward marker to forget. Fires at most once
    per missing-marker streak; ``_send_handoff`` clears the nag flag as soon
    as a real marker shows up, so a role that's genuinely stuck (e.g.
    waiting on the user) isn't nagged forever.
    """
    stage = PIPELINE.get(role)
    if stage is None or stage.next_role is None:
        return False

    done_path = done_marker_path(root, role)
    blocked_path = blocked_marker_path(root, role)

    if _read_fresh_marker(done_path) or (
        stage.bounce_to and _read_fresh_marker(blocked_path)
    ):
        return False

    if _already_nagged(done_path):
        return False

    app = await iterm2.async_get_app(connection)
    if not await iterm_ops.get_auto_handoff(app, marker=root):
        return False

    _mark_nagged(done_path)
    _print_nag_block(role, done_path)
    return True


async def _run(connection: iterm2.Connection, *, root: str, role: str) -> None:
    handed_off = await _send_handoff(connection, root=root, role=role)
    if not handed_off:
        await _maybe_nag_missing_marker(connection, root=root, role=role)


def main() -> None:
    """Entrypoint installed as the ``claudespace:handoff`` console script.

    Silently exits (code 0) if this isn't running inside a claudespace pane
    or there's nothing fresh to hand off - see module docstring. If the
    role forgot its completion marker and auto-handoff is on, prints a
    Stop-blocking JSON reminder instead (see ``_maybe_nag_missing_marker``).
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    role = os.environ.get("CLAUDESPACE_ROLE")
    root = os.environ.get("CLAUDESPACE_ROOT")
    if not role or not root:
        return

    try:
        iterm2.run_until_complete(
            lambda connection: _run(connection, root=root, role=role)
        )
    except Exception:
        logger.exception("Handoff failed for role '%s' in '%s'", role, root)
        sys.exit(0)


if __name__ == "__main__":
    main()
