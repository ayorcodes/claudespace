"""The researcher -> planner -> principal -> implementer -> reviewer pipeline.

Defines, for each role, which role is next on success and which role(s) a
rejection or question bounces back to. This is the one place that knows the
pipeline's shape - ``handoff.py`` and the Stop hook just walk this map.

Projects define their own documentation location conventions (e.g. a
``CLAUDE.md`` that says research briefs live in ``docs/research/``), and
each role's prompt already follows those conventions - a fixed artifact
path like ``.claudespace/research.md`` would either duplicate the real
document or, worse, silently never get written because the prompt's
project-standards instruction takes precedence. So ``.claudespace/`` holds
only small marker files - ``<role>.done`` / ``<role>.blocked`` - whose
*contents* are the real, project-root-relative path to wherever the role
actually persisted its document. See ``assets/prompts/*.prompt.md``.

## Bounces vs. questions

``.blocked`` covers two distinct situations, both routed the same way (back
to one of ``bounce_to``, subject to the same auto-handoff toggle as a
forward ``.done`` handoff - see ``handoff.py``):

- **Rejection** (principal -> planner, reviewer -> implementer): the
  downstream role's whole artifact is unacceptable and must be redone.
- **Question** (implementer -> principal or planner; principal -> planner or
  researcher; planner -> researcher): the asking role has a single open
  question that only another role can answer - a design/architecture
  question goes to principal, a product/scope question goes to planner, a
  fact about the repository's current behaviour goes to researcher - and
  does not want its own work redone, just the answer. researcher never asks
  a question itself (bounce_to=()) - it is the pipeline's investigative
  endpoint, nothing upstream of it to ask. Routing a fact-finding question
  to researcher rather than the asking role investigating it directly is
  also the cheaper choice - researcher runs a smaller model at lower effort
  (see roles.py) specifically because targeted investigation is its whole
  job; use this for anything beyond a single trivial grep/read, not just
  when the asking role technically *can't* look for itself.
- **Design request** (implementer -> principal): researcher fast-tracked a
  change straight to implementer as trivial (see `researcher`'s
  `alt_next_roles` below), but implementer discovers mid-implementation
  that it actually needs a real implementation design. Structurally this is
  the same `implementer.blocked` / `route: principal` shape as a question,
  but principal's response differs - it produces a full design and hands
  back to implementer, rather than a one-line answer. See
  implementer.prompt.md and principal.prompt.md for how each tells this
  apart from an ordinary question.

Both write the same ``.blocked`` marker shape (see ``blocked_marker_path``);
the note's content itself says which situation it is, since that changes
how the answering role responds (redo the artifact vs. answer inline and
route back). What both share structurally: the answering role's own
``.done`` must route back to *whichever role asked* rather than falling
through to its normal ``next_role`` - see ``alt_next_roles`` and the
``route:`` directive in ``parse_done_marker``. The note itself names the
asking role so the answering role knows where to route back to (see each
prompt's "Bouncing"/"Answering a bounced question" sections).

## Multi-feature dispatch (reviewer -> conductor)

``reviewer``'s ``alt_next_roles=("conductor",)`` is a different case from
the two above - not a bounce or a question, but a *forward* success path
under a conductor-driven multi-feature run (see ``conductor.prompt.md``).
Reviewer's PASS is normally terminal (``next_role=None``) so a single-feature
run always surfaces to the user; under conductor, PASS instead routes
forward to conductor via the same ``route:`` directive so it can dispatch
the next backlog item without user involvement. Reviewer's prompt decides
which applies by checking for ``.claudespace/conductor-run``.

The same ``route: conductor`` directive covers a third case: reviewer
handing conductor a brand-new goal (not an existing dispatched item) when
post-review follow-up findings span more than one role's territory - see
reviewer.prompt.md's "Post-review follow-up" section. The marker's content
is then a free-text goal description rather than a review path, typed into
conductor's pane exactly as a human-typed goal would be, so conductor runs
its ordinary first-invocation flow (scan, decompose, checkpoint) on it. This
works even when the workspace's template has no conductor pane at all - see
``handoff._reveal_destination``'s cross-template fallback via
``config.CANONICAL_PANES``, which spins one up on demand.
"""

