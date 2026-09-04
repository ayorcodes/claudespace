"""Backend-independent helpers shared by every ``TerminalBackend``.

Pure functions of the role/config that know nothing about a specific
terminal's scripting API - moved out of the old ``iterm.py`` so persona
baking, prompt-prefix semantics, and timing constants can't drift between
the iTerm2 and tmux implementations.
"""

from __future__ import annotations

import os
import shlex
from typing import Any

from claudespace.assets_sync import PROMPTS_DEST
from claudespace.pipeline import resolve_root, session_marker_dir

# Printed by claude's input box once its TUI accepts text. Both backends can
# read this: iTerm2 via its screen-content API, tmux via `capture-pane`.
CLAUDE_PROMPT_MARKER = "❯"

# Fallback for CLAUDESPACE_MAX_ITEMS (see --max-items) when a caller has
# none - notably reveal_role. Unlike the vars above, only the conductor pane
# reads it, so there is no cross-pane lookup to support and it isn't tracked
# as persisted state; a --lazy workspace whose conductor pane is revealed
# late falls back to this rather than the flag it was built with.
DEFAULT_MAX_ITEMS = 5

# Give up prefilling a pane after this long - claude may be stuck behind a
# dialog. The user still gets a normal session once they clear it.
CLAUDE_READY_TIMEOUT_SECONDS = 15
CLAUDE_READY_POLL_INTERVAL_SECONDS = 0.25

# After sending Enter, how long to poll for the input box actually clearing,
# and how many times to resend. Closes the race where a handoff's prompt is
# typed but never submitted, stalling the pipeline until someone notices.
SUBMIT_CONFIRM_TIMEOUT_SECONDS = 3
SUBMIT_CONFIRM_POLL_INTERVAL_SECONDS = 0.2
SUBMIT_MAX_ATTEMPTS = 3

# Pause between typing text and sending "\r". Claude Code's TUI reads a fast
# keystroke burst as an in-progress paste, and during that window "\r" is
# inserted as a literal newline instead of submitting - the "Enter just adds
# a new line" failure. It clears within one repaint; the confirm/retry loop
# backstops whatever this doesn't cover.
SUBMIT_KEYSTROKE_SETTLE_SECONDS = 0.3


def role_prompt_file(role: str) -> str:
    """Path a role's bundled prompt would be synced to by ``assets_sync``,
    regardless of whether it's actually present."""
    return str(PROMPTS_DEST / f"{role}.prompt.md")


def role_prompt_prefix(role: str) -> str:
    """``""`` if ``role``'s pane has its persona baked into its system prompt
    at launch, else ``"/{role} "`` to fall back to the slash command's own
    "read the prompt file" step.

    Baking is unconditional whenever a prompt file exists (see
    ``command_with_baked_persona``), so this reduces to a file check - no
    need to inspect the workspace's template or launch command. A role with
    no prompt file (an unrecognized name from a user's own template) still
    needs the slash command.

    Used both for the launch-time prefill and for every pipeline handoff
    (``handoff.py``), so the two can't drift - and identically by both
    backends, so a role's prompt semantics don't drift between them either.
    """
    if os.path.isfile(role_prompt_file(role)):
        return ""
    return f"/{role} "


def command_with_baked_persona(role: str, command: str) -> str:
    """Append ``--append-system-prompt-file`` and ``--name`` for ``role`` onto
    ``command``, unless no prompt file exists for ``role`` (an unrecognized
    role name from a user's own custom template).

    ``--name`` labels the session in Claude Code's own prompt box and the
    terminal title. It matters more than it looks: panes are no longer
    prefilled with ``/<role>``, so without it a pane running the TUI has no
    in-band indication of which role it is (the launch banner scrolls out of
    the alt-screen immediately, and the theme background is painted over).
    See ``themes.build_role_profile`` for the badge, which is the label that
    stays visible on iTerm2; the tmux backend conveys it via pane-border
    title/style instead (see ``backends/tmux.py``).

    This applies to a custom command from a user's own template
    (``~/.config/claudespace/templates.toml`` pointing a pane at a wrapper
    around a different model/CLI) as much as to the built-in ones. That
    wrapper isn't guaranteed to forward an unrecognized flag through to a
    real Claude Code process - but the readiness checks in both backends
    already tolerate a pane that never reaches claude's prompt (it just
    skips the prefill and logs a warning), so the failure mode for a
    genuinely incompatible wrapper is a visible, debuggable startup error in
    that one pane - not silent breakage.
    """
    prompt_file = role_prompt_file(role)
    if not os.path.isfile(prompt_file):
        return command
    return (
        f"{command} --append-system-prompt-file {shlex.quote(prompt_file)} "
        f"--name {shlex.quote(role)}"
    )


