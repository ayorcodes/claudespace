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

``alt_next_roles`` does double duty. Historically it only listed the roles a
given stage might be *answering back* (the case above). It now lists every
other role, full stop: any role can ``.done`` + ``route: <role>`` straight to
any other role's pane whenever the work in front of it plainly needs that
role's specialized operation and isn't a fit for its own documented
``next_role``/bounce targets - a review request handed to a researcher, a
design question that belongs with principal, a goal that spans multiple
roles and belongs with conductor. This is what lets every pane hand work
sideways to whichever specialist actually owns it, instead of either doing
that work itself or dead-ending because the specific ask wasn't one of the
few hardcoded paths a stage happened to enumerate. ``bounce_to`` stays
deliberately narrower than the full role set - it encodes "who can answer a
blocking question so I can finish my own artifact," a tighter relationship
than "who could plausibly do this piece of work," and widening it
indiscriminately would blur rejection/redo semantics that are genuinely
role-specific (see the three cases above).

``reviewer``'s original ``alt_next_roles=("conductor",)`` entry (still
present) is not a bounce but a *forward* success path under a
conductor-driven run: PASS is normally terminal so a single-feature run
always surfaces to the user, but under conductor it routes on so the next
backlog item can dispatch. The same directive also covers reviewer handing
conductor a brand-new goal after post-review findings span several roles -
the marker content is then free-text, typed into conductor's pane exactly as
a human-typed goal would be. That works even in a template with no conductor
pane, and so does every other now-reachable role; see
``handoff._reveal_destination``'s fallback via ``config.CANONICAL_PANES``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

MARKER_DIR = ".claudespace"
SESSION_DIR = "s"


@dataclass(frozen=True, slots=True)
class Stage:
    """One role's position in the pipeline.

    ``next_role`` is who to hand off to on success, ``None`` if terminal.
    ``bounce_to`` lists roles a ``.blocked`` may route back to; when more
    than one is listed the marker must pick via ``route: <role>``.
    ``alt_next_roles`` lists extra roles a ``.done`` may route to instead of
    ``next_role``: both a role answering a bounced question replying to the
    specific asker, and any role handing work sideways to whichever other
    role's specialized operation it actually needs (see the module
    docstring). In practice this is every other role.
    """

    next_role: str | None
    bounce_to: tuple[str, ...] = ()
    alt_next_roles: tuple[str, ...] = ()


