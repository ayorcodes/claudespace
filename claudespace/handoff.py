"""Pipeline handoff: send the next role's prompt into its pane.

Invoked by the ``claudespace-handoff`` Stop hook after a pane finishes a
turn. Reads which role/workspace it's running in from
``CLAUDESPACE_ROLE``/``CLAUDESPACE_ROOT`` (set when the pane was launched -
see ``backends/common.py``'s ``launch_command_text``), checks for a fresh
completion marker, and if one exists, prefills (and possibly submits) the
destination pane's prompt with a reference to the real artifact path the
marker names.

Forward handoffs (a role finished successfully, ``<role>.done`` exists) and
backward handoffs (a role bounced work back, ``<role>.blocked`` exists)
both auto-submit only if the workspace's auto-handoff toggle is on;
otherwise they only prefill.

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

If the handoff mechanism itself raises (a transient backend failure, say),
that failure used to be swallowed - logged to stderr, which nothing
surfaces to the user, so it read identically to "nothing needed handing
off." It's now still visible whenever a marker genuinely looks missing: a
filesystem-only fallback check (no backend calls, so it can't fail the same
way) blocks the Stop and tells the role the handoff plumbing broke, instead
of stopping clean. See ``_maybe_nag_after_handoff_error``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

from claudespace.backends import get_backend
from claudespace.backends.base import Pane, TerminalBackend
from claudespace.backends.common import role_prompt_prefix
from claudespace.config import CANONICAL_PANES, get_template
from claudespace.pipeline import (
    DOWNSTREAM_ROLES,
    PIPELINE,
    blocked_marker_path,
    conductor_run_marker_path,
    done_marker_path,
    parse_blocked_marker,
    parse_done_marker,
)

logger = logging.getLogger(__name__)

# Where each role's marker files record they've already been handed off, so
# a Stop hook firing again on the same marker (e.g. the user asks the pane
# a follow-up question after it already reported completion) doesn't
# re-trigger the handoff.
HANDOFF_STATE_SUFFIX = ".handed-off"

# Marks that this role has already been reminded about a missing marker, so
# a role that's genuinely done with nothing to hand off (or stuck waiting on
# the user for something outside the pipeline) doesn't get nagged on every
# subsequent Stop.
NAG_STATE_SUFFIX = ".nagged"

# How long a ``.nagged`` sentinel silences re-nagging for. The nag is not
# once-*ever*: a role that ends one turn with no marker (an early pause, a
# permission prompt), then works for many minutes and ends its *real*
# completion turn still with no marker, must be nagged again at that terminal
# stop - otherwise the pipeline goes silent exactly when it matters (the
# motivating "implementer finished after a long turn, no handoff, no nag"
# bug). Keying the re-nag on elapsed wall-clock re-fires at that later stop
# while still swallowing the tight block->reprompt->immediate-stop loop (which
# turns over in seconds, far inside this window), so a role that's genuinely
# parked waiting on the user isn't spammed. See ``_maybe_nag_missing_marker``.
NAG_COOLDOWN_SECONDS = 240.0

# Marks that a fresh .done/.blocked has already fired its terminal-state
# notification (D6/FR6), mtime-compared to the marker exactly like
# HANDOFF_STATE_SUFFIX, so a retriggered Stop on the same marker doesn't
# re-notify.
NOTIFIED_STATE_SUFFIX = ".notified"


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


def _marker_present_and_handed(path: str) -> bool:
    """Whether ``path`` exists and its ``.handed-off`` sentinel proves the
    handoff it names already went out - i.e. exactly the "stale marker" case
    ``_read_fresh_marker`` treats identically to "never existed."

    A role that insists from memory that it already handed off, while
    ``_read_fresh_marker`` reports no fresh marker, is this exact state: the
    sentinel is only ever written by ``_mark_handed_off`` right after a
    successful ``_send_handoff`` (see module docstring's AD-1) - its presence
    is proof, not a guess. Nagging here would ask the role to re-litigate a
    handoff that already landed.
    """
    if not os.path.isfile(path):
        return False
    state_path = path + HANDOFF_STATE_SUFFIX
    return os.path.isfile(state_path) and os.path.getmtime(path) <= os.path.getmtime(
        state_path
    )


def _already_notified(marker_path: str) -> bool:
    notified_path = marker_path + NOTIFIED_STATE_SUFFIX
    return os.path.isfile(notified_path) and os.path.getmtime(
        marker_path
    ) <= os.path.getmtime(notified_path)


def _mark_notified(marker_path: str) -> None:
    open(marker_path + NOTIFIED_STATE_SUFFIX, "w").close()


async def _notify_terminal_state(
    backend: TerminalBackend,
    *,
    root: str,
    instance: str,
    role: str,
    marker_path: str,
    kind: str,
) -> None:
    """FR6/AC5: fire once, the moment a fresh ``.done``/``.blocked`` marker
    is observed - deduped by ``.notified`` (mtime-compared to the marker,
    same idiom as ``.handed-off``), so a retriggered Stop on the same marker
    doesn't re-notify. Fires unconditionally on a fresh marker, even when
    the handoff itself can't find a destination pane (Edge Cases: "no
    destination pane" still lets the user learn the role finished).
    """
    if _already_notified(marker_path):
        return
    await backend.notify(
        title=f"claudespace: {role} {kind}",
        message=f"'{role}' {kind} in workspace {root}.",
        marker=root,
        instance=instance,
    )
    _mark_notified(marker_path)


def _already_nagged(done_path: str) -> bool:
    return os.path.isfile(done_path + NAG_STATE_SUFFIX)


def _mark_nagged(done_path: str) -> None:
    # Unlike _mark_handed_off (always called right after successfully
    # reading a marker that just proved its directory exists), this fires
    # on the *missing*-marker path - the role never wrote its .done/.blocked
    # at all, so under per-session scoping (pipeline.session_marker_dir)
    # nothing may have created its scoped s/<instance>/ subdirectory yet.
    os.makedirs(os.path.dirname(done_path), exist_ok=True)
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


def _print_handoff_error_block(role: str, error: BaseException) -> None:
    """Emit the Stop-hook JSON that blocks the stop after the handoff
    mechanism itself failed (an exception during ``_run`` - see ``main``'s
    exception handler), telling ``role`` about the failure explicitly
    instead of letting the turn end silently.

    The normal path (``_maybe_nag_missing_marker``) can only nag about a
    missing marker when it actually gets to run; if a backend RPC call
    somewhere in ``_send_handoff``/``_maybe_nag_missing_marker`` throws (a
    transient API hiccup, a dropped connection, anything), the entire nag
    check dies with it and used to just log to stderr - invisible to
    everyone, since Stop-hook stderr isn't surfaced anywhere the user would
    see it. From the model's and the user's point of view that read
    identically to "nothing needed handing off," which is exactly the
    silent-stop failure the nag mechanism exists to prevent in the first
    place. This makes that failure visible via the same block-the-Stop
    channel instead, so a turn doesn't end looking clean when the handoff
    plumbing actually broke underneath it.
    """
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    f"The claudespace handoff mechanism for role '{role}' "
                    f"raised an error while checking whether your work "
                    f"needs to be handed off: {error!r}. This is a bug in "
                    "claudespace's tooling, not something you did wrong - "
                    "but it means the automatic handoff to the next role "
                    "may not have happened. If you already created your "
                    "completion marker (`.claudespace/<role>.done` or "
                    "`.blocked`) per the Completion section of your "
                    "instructions, you can likely ignore this and stop "
                    "normally - just be aware the automatic handoff may "
                    "need to be triggered manually this time. If you have "
                    "not yet created that marker, create it now before "
                    "stopping."
                ),
            }
        )
    )


def has_unhanded_forward_work(
    root: str, role: str, instance: str | None
) -> bool:
    """Whether ``role`` finished a turn with a forward handoff still owed but
    no marker to trigger it - i.e. a *silent completion*: the role has
    somewhere to hand off to, yet left neither a fresh ``.done``/``.blocked``
    marker (one the Stop hook would act on) nor an already-handed one (proof
    the handoff already went out).

    This is the shared, backend-free predicate behind both the Stop-hook's
    post-error fallback (``_maybe_nag_after_handoff_error``) and the
    watchdog's idle-completion backstop (``watchdog._check_once``) - the two
    places that must recognise "done, idle, nothing handed off" without any
    backend call. The forward-stage test mirrors
    ``_maybe_nag_missing_marker`` exactly (``stage.next_role``, plus the
    conductor-driven-reviewer exception), *not* ``_send_handoff``'s broader
    ``next_role or alt_next_roles``: a reviewer that finished a plain
    (non-conductor) run and is parked at the prompt with no marker is
    correctly *done* - PASS is terminal - and must not be flagged as unhanded
    work.
    """
    stage = PIPELINE.get(role)
    if stage is None:
        return False
    conductor_driven_reviewer = role == "reviewer" and os.path.isfile(
        conductor_run_marker_path(root, instance)
    )
    if stage.next_role is None and not conductor_driven_reviewer:
        return False
    done_path = done_marker_path(root, role, instance)
    blocked_path = blocked_marker_path(root, role, instance)
    if _read_fresh_marker(done_path) or (
        stage.bounce_to and _read_fresh_marker(blocked_path)
    ):
        return False
    if _marker_present_and_handed(done_path) or (
        stage.bounce_to and _marker_present_and_handed(blocked_path)
    ):
        return False
    return True


def _maybe_nag_after_handoff_error(
    root: str | None, role: str, instance: str | None
) -> bool:
    """Filesystem-only, backend-free fallback check run from ``main``'s
    exception handler when ``_run`` (the normal handoff/nag path) itself
    raised. Deliberately makes no backend API calls - the whole point is to
    still say *something* useful even when the backend side is what's
    broken, so this fallback can't fail the same way the thing it's
    covering for just did.

    Returns ``True`` (and prints a Stop-block) whenever ``role`` has a
    forward pipeline stage and left no marker to hand off with
    (``has_unhanded_forward_work``), minus the auto-handoff toggle
    (unreachable here without a backend call) and minus the "already nagged"
    dedup (best-effort only; a repeated nag on back-to-back hook failures is
    an acceptable cost next to silently saying nothing). ``False`` if
    ``root`` is missing, the role is unknown, or a marker already exists -
    nothing useful to report.
    """
    if root is None:
        return False
    return has_unhanded_forward_work(root, role, instance)


async def _old_run_finished(
    backend: TerminalBackend, *, root: str, instance: str, run_started: float | None
) -> bool:
    """Whether the run that started at ``run_started`` already reached
    reviewer PASS, i.e. it's safe to silently clear panes for a new topic.

    ``reviewer.done`` is never marked ``.handed-off`` (reviewer is
    terminal - see ``PIPELINE``), so its mere existence can't distinguish
    "this run just finished" from "some run finished ages ago and nobody
    cleaned up." Comparing its mtime against when the current run started
    resolves that: a `reviewer.done` written after the run began means
    *this* run reached PASS.
    """
    if run_started is None:
        return True
    done_path = done_marker_path(root, "reviewer", instance)
    if not os.path.isfile(done_path):
        return False
    return os.path.getmtime(done_path) >= run_started


async def _handle_new_topic(
    backend: TerminalBackend,
    *,
    root: str,
    instance: str,
    doc_artifact: str,
    force: bool = False,
    clear_researcher: bool = False,
) -> str | None:
    """Detect whether ``doc_artifact`` (a fresh researcher.done's contents,
    or a fresh conductor.done's backlog-item description when conductor is
    dispatching the next item) starts a new topic in an already-used
    workspace, and if so, either clear the downstream panes (old run
    finished) or return a warning to prepend to the handoff prompt instead
    of clearing (old run still in flight - never discard unfinished work
    silently).

    Returns ``None`` if this continues the workspace's current run (no
    action needed), or a warning string to prefix the handoff prompt with
    if the old run was still in flight. In the warning case it still records
    ``doc_artifact`` as the current run, so the warning + suppressed
    auto-submit fire once for a genuinely new topic and a retrigger of that
    same doc then resumes normally (takes the ``current_doc == doc_artifact``
    fast-path) instead of re-warning every time.

    ``force`` (set for conductor-driven runs) skips the in-flight check
    entirely: the conductor owns the pipeline and is moving to the next
    backlog item on its own authority, so downstream panes are cleared and
    overwritten unconditionally rather than asking a human to confirm.

    ``clear_researcher`` additionally resets researcher's own pane. Only
    pass this for a conductor-dispatched item, never for a fresh
    researcher.done - when researcher itself just produced the artifact
    that triggered this call, its pane holds the very investigation that
    produced it, and clearing it would wipe that work. But when conductor
    is doing the dispatching, researcher's pane is reused across backlog
    items the same way planner/principal/implementer/reviewer's are (see
    DOWNSTREAM_ROLES) - excluding it would leave conversation from every
    earlier item silently piling up in that one pane while the others reset
    normally.
    """
    current_doc, run_started = await backend.get_run_doc(marker=root, instance=instance)

    if current_doc is None or current_doc == doc_artifact:
        await backend.set_run_doc(
            marker=root, instance=instance, doc=doc_artifact, started_at=time.time()
        )
        return None

    if force or await _old_run_finished(
        backend, root=root, instance=instance, run_started=run_started
    ):
        roles_to_clear = (
            DOWNSTREAM_ROLES + ("researcher",) if clear_researcher else DOWNSTREAM_ROLES
        )
        logger.info(
            "New topic '%s' replaces run '%s' (force=%s) - clearing panes: %s",
            doc_artifact,
            current_doc,
            force,
            roles_to_clear,
        )
        for downstream_role in roles_to_clear:
            pane = await backend.find_role_pane(
                marker=root, role=downstream_role, instance=instance
            )
            if pane is not None:
                await backend.send_new(pane)
        await backend.set_run_doc(
            marker=root, instance=instance, doc=doc_artifact, started_at=time.time()
        )
        return None

    logger.info(
        "New topic '%s' starts while run '%s' is still in flight - warning instead of clearing",
        doc_artifact,
        current_doc,
    )
    # Record the new doc as the workspace's run even though we're only
    # warning (not clearing) this time. The warning + no-auto-submit is a
    # one-shot: it exists to make a human look before displacing an
    # in-flight run, not to re-fire on every retry. Without this, a
    # retriggered handoff of the *same* doc keeps comparing against the
    # stale prior doc, re-warns, and re-suppresses the auto-submit - so the
    # user has to press Enter by hand on every retrigger. Recording it here
    # means the next fire of this same doc takes the ``current_doc ==
    # doc_artifact`` resume fast-path above (returns None, auto-submits).
    await backend.set_run_doc(
        marker=root, instance=instance, doc=doc_artifact, started_at=time.time()
    )
    return (
        f"NOTE: the previous run on {current_doc} was still in progress in "
        f"this pane. Continuing with {doc_artifact} now will discard that "
        "context. "
    )


async def _reveal_destination(
    backend: TerminalBackend, *, root: str, instance: str, role: str, destination_role: str
) -> Pane | None:
    """Split ``destination_role``'s pane directly off of ``role``'s own pane
    (the one handing off) and launch it - the counterpart to the panes a
    non-lazy workspace already launched upfront in ``build_workspace``.

    Two cases:

    - ``destination_role`` is one of this workspace's own template panes: the
      normal lazy-reveal case, gated on the workspace having been built with
      ``--lazy`` (a non-lazy/eager workspace already launched every one of
      its template's panes upfront, so if one of *those* is missing here,
      something else is wrong - not something to paper over by revealing).
    - ``destination_role`` isn't part of this workspace's template at all -
      most notably conductor, which the default ``native`` template leaves
      out (see reviewer.prompt.md's "Post-review follow-up" section). There
      is no other way that pane could already exist, so this is allowed
      regardless of ``--lazy``, as long as the role is one the pipeline
      itself knows how to spin up (``CANONICAL_PANES``) - an unrecognized
      custom role from a user template still can't be conjured from
      nothing.

    Returns ``None`` (handled the same as "pane truly missing" by the
    caller) if neither case applies, if the template name can't be
    recovered, or if ``role``'s own pane is somehow gone too (nothing to
    split off of) - all mean there's nowhere to reveal a pane, e.g. an
    unrelated Claude Code session's Stop hook firing.
    """
    template_name = await backend.get_template_name(marker=root, instance=instance)
    if template_name is None:
        logger.warning(
            "Cannot reveal '%s' for workspace '%s' (instance '%s'): no "
            "workspace state found, so the destination pane can't be launched "
            "- the handoff is being skipped. This usually means the backend's "
            "workspace state couldn't be located for this root.",
            destination_role,
            root,
            instance,
        )
        return None

    template = get_template(template_name)
    in_template = destination_role in {pane.role for pane in template.panes}

    if in_template:
        if not await backend.get_lazy(marker=root, instance=instance):
            return None
    elif destination_role not in CANONICAL_PANES:
        return None

    source = await backend.find_role_pane(marker=root, role=role, instance=instance)
    if source is None:
        return None

    return await backend.reveal_role(
        marker=root,
        instance=instance,
        root=root,
        template=template,
        role=destination_role,
        source=source,
    )


async def _send_handoff(
    backend: TerminalBackend, *, root: str, instance: str, role: str
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

    blocked_path = blocked_marker_path(root, role, instance)
    done_path = done_marker_path(root, role, instance)

    raw_blocked_content = stage.bounce_to and _read_fresh_marker(blocked_path)
    # A `.done` marker can route to `next_role` OR any `alt_next_roles`
    # target (via a `route:` directive - see parse_done_marker). Reviewer is
    # the case that makes the distinction matter: its `next_role` is None
    # (PASS is terminal in a single-feature run) but it can still route
    # forward to conductor under a conductor-driven run. Gating on
    # `next_role` alone silently dropped that handoff, since `None and ...`
    # short-circuits before the marker is ever read.
    has_forward = stage.next_role or stage.alt_next_roles
    raw_done_content = has_forward and _read_fresh_marker(done_path)

    new_topic_warning = None

    if raw_blocked_content:
        await _notify_terminal_state(
            backend, root=root, instance=instance, role=role,
            marker_path=blocked_path, kind="blocked",
        )
        destination_role, blocked_artifact = parse_blocked_marker(
            raw_blocked_content, stage=stage
        )
        if destination_role is None:
            logger.warning(
                "Role '%s' has multiple bounce targets %s but '%s' didn't "
                "say which via 'route: <role>' - skipping handoff",
                role,
                stage.bounce_to,
                blocked_path,
            )
            return False
        marker_path = blocked_path
        submit = await backend.get_auto_handoff(marker=root, instance=instance)
        # Omitted when that pane's persona is already baked into its
        # system prompt - see backends.common.role_prompt_prefix.
        prefix = role_prompt_prefix(destination_role)
        prompt_text = (
            f"{prefix}{role} sent this back - see "
            f"{blocked_artifact} "
        )
    elif raw_done_content:
        await _notify_terminal_state(
            backend, root=root, instance=instance, role=role,
            marker_path=done_path, kind="done",
        )
        destination_role, done_artifact = parse_done_marker(
            raw_done_content, stage=stage
        )
        marker_path = done_path
        submit = await backend.get_auto_handoff(marker=root, instance=instance)

        if role in ("researcher", "conductor"):
            # A fresh researcher.done from a human starting a new /researcher
            # request, and a fresh conductor.done dispatching the next
            # backlog item, are the same situation from downstream panes'
            # point of view: a new topic is about to occupy
            # planner/principal/implementer/reviewer, and their conversation
            # state from whatever came before needs clearing so it doesn't
            # bleed into the new one. Under a conductor-driven run the clear
            # is unconditional: the conductor owns the pipeline and
            # dispatching the next backlog item is its own decision, so it
            # never stops to ask a human whether it may overwrite the panes -
            # it clears them and moves on. Only a human-driven /researcher
            # run outside a conductor run keeps the in-flight check and its
            # "this will discard that context" warning.
            conductor_driven = role == "conductor" or os.path.isfile(
                conductor_run_marker_path(root, instance)
            )
            new_topic_warning = await _handle_new_topic(
                backend,
                root=root,
                instance=instance,
                doc_artifact=done_artifact,
                force=conductor_driven,
                clear_researcher=role == "conductor",
            )
            if new_topic_warning is not None:
                # Collapsed to one line for the same reason parse_done_marker
                # normalizes artifact paths (see pipeline.py's
                # _normalize_artifact): whatever ends up in prompt_text gets
                # typed verbatim into the destination pane, and a literal
                # newline there is what makes Claude Code's TUI treat the
                # injected text as a multi-line paste instead of a normal
                # command line - which the submit-retry logic below doesn't
                # recognize as "still unsubmitted." doc_artifact (a backlog
                # item description, in the conductor case) could in
                # principle contain one.
                new_topic_warning = " ".join(new_topic_warning.split())
                submit = False

        # The implementer -> reviewer handoff is the one place where the
        # artifact being handed off (the implementer's report) is *not* the
        # thing the destination role is supposed to treat as authoritative -
        # reviewer.prompt.md already tells reviewer to verify the diff
        # itself rather than trust the report, but the injected prompt text
        # is the very first thing reviewer reads, and "read {report} and
        # continue" on its own anchors attention on the report's own claims
        # (including its test summary) before that instruction is reached.
        # Spell out the diff-first framing here instead of relying on
        # reviewer to override the handoff's own framing on its own.
        # Same conditional prefix as the blocked-marker branch above.
        prefix = role_prompt_prefix(destination_role)
        if role == "implementer" and destination_role == "reviewer":
            prompt_text = (
                f"{new_topic_warning or ''}{prefix}"
                f"implementer finished - report at {done_artifact}. "
                "Verify the actual diff and repository state yourself before "
                "treating anything in that report (including its test "
                "results) as established; the report is a claim to check, "
                "not a summary to trust. "
            )
        else:
            prompt_text = (
                f"{new_topic_warning or ''}{prefix}"
                f"read {done_artifact} from {role} and continue "
            )
    else:
        return False

    destination = await backend.find_role_pane(
        marker=root, role=destination_role, instance=instance
    )
    if destination is None:
        destination = await _reveal_destination(
            backend, root=root, instance=instance, role=role, destination_role=destination_role
        )
    if destination is None:
        logger.warning(
            "No pane found for role '%s' in workspace '%s' (instance '%s') - "
            "skipping handoff",
            destination_role,
            root,
            instance,
        )
        return False

    await backend.send_role_prompt(
        destination_role, destination, text=prompt_text, submit=submit
    )
    await backend.activate_pane(destination)
    _mark_handed_off(marker_path)
    _clear_nag(done_path)
    logger.info(
        "Handed off %s -> %s (submit=%s)", role, destination_role, submit
    )
    return True


async def _maybe_nag_missing_marker(
    backend: TerminalBackend, *, root: str, instance: str, role: str
) -> bool:
    """If ``role`` has a forward stage but left no fresh marker at all, fire
    an attention notification once (D6/FR8, independent of auto-handoff) and,
    if auto-handoff is also on, print a Stop-blocking nag - returning
    ``True`` only for the latter (the caller cares whether the Stop was
    blocked, not whether a notification fired).

    A marker that's merely stale-but-already-handed-off (see
    ``_marker_present_and_handed``) is not "missing" and never nags - it is
    proof the handoff already went out, not a gap to fill.

    Only fires for roles that have somewhere to hand off to
    (``stage.next_role``) - reviewer's terminal PASS case is normally
    exempt, since it has no forward marker to forget. That exemption itself
    has an exception: under a conductor-driven run (``conductor-run``
    marker present), reviewer's PASS *does* have somewhere to go - back to
    conductor via ``route: conductor`` (see ``reviewer.prompt.md``'s "On
    PASS" section) - and forgetting that marker would otherwise silently
    stall the whole multi-feature run with no nag to catch it, since
    reviewer's Stop hook would just see "nothing to do" and exit quietly.
    So reviewer is nag-eligible whenever a conductor-run is active, even
    though its ``Stage.next_role`` is ``None``.

    Fires again on a later Stop only once the standing ``.nagged`` is stale;
    ``_send_handoff`` also clears the flag the moment a real marker shows up,
    so a role that's genuinely stuck (e.g. waiting on the user) isn't nagged
    on every idle turn. Two things make a ``.nagged`` stale:

    * ``run_started`` scoping (by mtime, not mere presence): consecutive
      backlog items in one conductor run share a single instance and
      therefore a single scoped marker dir, so a leftover ``.nagged`` from an
      earlier item is otherwise indistinguishable from a fresh one for the
      item now in flight - the original stale-nag bug. A ``.nagged`` older
      than ``run_started`` is a leftover, cleared here so the nag fires again
      for this item, mirroring ``_old_run_finished``'s own mtime comparison.
      ``run_started is None`` (no run doc recorded yet) is treated as "still
      valid" for this check - same as ``_old_run_finished`` - to avoid a
      spurious nag before a run is even recorded.
    * the ``NAG_COOLDOWN_SECONDS`` cooldown: a ``.nagged`` older than the
      cooldown is re-fired even *within* one run, so a role that ends an
      early turn with no marker, then works for many minutes and ends its
      real completion turn still with no marker, is nagged again at that
      terminal stop instead of silently stalling the pipeline - the
      long-turn silent-completion bug. The cooldown is long enough that the
      block->reprompt->immediate-stop loop (seconds) never re-nags.
    """
    stage = PIPELINE.get(role)
    if stage is None:
        return False

    conductor_driven_reviewer = role == "reviewer" and os.path.isfile(
        conductor_run_marker_path(root, instance)
    )
    if stage.next_role is None and not conductor_driven_reviewer:
        return False

    done_path = done_marker_path(root, role, instance)
    blocked_path = blocked_marker_path(root, role, instance)

    if _read_fresh_marker(done_path) or (
        stage.bounce_to and _read_fresh_marker(blocked_path)
    ):
        return False

    if _marker_present_and_handed(done_path) or (
        stage.bounce_to and _marker_present_and_handed(blocked_path)
    ):
        # Already handed off, just stale - not missing. Exit silently rather
        # than nag the role to re-litigate a handoff that already landed
        # (see _marker_present_and_handed).
        return False

    already_nagged = _already_nagged(done_path)
    if already_nagged:
        _, run_started = await backend.get_run_doc(marker=root, instance=instance)
        nag_mtime = os.path.getmtime(done_path + NAG_STATE_SUFFIX)
        # Two independent reasons a standing ``.nagged`` is stale and the nag
        # should fire again:
        #   * cross-item: it predates the current run (an earlier conductor
        #     backlog item left it - the original mtime-scoping bug), or
        #   * within-run: it's older than the cooldown, meaning the role has
        #     since done a long stretch of work and ended another turn still
        #     with no marker (the long-turn silent-completion bug). Each Stop
        #     is a real turn boundary; a terminal stop minutes after the last
        #     nag is exactly the one that must re-nag, while a stop seconds
        #     after it (the reprompt loop) stays inside the cooldown and does
        #     not.
        stale_for_run = run_started is not None and nag_mtime < run_started
        stale_by_cooldown = time.time() - nag_mtime >= NAG_COOLDOWN_SECONDS
        if not (stale_for_run or stale_by_cooldown):
            return False
        _clear_nag(done_path)
        already_nagged = False

    # FR8/AC7: the attention notification fires for any role with a forward
    # stage that stops with no fresh marker at all, independent of the
    # auto-handoff toggle - a supervised (non--think) run where a role stops
    # to ask the user still notifies, even in prefill-only mode (D6). It
    # shares the .nagged streak sentinel with the reminder block below for
    # dedup, but is not gated on auto-handoff the way the reminder
    # injection is - so the sentinel gets written here regardless, and the
    # auto-handoff check below only decides whether the reminder itself
    # (which blocks the Stop and re-prompts this same pane) also fires.
    if not already_nagged:
        _mark_nagged(done_path)
        await backend.notify(
            title=f"claudespace: {role} needs attention",
            message=f"'{role}' pane in {root} stopped with no completion marker.",
            marker=root,
            instance=instance,
        )

    if not await backend.get_auto_handoff(marker=root, instance=instance):
        return False

    _print_nag_block(role, done_path)
    return True


async def _run(backend: TerminalBackend, *, root: str, instance: str, role: str) -> None:
    handed_off = await _send_handoff(backend, root=root, instance=instance, role=role)
    if not handed_off:
        await _maybe_nag_missing_marker(backend, root=root, instance=instance, role=role)


def main() -> None:
    """Entrypoint installed as the ``claudespace-handoff`` console script.

    Silently exits (code 0) if this isn't running inside a claudespace pane
    or there's nothing fresh to hand off - see module docstring. If the
    role forgot its completion marker and auto-handoff is on, prints a
    Stop-blocking JSON reminder instead (see ``_maybe_nag_missing_marker``).

    ``CLAUDESPACE_INSTANCE`` (the per-window UUID stamped by
    ``build_workspace``/``reveal_role``) restricts every backend lookup this
    run performs to panes in the *same physical window* - see
    ``backends/iterm.py``'s ``_matches_workspace``. Older panes launched
    before this variable existed won't have it exported; falling back to
    ``None`` degrades to the old root-only matching for them rather than
    refusing to hand off at all.

    ``CLAUDESPACE_ORIGIN_ROOT`` (the original, unresolved project root) is
    used for all marker-path lookups and backend pane matching, so markers
    written before a worktree existed (e.g. ``conductor-run``) remain
    visible even after a role re-exports ``CLAUDESPACE_ROOT`` to point at
    the worktree. Falls back to ``CLAUDESPACE_ROOT`` for panes launched
    before ``CLAUDESPACE_ORIGIN_ROOT`` was introduced.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    role = os.environ.get("CLAUDESPACE_ROLE")
    root = os.environ.get("CLAUDESPACE_ORIGIN_ROOT") or os.environ.get("CLAUDESPACE_ROOT")
    instance = os.environ.get("CLAUDESPACE_INSTANCE")
    if not role or not root:
        return

    try:
        backend = get_backend()
        backend.run(
            lambda backend: _run(backend, root=root, instance=instance, role=role)
        )
    except Exception as exc:
        logger.exception("Handoff failed for role '%s' in '%s'", role, root)
        # Don't let a broken handoff mechanism read as "nothing to hand
        # off" - that's indistinguishable from success to both the model
        # and the user, since this stderr log isn't surfaced anywhere. See
        # _maybe_nag_after_handoff_error and _print_handoff_error_block.
        if _maybe_nag_after_handoff_error(root, role, instance):
            _print_handoff_error_block(role, exc)
        sys.exit(0)


if __name__ == "__main__":
    main()