from __future__ import annotations

from dataclasses import dataclass

MARKER_DIR = ".claudespace"


@dataclass(frozen=True, slots=True)
class Stage:
    """One role's position in the pipeline.

    ``next_role`` is who to hand off to on success, or ``None`` if this role
    is terminal (reviewer - always surfaced to the user, never
    auto-advanced). ``bounce_to`` lists the role(s) a ``.blocked`` marker is
    allowed to route back to - a rejection or a question, see the module
    docstring - or is empty if this role has no bounce path. When more than
    one role is listed, the marker's content picks which one via the same
    ``route: <role>`` directive ``.done`` markers use (see
    ``parse_blocked_marker``).

    ``alt_next_roles`` lists other roles this stage's ``.done`` marker is
    allowed to route to instead of ``next_role``, when the marker's content
    requests it (see ``parse_done_marker``) - this is what lets a role
    that's answering a bounced question route back to the specific asker
    instead of falling through to its normal forward stage. Empty for
    stages with only one possible destination.
    """

    next_role: str | None
    bounce_to: tuple[str, ...] = ()
    alt_next_roles: tuple[str, ...] = ()


PIPELINE: dict[str, Stage] = {
    # A researcher investigating a well-scoped engineering change (bug fix,
    # refactor, infra tweak) with no open product questions can route
    # straight to principal - a Planning Brief would just restate facts the
    # researcher already confirmed. See researcher.prompt.md's routing
    # guidance and parse_done_marker's "route:" directive.
    #
    # For a genuinely trivial change - small, well-understood, no
    # architectural decision to make - researcher can skip principal too and
    # route straight to implementer, since a full implementation design
    # would just restate the obvious fix. This is a stricter bar than the
    # principal skip: see researcher.prompt.md's routing guidance. implementer
    # can still bounce back up to principal on its own (see implementer's
    # bounce_to) if the change turns out to be bigger than it looked once
    # implementer starts working.
    #
    # researcher never has a bounce_to of its own - it's the pipeline's
    # investigative endpoint, nothing further upstream to ask - but it does
    # answer questions bounced *to* it: planner and principal can both ask
    # it a narrow fact-finding question (see their own bounce_to below)
    # instead of investigating the repository themselves, which neither is
    # well-positioned to do (planner is forbidden from it outright; principal
    # is discouraged from repeating investigation - see each prompt's
    # Never/Inputs). Both are in alt_next_roles alongside principal (used for
    # researcher's own forward skip-ahead, unrelated to question-answering)
    # and implementer (routing straight to implementer as a trivial skip),
    # so researcher.done's `route:` directive can send an answer back to
    # whichever of them asked.
    "researcher": Stage(
        next_role="planner", alt_next_roles=("principal", "implementer", "planner")
    ),
    # planner's default forward target is principal (a fresh Planning
    # Brief, or an answer to a question principal bounced up - `route:
    # principal` matches the default so no alt_next_roles entry is needed
    # for that case). implementer can also bounce a product-scope question
    # straight to planner (see implementer's bounce_to); planner then must
    # route its answer back to implementer specifically, hence implementer
    # in alt_next_roles here too. planner itself can bounce a narrow
    # fact-finding question up to researcher via bounce_to - planner is
    # forbidden from investigating the repository itself (see its Never
    # list), so a missing fact has nowhere else to go.
    "planner": Stage(
        next_role="principal", bounce_to=("researcher",), alt_next_roles=("implementer",)
    ),
    # principal bounces a whole rejected Planning Brief back to planner for
    # a redo, or a narrow fact-finding question to researcher instead of
    # investigating the repository itself (see Inputs' "do not repeat
    # repository investigation" guidance) - both via bounce_to, disambiguated
    # by the `route:` directive on `principal.blocked` now that there's more
    # than one target. principal also needs to route *back* to implementer
    # when answering a question implementer bounced up to it (or a design
    # implementer requested mid-flight, see implementer's bounce_to), hence
    # implementer in alt_next_roles alongside the normal next_role.
    "principal": Stage(
        next_role="implementer",
        bounce_to=("planner", "researcher"),
        alt_next_roles=("implementer",),
    ),
    # implementer previously had no bounce path at all - an implementer
    # stuck on a design question or a product/scope question had nowhere to
    # send it and no way to signal "I need an answer, not a redo." Routes
    # to whichever of principal/planner owns the question's domain.
    #
    # This same bounce_to also covers a distinct situation: researcher
    # routed straight to implementer for what looked like a trivial change
    # (skipping principal entirely, see researcher's alt_next_roles above),
    # but implementer discovers mid-implementation that the change is
    # bigger than it looked and a real implementation design is needed
    # after all. That's not a narrow question - implementer wants principal
    # to take over and design the thing - but it bounces through the same
    # `principal.blocked` marker and `route: principal` directive as an
    # ordinary question; principal's prompt tells the two apart by the
    # note's content and produces a full design rather than a one-line
    # answer when that's what's being asked for. See implementer.prompt.md's
    # "Bouncing a question" and "Requesting a design" sections and
    # principal.prompt.md's "Answering a question bounced from implementer".
    "implementer": Stage(next_role="reviewer", bounce_to=("principal", "planner")),
    # reviewer's PASS is normally terminal (next_role=None) - always
    # surfaced to the user, never auto-advanced, so a single pipeline run
    # never silently keeps going past a human's blind spot. Under a
    # conductor-driven multi-feature run, though, PASS should feed back to
    # conductor so it can dispatch the next backlog item - see
    # conductor.prompt.md and reviewer.prompt.md's "On PASS" section, which
    # checks for .claudespace/conductor-run before using this route.
    "reviewer": Stage(next_role=None, bounce_to=("implementer",), alt_next_roles=("conductor",)),
    # conductor owns backlog bookkeeping and dispatch only - it never
    # researches/plans/designs/implements/reviews itself. Its default
    # forward target is researcher, matching claudespace's normal entry
    # point: dispatching the next backlog item is mechanically identical to
    # a user starting a fresh /researcher run, just with the item
    # description as the topic instead of a user-typed request.
    #
    # For an item conductor itself judges trivial or well-scoped enough at
    # dispatch time (see conductor.prompt.md's "Choosing where to dispatch"),
    # it can route straight to principal or implementer instead, skipping
    # researcher (and, for the implementer case, planner and principal too)
    # the same way researcher's own alt_next_roles let it skip ahead once
    # it has investigated. Unlike researcher's skip, conductor's is made
    # without any investigation at all - implementer/principal's prompts
    # account for that by doing their own minimal investigation when they
    # were dispatched with only a backlog item description and no brief.
    "conductor": Stage(next_role="researcher", alt_next_roles=("principal", "implementer")),
}


