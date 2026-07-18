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
    """

    next_role: str | None
    bounce_to: str | None = None


PIPELINE: dict[str, Stage] = {
    "researcher": Stage(next_role="planner"),
    "planner": Stage(next_role="principal"),
    "principal": Stage(next_role="implementer", bounce_to="planner"),
    "implementer": Stage(next_role="reviewer"),
    "reviewer": Stage(next_role=None, bounce_to="implementer"),
}


def done_marker_path(root: str, role: str) -> str:
    return f"{root.rstrip('/')}/{MARKER_DIR}/{role}.done"


def blocked_marker_path(root: str, role: str) -> str:
    return f"{root.rstrip('/')}/{MARKER_DIR}/{role}.blocked"
