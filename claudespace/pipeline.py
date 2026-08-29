"""The researcher -> planner -> principal -> implementer -> reviewer pipeline.

The one place that knows the pipeline's shape; ``handoff.py`` and the Stop
hook just walk this map.

Markers, not artifacts. Projects define their own doc conventions (a
``CLAUDE.md`` saying briefs live in ``docs/research/``), and each role's
prompt follows them - so a fixed path like ``.claudespace/research.md``
would either duplicate the real document or never get written at all.
``.claudespace/`` holds only ``<role>.done`` / ``<role>.blocked`` markers
whose *contents* are the project-root-relative path to the real document.

``.blocked`` covers three situations, all routed the same way (to one of
``bounce_to``, subject to the same auto-handoff toggle as a forward
``.done``). The note's content tells the answering role which it is:

- **Rejection** (principal -> planner, reviewer -> implementer): the
  artifact is unacceptable and must be redone.
- **Question** (implementer -> principal/planner; principal ->
  planner/researcher; planner -> researcher): one open question only another
  role can answer; the asker does not want its own work redone. Routing a
  fact-finding question to researcher is also the cheaper choice - it runs a
  smaller model at lower effort precisely because targeted investigation is
  its whole job.
- **Design request** (implementer -> principal): researcher fast-tracked a
  change as trivial, but implementer found it needs a real design. Same
  marker shape as a question; principal produces a full design instead of a
  one-line answer.

Whatever the case, the answering role's own ``.done`` must route back to
whoever asked rather than falling through to its normal ``next_role`` - see
``alt_next_roles`` and the ``route:`` directive.

``reviewer``'s ``alt_next_roles=("conductor",)`` is not a bounce but a
*forward* success path under a conductor-driven run: PASS is normally
terminal so a single-feature run always surfaces to the user, but under
conductor it routes on so the next backlog item can dispatch. The same
directive also covers reviewer handing conductor a brand-new goal after
post-review findings span several roles - the marker content is then
free-text, typed into conductor's pane exactly as a human-typed goal would
be. That works even in a template with no conductor pane; see
``handoff._reveal_destination``'s fallback via ``config.CANONICAL_PANES``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

MARKER_DIR = ".claudespace"


@dataclass(frozen=True, slots=True)
class Stage:
    """One role's position in the pipeline.

    ``next_role`` is who to hand off to on success, ``None`` if terminal.
    ``bounce_to`` lists roles a ``.blocked`` may route back to; when more
    than one is listed the marker must pick via ``route: <role>``.
    ``alt_next_roles`` lists extra roles a ``.done`` may route to instead of
    ``next_role`` - this is what lets a role answering a bounced question
    reply to the specific asker.
    """

    next_role: str | None
    bounce_to: tuple[str, ...] = ()
    alt_next_roles: tuple[str, ...] = ()


PIPELINE: dict[str, Stage] = {
    # Can skip ahead once it has investigated: straight to principal when
    # there are no open product questions, or straight to implementer for a
    # genuinely trivial change (a stricter bar - see researcher.prompt.md).
    # implementer can still bounce back up to principal if the change turns
    # out bigger than it looked. No bounce_to of its own: researcher is the
    # investigative endpoint, with nothing upstream to ask. It does answer
    # questions bounced *to* it by planner and principal, hence those in
    # alt_next_roles alongside its own skip-ahead targets.
    "researcher": Stage(
        next_role="planner", alt_next_roles=("principal", "implementer", "planner")
    ),
    # `route: principal` matches the default, so no alt entry is needed for
    # answering principal. implementer is listed because it can bounce a
    # product-scope question straight here and needs the answer back.
    # planner may not investigate the repository itself (see its Never
    # list), so a missing fact has nowhere to go but researcher.
    "planner": Stage(
        next_role="principal", bounce_to=("researcher",), alt_next_roles=("implementer",)
    ),
    # Rejects a whole Planning Brief back to planner, or asks researcher a
    # narrow fact-finding question rather than re-investigating itself -
    # two targets, so `route:` disambiguates.
    "principal": Stage(
        next_role="implementer",
        bounce_to=("planner", "researcher"),
        alt_next_roles=("implementer",),
    ),
    # Routes to whichever of principal/planner owns the question's domain.
    # The same path carries a design request (see the module docstring).
    "implementer": Stage(next_role="reviewer", bounce_to=("principal", "planner")),
    # next_role=None so a single run never auto-advances past a human's
    # blind spot; conductor is reachable via `route:` when a conductor-run
    # marker is present. See reviewer.prompt.md's "On PASS".
    "reviewer": Stage(next_role=None, bounce_to=("implementer",), alt_next_roles=("conductor",)),
    # Owns backlog bookkeeping and dispatch only. Dispatching an item is
    # mechanically identical to a user starting a fresh /researcher run, so
    # researcher is the default. It may skip to principal or implementer for
    # an item it judges trivial - unlike researcher's skip, made without any
    # investigation, which those roles' prompts compensate for.
    "conductor": Stage(next_role="researcher", alt_next_roles=("principal", "implementer")),
}


def _normalize_artifact(text: str) -> str:
    """Collapse a marker's artifact path to a single line.

    Nothing stops a model writing stray blank lines or trailing notes into a
    marker, and ``.strip()`` only trims the ends. The result is typed
    verbatim into the destination pane, and a literal newline there makes
    Claude Code's TUI treat the input as a multi-line paste (a
    ``[Pasted text #N]`` chip) rather than a typed command - which the
    submit-retry logic does not recognise as "still unsubmitted".
    """
    return " ".join(text.split())


def parse_done_marker(content: str, *, stage: Stage) -> tuple[str, str]:
    """Split a ``.done`` marker into ``(destination_role, artifact_path)``.

    Content is normally just the path, destined for ``stage.next_role``. A
    stage with ``alt_next_roles`` may prefix a ``route: <role>\\n`` line
    naming one of them. An unrecognised or disallowed ``route:`` falls back
    to ``next_role`` rather than erroring - a malformed directive shouldn't
    stall the pipeline.
    """
    first_line, _, rest = content.partition("\n")
    if first_line.startswith("route:"):
        requested = first_line.removeprefix("route:").strip()
        if requested in stage.alt_next_roles:
            return requested, _normalize_artifact(rest)
        return stage.next_role, _normalize_artifact(rest)
    return stage.next_role, _normalize_artifact(content)


def parse_blocked_marker(content: str, *, stage: Stage) -> tuple[str | None, str]:
    """Split a ``.blocked`` marker into ``(destination_role, note_path)``.

    Mirrors ``parse_done_marker``'s ``route:`` directive. A stage with
    exactly one ``bounce_to`` needs no directive. Returns ``(None, path)``
    when the destination can't be determined, so the caller skips rather
    than guesses - unlike a ``.done``, there is no obvious fallback when a
    role has several bounce targets and doesn't say which.
    """
    first_line, _, rest = content.partition("\n")
    if first_line.startswith("route:"):
        requested = first_line.removeprefix("route:").strip()
        if requested in stage.bounce_to:
            return requested, _normalize_artifact(rest)
        return (
            None if len(stage.bounce_to) != 1 else stage.bounce_to[0]
        ), _normalize_artifact(rest)
    if len(stage.bounce_to) == 1:
        return stage.bounce_to[0], _normalize_artifact(content)
    return None, _normalize_artifact(content)


def worktree_marker_path(root: str) -> str:
    """Sentinel a role writes when it creates a run-scoped git worktree.

    Content is the worktree's absolute path. Every prompt is told (see each
    ``*.prompt.md``'s "Worktree" section) to check this file first and
    ``cd`` into its contents before doing anything else, so ``resolve_root``
    below is what makes the rest of the pipeline's path handling honor that
    without every caller re-checking it by hand.
    """
    return f"{root.rstrip('/')}/{MARKER_DIR}/worktree"


def resolve_root(root: str) -> str:
    """The effective project root for ``root``'s workspace: the contents of
    its ``worktree`` marker if one exists and still points at a real
    directory, else ``root`` unchanged.

    ``CLAUDESPACE_ROOT`` (what every pane's env var and every caller in this
    module is handed) is fixed at workspace-creation time and never changes
    for the life of the workspace - but a role can be told mid-run to do its
    work in a freshly created git worktree (see the "Worktree" section now
    in every ``*.prompt.md``), which lives at a different path on disk.
    Without this indirection, a role that ``cd``'d into that worktree writes
    its markers and artifacts there while every path this module builds
    still points at the original ``CLAUDESPACE_ROOT`` - the exact drift that
    left a downstream role unable to find a researcher's brief after a
    worktree-scoped run (docs/research/2026-08-29-vat-exclusion...). Routing
    every marker-path builder through this function means a worktree, once
    recorded, is honored everywhere without each caller re-checking it.
    """
    pointer = worktree_marker_path(root)
    if os.path.isfile(pointer):
        with open(pointer) as f:
            candidate = f.read().strip()
        if candidate and os.path.isdir(candidate):
            return candidate
    return root


def done_marker_path(root: str, role: str) -> str:
    return f"{resolve_root(root).rstrip('/')}/{MARKER_DIR}/{role}.done"


def blocked_marker_path(root: str, role: str) -> str:
    return f"{resolve_root(root).rstrip('/')}/{MARKER_DIR}/{role}.blocked"


def conductor_run_marker_path(root: str) -> str:
    """Sentinel conductor writes when it dispatches its first backlog item.

    Reviewer checks its presence on PASS to decide between ``route:
    conductor`` and terminal. Content is conductor's own bookkeeping - the
    path to whichever ``docs/backlog-<slug>.md`` this run is dispatching
    from, read back on a goal-less invocation so it knows which file to
    resume. ``handoff.py`` only ever checks presence.
    """
    return f"{resolve_root(root).rstrip('/')}/{MARKER_DIR}/conductor-run"


def think_marker_path(root: str) -> str:
    """Sentinel written by ``claudespace --think``, removed by a run without it.

    Marks the workspace autonomous: roles that would stop to ask the user
    decide themselves and record the decision in their artifact. Only
    presence is checked.
    """
    return f"{resolve_root(root).rstrip('/')}/{MARKER_DIR}/think"


# Panes that accumulate per-feature context and need clearing when a fresh
# researcher.done starts a new topic in an already-used workspace. Excludes
# researcher, which is normally the *source* of that marker - wiping it
# would discard the investigation that just produced the artifact (under a
# conductor-driven run it is a destination like any other, handled by
# _handle_new_topic's clear_researcher parameter, not this tuple). Excludes
# conductor, which holds backlog bookkeeping rather than one feature's
# substance.
DOWNSTREAM_ROLES: tuple[str, ...] = ("planner", "principal", "implementer", "reviewer")