# Every role's alt_next_roles below is "every other role" (self and
# next_role omitted as redundant, except where a stage's comment says
# otherwise) - see the module docstring's "alt_next_roles does double duty"
# paragraph. bounce_to is left at its original, deliberately narrower set:
# widening *that* would blur rejection/redo semantics that are genuinely
# role-specific, and it wasn't the gap that motivated opening alt_next_roles
# up in the first place.
PIPELINE: dict[str, Stage] = {
    # Can skip ahead once it has investigated: straight to principal when
    # there are no open product questions, or straight to implementer for a
    # genuinely trivial change (a stricter bar - see researcher.prompt.md).
    # implementer can still bounce back up to principal if the change turns
    # out bigger than it looked. No bounce_to of its own: researcher is the
    # investigative endpoint, with nothing upstream to ask.
    "researcher": Stage(
        next_role="planner",
        alt_next_roles=("principal", "implementer", "reviewer", "conductor"),
    ),
    "planner": Stage(
        next_role="principal",
        bounce_to=("researcher",),
        alt_next_roles=("researcher", "implementer", "reviewer", "conductor"),
    ),
    "principal": Stage(
        next_role="implementer",
        bounce_to=("planner", "researcher"),
        alt_next_roles=("researcher", "planner", "reviewer", "conductor"),
    ),
    "implementer": Stage(
        next_role="reviewer",
        bounce_to=("principal", "planner"),
        alt_next_roles=("researcher", "planner", "principal", "conductor"),
    ),
    # next_role=None so a single run never auto-advances past a human's
    # blind spot; conductor is reachable via `route:` regardless (a
    # conductor-run marker being present is what tells reviewer's own prompt
    # to prefer it on a plain PASS - see reviewer.prompt.md's "On PASS" -
    # but the pipeline itself allows the route unconditionally, same as
    # every other now-reachable role below).
    "reviewer": Stage(
        next_role=None,
        bounce_to=("implementer",),
        alt_next_roles=("conductor", "researcher", "planner", "principal", "implementer"),
    ),
    # Owns backlog bookkeeping and dispatch only. Dispatching an item is
    # mechanically identical to a user starting a fresh /researcher run, so
    # researcher is the default. It may skip to principal or implementer for
    # an item it judges trivial - unlike researcher's skip, made without any
    # investigation, which those roles' prompts compensate for. No
    # bounce_to: conductor dispatches and decomposes, it doesn't produce an
    # artifact that could itself need someone else's answer to finish.
    "conductor": Stage(
        next_role="researcher", alt_next_roles=("principal", "implementer", "planner", "reviewer")
    ),
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


def session_marker_dir(root: str, instance: str | None) -> str:
    """The marker directory for ``root``, scoped to ``instance`` when given.

    ``instance`` present -> ``<root>/.claudespace/s/<instance>``: two
    sessions on the same repo get distinct subtrees, so their markers never
    collide. ``instance is None`` -> ``<root>/.claudespace``, the flat
    legacy path - the backward-compat fallback for panes launched before
    this scoping existed (see ``resolve_root``'s docstring). A pure join:
    never calls ``resolve_root`` itself, so callers control whether ``root``
    is resolved first (see ``resolve_root`` vs. ``worktree_marker_path``).
    """
    base = f"{root.rstrip('/')}/{MARKER_DIR}"
    return f"{base}/{SESSION_DIR}/{instance}" if instance else base


def worktree_marker_path(root: str, instance: str | None = None) -> str:
    """Sentinel a role writes when it creates a run-scoped git worktree.

    Content is the worktree's absolute path. Every prompt is told (see each
    ``*.prompt.md``'s "Worktree" section) to check this file first and
    ``cd`` into its contents before doing anything else, so ``resolve_root``
    below is what makes the rest of the pipeline's path handling honor that
    without every caller re-checking it by hand.

    Keyed on the *unresolved* ``root`` (via ``session_marker_dir`` directly,
    not through ``resolve_root``) - it's the thing resolution reads, so it
    can't itself depend on resolution.
    """
    return f"{session_marker_dir(root, instance)}/worktree"


def resolve_root(root: str, instance: str | None = None) -> str:
    """The effective project root for ``root``'s workspace: the contents of
    its ``worktree`` marker if one exists and still points at a real
    directory, else ``root`` unchanged.

    Used by ``launch_command_text`` to set the pane's ``cd`` target and
    ``CLAUDESPACE_ROOT`` so code work happens in the worktree. **Not** used
    by marker-path builders (``done_marker_path`` etc.) - markers are always
    anchored at the original (unresolved) root so that pipeline state
    written before a worktree exists (e.g. ``conductor-run``) remains
    visible to every role regardless of whether a worktree was created
    mid-run.
    """
    pointer = worktree_marker_path(root, instance)
    if os.path.isfile(pointer):
        with open(pointer) as f:
            candidate = f.read().strip()
        if candidate and os.path.isdir(candidate):
            return candidate
    return root


def done_marker_path(root: str, role: str, instance: str | None = None) -> str:
    return f"{session_marker_dir(root, instance)}/{role}.done"


def blocked_marker_path(root: str, role: str, instance: str | None = None) -> str:
    return f"{session_marker_dir(root, instance)}/{role}.blocked"


def conductor_run_marker_path(root: str, instance: str | None = None) -> str:
    """Sentinel conductor writes when it dispatches its first backlog item.

    Reviewer checks its presence on PASS to decide between ``route:
    conductor`` and terminal. Content is conductor's own bookkeeping - the
    path to whichever ``docs/backlog-<slug>.md`` this run is dispatching
    from, read back on a goal-less invocation so it knows which file to
    resume. ``handoff.py`` only ever checks presence.
    """
    return f"{session_marker_dir(root, instance)}/conductor-run"


def think_marker_path(root: str, instance: str | None = None) -> str:
    """Sentinel written by ``claudespace --think``, removed by a run without it.

    Marks the workspace autonomous: roles that would stop to ask the user
    decide themselves and record the decision in their artifact. Only
    presence is checked.
    """
    return f"{session_marker_dir(root, instance)}/think"


def think_active(root: str, instance: str | None = None) -> bool:
    """Whether a pane launched now should run autonomous (``--think``).

    The same check every role's prompt makes: the ``think`` marker exists, or
    ``CLAUDESPACE_THINK`` is ``1`` in the environment. Backends call this when
    revealing a lazy pane, and the env fallback is what keeps that correct
    across a git worktree. The marker is written under the *original* checkout
    at build time (``workspace._set_think``); once a role follows a worktree it
    re-exports ``CLAUDESPACE_ROOT`` into the worktree, so
    ``think_marker_path`` then points at a session dir the marker was never
    written to. The handoff hook doing the reveal runs inside the *stopping*
    pane, though, whose ``CLAUDESPACE_THINK`` env was set at its own launch and
    travels with it regardless of cwd/worktree - so a ``--think`` run keeps
    revealing autonomous panes even after a worktree redirect, matching how the
    revealed pane's own prompt then reads its autonomy back.
    """
    if os.path.isfile(think_marker_path(root, instance)):
        return True
    return os.environ.get("CLAUDESPACE_THINK") == "1"


# Panes that accumulate per-feature context and need clearing when a fresh
# researcher.done starts a new topic in an already-used workspace. Excludes
# researcher, which is normally the *source* of that marker - wiping it
# would discard the investigation that just produced the artifact (under a
# conductor-driven run it is a destination like any other, handled by
# _handle_new_topic's clear_researcher parameter, not this tuple). Excludes
# conductor, which holds backlog bookkeeping rather than one feature's
# substance.
DOWNSTREAM_ROLES: tuple[str, ...] = ("planner", "principal", "implementer", "reviewer")
