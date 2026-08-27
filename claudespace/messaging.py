"""Ad hoc, mid-turn messaging between pipeline role panes.

``handoff.py`` moves work forward/backward through ``pipeline.PIPELINE``, but
only at Stop-hook time and only via a ``.done``/``.blocked`` marker - a role
has to finish its whole turn and decide "this is a formal handoff" before it
can say anything to another role. That's the right mechanism for stage
transitions (it's what makes the pipeline auditable and resumable), but it
means a role can't casually ping another one while both are still working -
a status check, a heads-up, a "still there?" - without ending its turn and
routing through the pipeline's rejection/question machinery.

This module is that side channel. ``claudespace-msg <role> "<text>"``,
invoked as an ordinary shell command from inside any role's Claude session
(a normal Bash tool call, not a Stop hook), types ``text`` straight into
``<role>``'s pane and submits it immediately - fire-and-forget, same as a
human switching panes and typing something. The sender's own turn is
unaffected: this returns as soon as the message is sent, it does not wait
for a reply.

Deliberately unrestricted: any role can message any role, not just its
pipeline-adjacent ``bounce_to``/``next_role`` neighbors - the whole point is
to not re-encode the pipeline graph as a second, stricter one for this. The
pipeline graph remains the *only* thing that gates stage transitions; this
tool can't advance or bounce anything, it can only make a pane's screen say
something.

Deduplicated against handoff.py rather than reimplemented: both need to find
a role's pane (creating it via ``reveal_role`` if the workspace hasn't
launched it lazily yet) and type text into it. ``handoff.py`` keeps its own
copy of that logic (``_reveal_destination``) because it additionally needs
the lazy/in-template distinction to decide whether revealing is *allowed* at
all for a formal handoff; this module always allows it, since an ad hoc
message reaching a not-yet-launched pane is unambiguously fine - there is no
"you're not supposed to skip ahead" concern for a side channel that can't
advance the pipeline anyway.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import iterm2

from claudespace import iterm as iterm_ops
from claudespace.config import CANONICAL_PANES, get_template

logger = logging.getLogger(__name__)


async def _find_or_reveal(
    app: iterm2.App, *, root: str, instance: str | None, sender_role: str, target_role: str
) -> "iterm2.Session | None":
    """Locate ``target_role``'s pane, revealing it off ``sender_role``'s pane
    if the workspace hasn't launched it yet.

    Unlike ``handoff._reveal_destination``, this doesn't gate revealing on
    the workspace's lazy toggle or on the target being part of the current
    template - an ad hoc message is allowed to spin up any canonical role's
    pane on demand. Returns ``None`` only if the target truly can't be
    located or created (unknown role, or the sender's own pane is gone too -
    nothing to split off of).
    """
    existing = await iterm_ops.find_role_session(
        app, marker=root, role=target_role, instance=instance
    )
    if existing is not None:
        return existing

    if target_role not in CANONICAL_PANES:
        return None

    template_name = await iterm_ops.get_template_name(app, marker=root, instance=instance)
    template = get_template(template_name) if template_name else get_template("agentic")

    source = await iterm_ops.find_role_session(
        app, marker=root, role=sender_role, instance=instance
    )
    if source is None:
        return None

    return await iterm_ops.reveal_role(
        app,
        marker=root,
        instance=instance,
        root=root,
        template=template,
        role=target_role,
        source=source,
    )


async def _send(
    connection: iterm2.Connection,
    *,
    root: str,
    instance: str | None,
    sender_role: str,
    target_role: str,
    text: str,
) -> bool:
    app = await iterm2.async_get_app(connection)
    destination = await _find_or_reveal(
        app, root=root, instance=instance, sender_role=sender_role, target_role=target_role
    )
    if destination is None:
        logger.error(
            "No pane found or creatable for role '%s' in workspace '%s' - "
            "message not sent",
            target_role,
            root,
        )
        return False

    prompt_text = f"[msg from {sender_role}] {text} "
    await iterm_ops.send_role_prompt(target_role, destination, text=prompt_text, submit=True)
    await iterm_ops.activate_session(destination)
    logger.info("Sent message %s -> %s", sender_role, target_role)
    return True


def main() -> None:
    """Entrypoint installed as the ``claudespace-msg`` console script.

    Reads the sender's own role/workspace from ``CLAUDESPACE_ROLE``/
    ``CLAUDESPACE_ROOT``/``CLAUDESPACE_INSTANCE`` (set on every claudespace
    pane at launch - see ``roles.py``/``iterm.py``), exactly like
    ``handoff.py``. Exits non-zero with a message on stderr if run outside a
    claudespace pane or if the target role is unrecognized/unreachable, so a
    role invoking this via Bash gets clear feedback rather than a silent
    no-op.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(
        prog="claudespace-msg",
        description="Send an ad hoc message into another pipeline role's pane.",
    )
    parser.add_argument("role", help="Target role, e.g. principal, implementer, reviewer")
    parser.add_argument("text", help="Message text")
    args = parser.parse_args()

    sender_role = os.environ.get("CLAUDESPACE_ROLE")
    root = os.environ.get("CLAUDESPACE_ROOT")
    instance = os.environ.get("CLAUDESPACE_INSTANCE")
    if not sender_role or not root:
        print(
            "claudespace-msg: not running inside a claudespace pane "
            "(CLAUDESPACE_ROLE/CLAUDESPACE_ROOT unset)",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.role not in CANONICAL_PANES:
        print(
            f"claudespace-msg: unknown role '{args.role}' - expected one of "
            f"{sorted(CANONICAL_PANES)}",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.role == sender_role:
        print("claudespace-msg: can't message your own role", file=sys.stderr)
        sys.exit(1)

    try:
        sent = iterm2.run_until_complete(
            lambda connection: _send(
                connection,
                root=root,
                instance=instance,
                sender_role=sender_role,
                target_role=args.role,
                text=args.text,
            )
        )
    except Exception as exc:
        logger.exception("claudespace-msg failed")
        print(f"claudespace-msg: failed to send: {exc!r}", file=sys.stderr)
        sys.exit(1)

    sys.exit(0 if sent else 1)


if __name__ == "__main__":
    main()