def _normalize_artifact(text: str) -> str:
    """Collapse a marker's artifact-path portion to a single line.

    Prompts tell each role a marker's content is "the project-root-relative
    path" (a single line), but nothing stops a model from writing extra
    blank lines, trailing notes, or other stray whitespace into the file -
    ``.strip()`` alone only trims the ends, so an internal newline survives
    into the returned artifact string untouched. That string is later typed
    verbatim into the destination pane's input by ``iterm.send_role_prompt``
    (see ``handoff.py``) - and a prompt containing a literal newline is
    exactly what makes Claude Code's TUI treat the injected text as a
    multi-line paste (rendered as a `[Pasted text #N]` chip) instead of a
    normal typed command line, which the handoff's submit-retry logic
    doesn't recognize as "still unsubmitted." Collapsing every run of
    whitespace (including newlines) to a single space, then stripping,
    guarantees the artifact string handed to the destination pane is always
    exactly one line, regardless of how the source marker file happened to
    be formatted.
    """
    return " ".join(text.split())


def parse_done_marker(content: str, *, stage: Stage) -> tuple[str, str]:
    """Split a ``.done`` marker's content into ``(destination_role, artifact_path)``.

    The marker's content is normally just the artifact path, in which case
    the destination is ``stage.next_role``. A stage that allows alternate
    destinations (``stage.alt_next_roles``) may instead prefix the content
    with a ``route: <role>\\n`` directive naming one of those roles; the
    remaining line(s) are the artifact path as usual.

    An unrecognized or disallowed ``route:`` value falls back to
    ``stage.next_role`` rather than erroring, since a malformed directive
    shouldn't stall the pipeline. The returned artifact path is always
    single-line - see ``_normalize_artifact``.
    """
    first_line, _, rest = content.partition("\n")
    if first_line.startswith("route:"):
        requested = first_line.removeprefix("route:").strip()
        if requested in stage.alt_next_roles:
            return requested, _normalize_artifact(rest)
        return stage.next_role, _normalize_artifact(rest)
    return stage.next_role, _normalize_artifact(content)


