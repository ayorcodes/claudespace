"""PreToolUse hook: stop a read-only role from modifying code.

Every role's prompt states what it must not do - researcher investigates,
planner never reads code, reviewer never fixes what it finds. Those are
requests. A pane is a full Claude Code session with every tool and
``--permission-mode auto``, so nothing enforced them, and a researcher was
observed rewriting a component mid-investigation: editing is the obvious way
to act on what you just found, and no permission prompt stood in the way.

Tool denial can't express the rule. ``--disallowed-tools Edit`` is trivially
routed around with ``Write`` (verified: the model reaches for it immediately),
and denying ``Write`` as well would stop these roles persisting the one
artifact each exists to produce. The real constraint is about *what* is
written, not *which tool* writes it.

So: for a role in ``READ_ONLY_ROLES``, a write is allowed only to

- anything inside a ``.claudespace/`` directory - the markers and reports
  that drive the pipeline, and
- a Markdown file - every artifact these roles produce (Technical Brief,
  Planning Brief, Implementation Design, review, memory note, backlog) is a
  document, wherever the project's own conventions put it.

Anything else is denied with an imperative reason naming the role and the
exact marker to write (route: implementer) - a block is a routing
instruction to act on immediately, not advice a role can go dormant in front
of (a real failure mode: a block landed and the role just waited for the
user to say "route it"). Deliberately a path rule rather than a directory
allowlist: projects define their own documentation locations (see
``pipeline.py``), so there is no fixed ``docs/`` to permit.

Like ``handoff.py`` this is wired in globally and must be a fast no-op
everywhere else - it exits silently (allowing the call) whenever
``CLAUDESPACE_ROLE`` is unset or names a role with no restriction.
"""

from __future__ import annotations

import json
import os
import sys

from claudespace.config import READ_ONLY_ROLES
from claudespace.pipeline import done_marker_path

MARKER_DIR_SEGMENT = f"{os.sep}.claudespace{os.sep}"
DOC_SUFFIXES = (".md", ".markdown")

# Tools that write to disk. Anything else (Read, Grep, Bash, ...) is outside
# this hook's remit - Bash could of course modify a file too, but a role
# reaches for Edit/Write to change code, not for `sed -i`, and blocking Bash
# would break the investigation these roles exist to do.
WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})


def is_allowed_path(path: str) -> bool:
    """Whether a read-only role may write to ``path``."""
    if not path:
        # No path to judge - let the normal permission flow handle it rather
        # than denying something this hook doesn't understand.
        return True
    normalized = os.path.normpath(path)
    if MARKER_DIR_SEGMENT in f"{os.sep}{normalized.strip(os.sep)}{os.sep}":
        return True
    return normalized.lower().endswith(DOC_SUFFIXES)


def decide(
    payload: dict,
    role: str | None,
    *,
    root: str | None = None,
    instance: str | None = None,
) -> str | None:
    """Return a denial reason, or ``None`` to allow the call through.

    The reason is an imperative next action, not advice: a block is a
    routing instruction, never a dead-end a role can go dormant in front of
    (see module docstring's "R3" reference and the friction ADR it
    implements). ``root``/``instance`` default to the same env vars
    ``handoff.py`` reads (``CLAUDESPACE_ROOT``/``CLAUDESPACE_INSTANCE``);
    accepting them as arguments keeps this unit-testable without env vars.
    """
    if not role or role not in READ_ONLY_ROLES:
        return None
    if payload.get("tool_name") not in WRITE_TOOLS:
        return None

    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if is_allowed_path(path):
        return None

    root = root if root is not None else os.environ.get("CLAUDESPACE_ROOT")
    instance = instance if instance is not None else os.environ.get("CLAUDESPACE_INSTANCE")

    if root:
        marker = done_marker_path(root, role, instance)
        next_action = f"write {marker} with `route: implementer` naming a note describing the change"
    else:
        next_action = (
            "write your role's `.done` marker with `route: implementer` "
            "naming a note describing the change"
        )

    return (
        f"claudespace blocked this write: the '{role}' role does not modify "
        f"code. '{path}' is neither a Markdown document nor inside "
        f".claudespace/. Do NOT stop. Your next and only action: {next_action}. "
        "Then stop."
    )


def main() -> None:
    """Entrypoint installed as the ``claudespace-guard`` console script."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        # A hook that can't parse its input must not block the session.
        return

    reason = decide(
        payload,
        os.environ.get("CLAUDESPACE_ROLE"),
        root=os.environ.get("CLAUDESPACE_ROOT"),
        instance=os.environ.get("CLAUDESPACE_INSTANCE"),
    )
    if reason is None:
        return

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


if __name__ == "__main__":
    main()
