"""Backend-independent helpers shared by every ``TerminalBackend``.

Pure functions of the role/config that know nothing about a specific
terminal's scripting API - moved out of the old ``iterm.py`` so persona
baking, prompt-prefix semantics, and timing constants can't drift between
the iTerm2 and Ghostty implementations.
"""

from __future__ import annotations

import os
import shlex

from claudespace.assets_sync import PROMPTS_DEST
from claudespace.pipeline import resolve_root

# Printed by claude's input box once its TUI accepts text on iTerm2, where
# the screen can actually be read. Ghostty has no screen-read equivalent
# (see backends/ghostty.py's readiness poll, which uses the pane title
# instead) - this constant stays here because it's still iTerm2's own ready
# signal, just relocated alongside the timing constants below it.
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
# (iTerm2) or the single resend (Ghostty) backstops whatever this doesn't
# cover.
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
    stays visible on iTerm2; Ghostty relies on the title alone (see
    backends/ghostty.py).

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
    banner: str = "",
) -> str:
    """The full shell command a pane runs at launch: cd, env exports, an
    optional theme banner, then ``command``.

    Shared verbatim by both backends - iTerm2 sends it via
    ``async_send_text``, Ghostty via ``input text`` - so the pane's launch
    environment can't drift between them. ``root`` is the workspace's
    original launch root; ``resolve_root`` follows a run-scoped worktree
    marker if one has been created since, same as every other marker-path
    lookup in the codebase.
    """
    effective_root = resolve_root(root)
    return (
        f"cd {effective_root} && export CLAUDESPACE_ROOT={effective_root} && "
        f"export CLAUDESPACE_ROLE={role} && "
        f"export CLAUDESPACE_INSTANCE={instance} && "
        f"export CLAUDESPACE_MAX_ITEMS={max_items} && "
        f"export CLAUDESPACE_THINK={int(think)} && {banner}{command}\n"
    )
