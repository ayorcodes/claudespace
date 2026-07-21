"""The researcher -> planner -> principal -> implementer -> reviewer pipeline.

Defines, for each role, which role is next on success and which role a
rejection bounces back to. This is the one place that knows the pipeline's
shape - ``handoff.py`` and the Stop hook just walk this map.

Projects define their own documentation location conventions (e.g. a
``CLAUDE.md`` that says research briefs live in ``docs/research/``), and
each role's prompt already follows those conventions - a fixed artifact
path like ``.claudespace/research.md`` would either duplicate the real
document or, worse, silently never get written because the prompt's
project-standards instruction takes precedence. So ``.claudespace/`` holds
only small marker files - ``<role>.done`` / ``<role>.blocked`` - whose
*contents* are the real, project-root-relative path to wherever the role
actually persisted its document. See ``assets/prompts/*.prompt.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

MARKER_DIR = ".claudespace"


@dataclass(frozen=True, slots=True)
class Stage:
    """One role's position in the pipeline.

    ``next_role`` is who to hand off to on success, or ``None`` if this role
    is terminal (reviewer - always surfaced to the user, never
    auto-advanced). ``bounce_to`` is who a ``.blocked`` marker routes back
    to, or ``None`` if this role has no bounce path.

    ``alt_next_roles`` lists other roles this stage's ``.done`` marker is
    allowed to route to instead of ``next_role``, when the marker's content
    requests it (see ``parse_done_marker``). Empty for stages with only one
    possible destination.
    """

    next_role: str | None
    bounce_to: str | None = None
    alt_next_roles: tuple[str, ...] = ()


PIPELINE: dict[str, Stage] = {
    # A researcher investigating a well-scoped engineering change (bug fix,
    # refactor, infra tweak) with no open product questions can route
    # straight to principal - a Planning Brief would just restate facts the
    # researcher already confirmed. See researcher.prompt.md's routing
    # guidance and parse_done_marker's "route:" directive.
    "researcher": Stage(next_role="planner", alt_next_roles=("principal",)),
    "planner": Stage(next_role="principal"),
    "principal": Stage(next_role="implementer", bounce_to="planner"),
    "implementer": Stage(next_role="reviewer"),
    "reviewer": Stage(next_role=None, bounce_to="implementer"),
}


def parse_done_marker(content: str, *, stage: Stage) -> tuple[str, str]:
    """Split a ``.done`` marker's content into ``(destination_role, artifact_path)``.

    The marker's content is normally just the artifact path, in which case
    the destination is ``stage.next_role``. A stage that allows alternate
    destinations (``stage.alt_next_roles``) may instead prefix the content
    with a ``route: <role>\\n`` directive naming one of those roles; the
    remaining line(s) are the artifact path as usual.

    An unrecognized or disallowed ``route:`` value falls back to
    ``stage.next_role`` rather than erroring, since a malformed directive
    shouldn't stall the pipeline.
    """
    first_line, _, rest = content.partition("\n")
    if first_line.startswith("route:"):
        requested = first_line.removeprefix("route:").strip()
        if requested in stage.alt_next_roles:
            return requested, rest.strip()
        return stage.next_role, rest.strip()
    return stage.next_role, content.strip()


def done_marker_path(root: str, role: str) -> str:
    return f"{root.rstrip('/')}/{MARKER_DIR}/{role}.done"


def blocked_marker_path(root: str, role: str) -> str:
    return f"{root.rstrip('/')}/{MARKER_DIR}/{role}.blocked"


# Every role except researcher - these are the panes that accumulate
# context from a run and need clearing when a fresh researcher.done starts
# a new topic in an already-used workspace. See handoff.py's new-topic
# detection.
DOWNSTREAM_ROLES: tuple[str, ...] = ("planner", "principal", "implementer", "reviewer")