def parse_blocked_marker(content: str, *, stage: Stage) -> tuple[str | None, str]:
    """Split a ``.blocked`` marker's content into ``(destination_role, note_path)``.

    Mirrors ``parse_done_marker``'s ``route:`` directive, but for the
    ``.blocked`` side: a stage with more than one ``bounce_to`` role (e.g.
    implementer, which can bounce to either principal or planner) must say
    which one via a ``route: <role>`` first line; the remaining line(s) are
    the note path as usual. A stage with exactly one ``bounce_to`` role
    doesn't need the directive - that single role is used unconditionally.

    Returns ``(None, note_path)`` if the destination can't be determined
    (no ``bounce_to`` at all, or more than one with no/invalid ``route:``)
    so the caller can skip the handoff rather than guess - unlike
    ``parse_done_marker``, there is no single obvious fallback when a role
    has multiple bounce targets and doesn't say which. The returned note
    path is always single-line - see ``_normalize_artifact``.
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


def done_marker_path(root: str, role: str) -> str:
    return f"{root.rstrip('/')}/{MARKER_DIR}/{role}.done"


def blocked_marker_path(root: str, role: str) -> str:
    return f"{root.rstrip('/')}/{MARKER_DIR}/{role}.blocked"


def conductor_run_marker_path(root: str) -> str:
    """Path to the sentinel conductor writes on dispatching its first
    backlog item, and reviewer checks on PASS to decide whether to route
    back to conductor (``route: conductor``) or behave as terminal, as in
    the normal single-feature flow. See ``conductor.prompt.md`` and
    ``reviewer.prompt.md``'s "On PASS" section.

    Only presence is checked by this module (``handoff.py``) - the content
    is conductor's own bookkeeping, not something the Stop hook parses.
    Conductor writes the project-root-relative path to whichever
    ``docs/backlog-<slug>.md`` this run is dispatching from (a workspace can
    accumulate several backlog files across unrelated goals over its
    lifetime, see conductor.prompt.md's "Which backlog?"), and reads it back
    on a goal-less invocation to know which file to resume without guessing.
    """
    return f"{root.rstrip('/')}/{MARKER_DIR}/conductor-run"


def think_marker_path(root: str) -> str:
    """Path to the sentinel written by ``claudespace --think`` (removed by a
    run without it), marking this workspace as autonomous: roles that would
    normally stop and ask the user a clarifying question instead decide the
    answer themselves, at the level of a 30-year staff engineer at a
    top-tier shop, and record it as an explicit assumption/decision in
    their artifact. Planner is the role this matters most for - see
    ``planner.prompt.md``'s "Autonomous mode". Content is irrelevant - only
    presence is checked.
    """
    return f"{root.rstrip('/')}/{MARKER_DIR}/think"


# Every role except researcher - these are the panes that accumulate
# context from a run and need clearing when a fresh researcher.done starts
# a new topic in an already-used workspace. See handoff.py's new-topic
# detection. Excludes researcher itself because it's normally the *source*
# of the fresh marker that triggers this clearing - wiping its own pane
# would discard the investigation that just produced the artifact. Under a
# conductor-driven run, though, researcher's pane is a destination like any
# other and does get cleared - see _handle_new_topic's clear_researcher
# parameter in handoff.py, not this tuple. Deliberately excludes conductor:
# conductor doesn't accumulate
# per-feature conversational context the way the others do (it dispatches
# backlog items rather than working on one feature's substance), so
# clearing it on every new-topic detection would discard its backlog
# bookkeeping for no benefit.
DOWNSTREAM_ROLES: tuple[str, ...] = ("planner", "principal", "implementer", "reviewer")