def launch_command_text(
    *,
    root: str,
    role: str,
    instance: str,
    think: bool,
    max_items: int,
    command: str,
    backend_name: str,
    banner: str = "",
) -> str:
    """The full shell command a pane runs at launch: cd, env exports, an
    optional theme banner, then ``command``.

    Shared verbatim by both backends - iTerm2 sends it via
    ``async_send_text``, tmux via ``send-keys`` - so the pane's launch
    environment can't drift between them. ``root`` is the workspace's
    original launch root; ``resolve_root`` follows a run-scoped worktree
    marker if one has been created since, so the pane's ``cd`` and
    ``CLAUDESPACE_ROOT`` land in the worktree for code work.

    ``CLAUDESPACE_MARKER_DIR`` is always anchored at the **original**
    (unresolved) ``root`` so pipeline markers (``.done``, ``.blocked``,
    ``conductor-run``, ``think``) stay in one place regardless of whether a
    worktree was created mid-run. ``CLAUDESPACE_ORIGIN_ROOT`` records the
    original root explicitly so that ``claudespace-handoff`` /
    ``claudespace-msg`` can always find markers even when a role re-exports
    ``CLAUDESPACE_ROOT`` to point at a worktree.

    ``CLAUDESPACE_TERMINAL=<backend_name>`` matters more than it looks:
    ``claudespace-handoff``/``claudespace-msg`` run as their *own* process
    later, from inside this pane, and resolve their own backend from
    scratch via ``config.load_terminal_backend`` (env var, then
    ``config.toml``, else iTerm2) - they have no way to see how the CLI
    that built this workspace chose its backend (``--tmux``, in
    particular, is a flag local to that one invocation and never reaches
    here otherwise). Without this, a workspace built via ``--tmux`` alone
    (no ``config.toml``) has every handoff silently resolve back to
    iTerm2, fail to find iTerm2 even running, and exit before printing
    anything a Stop hook's caller would see - a handoff that looks like it
    just does nothing.
    """
    effective_root = resolve_root(root, instance)
    marker_dir = session_marker_dir(root, instance)
    return (
        f"cd {effective_root} && export CLAUDESPACE_ROOT={effective_root} && "
        f"export CLAUDESPACE_ORIGIN_ROOT={root} && "
        f"export CLAUDESPACE_ROLE={role} && "
        f"export CLAUDESPACE_INSTANCE={instance} && "
        f"export CLAUDESPACE_MARKER_DIR={marker_dir} && "
        f"export CLAUDESPACE_MAX_ITEMS={max_items} && "
        f"export CLAUDESPACE_THINK={int(think)} && "
        f"export CLAUDESPACE_TERMINAL={backend_name} && {banner}{command}\n"
    )


def screen_signature(text: str) -> tuple[str, bool]:
    """Return ``(full-screen text, ends-at-ready-prompt)`` for one poll of
    a pane's visible content.

    ``ends-at-ready-prompt`` mirrors the ready-prompt detection used while
    waiting for claude to come up (``CLAUDE_PROMPT_MARKER``), so "idle at
    prompt" is recognized the same way everywhere. Shared by both backends'
    watchdog content-diffing (AD6): iTerm2 gets ``text`` from
    ``async_get_screen_contents``, tmux from ``capture-pane -p``.
    """
    lines = text.split("\n")
    ready = any(
        line.strip().startswith(CLAUDE_PROMPT_MARKER) for line in lines if line.strip()
    )
    return text, ready


def stall_decision(
    previous: dict[str, Any] | None,
    *,
    text: str,
    ready: bool,
    now: float,
    stall_after_seconds: float,
) -> tuple[dict[str, Any], bool]:
    """Pure watchdog stall decision shared by both backends (AD6: both get
    full-fidelity content-diff, unlike the superseded native-Ghostty draft's
    crash-detection-only descope).

    Mirrors the original ``watchdog._check_once`` state machine exactly
    (move-only): every poll's snapshot is recorded as the new state
    regardless of outcome, and a stall is flagged (with the clock reset)
    only once the recorded state has stopped changing across two
    consecutive polls, isn't idle-at-prompt, and enough time has elapsed
    since the state was first that value.
    """
    current = {"text": text, "ready": ready, "seen_at": now}
    if previous is None:
        return current, False
    if text != previous["text"]:
        return current, False
    if ready:
        return current, False
    if now - previous["seen_at"] < stall_after_seconds:
        return current, False
    return current, True


def idle_completion_decision(
    previous: dict[str, Any] | None,
    *,
    text: str,
    ready: bool,
    now: float,
    idle_after_seconds: float,
) -> tuple[dict[str, Any], bool]:
    """Pure watchdog decision for a *silent completion*: a pane that has sat
    idle at claude's ready prompt, its screen unchanged, for
    ``idle_after_seconds``.

    This is the complement of ``stall_decision`` - which deliberately treats
    an idle-at-prompt pane as healthy (a human supervising a run wants no
    stall alert while a role sits waiting) - and is what lets the watchdog
    catch the failure ``stall_decision`` can't see: a role that *finished* a
    turn, produced no handoff marker, and is now parked at the prompt with
    the pipeline silently stalled behind it. Whether that idle pane is
    actually a problem (forward pipeline stage exists, no marker handed off)
    is a filesystem/pipeline question the watchdog answers separately; this
    function only reports the raw "idle and unchanged long enough" signal.

    Unlike ``stall_decision``'s state (which restamps ``seen_at`` every poll
    and so measures only the gap between the last two polls), this carries
    the timestamp at which the current idle screen was *first* observed
    (``since``) forward across polls, so "idle for ``idle_after_seconds``"
    measures from first sighting regardless of the poll interval. A screen
    that changes, or a pane that isn't at the ready prompt, resets the clock.
    """
    if not ready:
        return {"text": text, "ready": ready, "since": now}, False
    unchanged = (
        previous is not None
        and previous.get("ready")
        and previous.get("text") == text
    )
    since = previous["since"] if unchanged else now
    current = {"text": text, "ready": ready, "since": since}
    return current, (now - since >= idle_after_seconds)
