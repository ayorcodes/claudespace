"""Command-line interface for the workspace launcher."""

from __future__ import annotations

import argparse
import functools
import logging
import os
import sys

from claudespace import (
    assets_sync,
    environment,
    update,
    utils,
    watchdog,
    workspace,
)
from claudespace.backends import get_backend
from claudespace.backends.base import TerminalBackend
from claudespace.backends.common import DEFAULT_MAX_ITEMS
from claudespace.config import (
    DEFAULT_TEMPLATE,
    get_template,
    list_templates,
    load_tmux_persistence,
    load_tmux_viewer,
)
from claudespace.watchdog import DEFAULT_INTERVAL_SECONDS, DEFAULT_STALL_AFTER_SECONDS

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claudespace",
        description="Build or attach to a terminal development workspace for a "
        "folder (iTerm2 by default; pass --tmux or --cmux, or set "
        "'terminal.backend' in ~/.config/claudespace/config.toml, for the "
        "tmux or cmux backend).",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "update",
        help="Pull the latest claudespace from git, reinstall via pipx, "
        "and resync bundled commands/prompts.",
    )
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check and repair everything claudespace needs: the claude CLI, "
        "iTerm2, and iTerm2's Python API. Run automatically by install.sh so "
        "the one-time setup happens at install time rather than partway "
        "through your first real run. Only checks the iTerm2 backend - the "
        "tmux backend's own preflight (tmux installed/new enough) runs at "
        "build time instead (see 'terminal.backend').",
    )
    doctor_parser.add_argument(
        "--yes",
        action="store_true",
        help="Don't prompt before installing iTerm2 via Homebrew. Required "
        "when running non-interactively.",
    )
    doctor_parser.add_argument(
        "--no-launch",
        dest="launch",
        action="store_false",
        help="Don't start iTerm2 to verify its Python API came up. Used at "
        "install time, where launching the app is unwanted.",
    )
    subparsers.add_parser(
        "uninstall",
        help="Remove claudespace's global Stop hook and its bundled "
        "commands/prompts. Leaves your templates.toml alone. Run this before "
        "'pipx uninstall claudespace', otherwise the Stop hook is left "
        "pointing at a command that no longer exists and fails on every turn "
        "of every Claude Code session on this machine.",
    )
    watchdog_parser = subparsers.add_parser(
        "watchdog",
        help="Watch an open workspace's panes for stalls (stuck dialog, "
        "runaway tool loop, crashed process) and notify when one is found. "
        "Runs until interrupted - meant to be left running in its own "
        "terminal or backgrounded alongside an unattended --think/conductor "
        "run.",
    )
    watchdog_parser.add_argument(
        "--root",
        default=os.getcwd(),
        help="Workspace folder to watch (default: current directory).",
    )
    watchdog_parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"Seconds between polls (default: {DEFAULT_INTERVAL_SECONDS}).",
    )
    watchdog_parser.add_argument(
        "--stall-after",
        type=float,
        default=DEFAULT_STALL_AFTER_SECONDS,
        help="Seconds of unchanged, non-idle screen output before a pane is "
        f"flagged as possibly stalled (default: {DEFAULT_STALL_AFTER_SECONDS}). "
        "Applies identically on the iTerm2 and tmux backends (AD6).",
    )
    watchdog_parser.add_argument(
        "--tmux",
        action="store_true",
        help="Watch a tmux-backed workspace instead of the default iTerm2 "
        "one. Same effect as [terminal] backend = \"tmux\" in "
        "~/.config/claudespace/config.toml, for this invocation only.",
    )
    watchdog_parser.add_argument(
        "--cmux",
        action="store_true",
        help="Watch a cmux-backed workspace instead of the default iTerm2 "
        "one. Same effect as [terminal] backend = \"cmux\" in "
        "~/.config/claudespace/config.toml, for this invocation only. "
        "Mutually exclusive with --tmux.",
    )
    parser.add_argument(
        "--root",
        default=os.getcwd(),
        help="Folder to build the workspace in (default: current directory). "
        "The resolved absolute path is used to detect an already-open "
        "workspace on re-run.",
    )
    parser.add_argument(
        "--template",
        default=DEFAULT_TEMPLATE,
        help=f"Template to use (default: {DEFAULT_TEMPLATE}; see --list-templates).",
    )
    parser.add_argument(
        "--new",
        action="store_true",
        help="Always build a new workspace window, even if one already exists.",
    )
    parser.add_argument(
        "--manual",
        dest="auto_handoff",
        action="store_false",
        help="Fully supervised mode: disables both auto-handoff (pipeline "
        "handoffs between panes - researcher->planner->principal->"
        "implementer->reviewer, including rejected/blocked bounces - only "
        "prefill the next pane's input, you press enter to advance) and "
        "autonomous (--think) mode. This is the opposite of the default: "
        "by default all handoffs auto-submit and roles decide autonomously "
        "instead of stopping to ask you. Use --manual when you're at the "
        "keyboard and want to stay in the loop on everything.",
    )
    parser.add_argument(
        "--think",
        action="store_true",
        default=True,
        help="Autonomous mode (on by default): roles never stop to ask you "
        "clarifying questions. The planner, instead of pausing on an open "
        "product question, answers it the way a 30-year staff engineer at "
        "a top-tier shop would and records the answer as an explicit "
        "decision in the Planning Brief. This flag is redundant now that "
        "it's the default - kept for explicitness in scripts. Use "
        "--manual to turn it back off.",
    )
    parser.add_argument(
        "--lazy",
        action="store_true",
        help="Start with only the template's entry-role pane visible. Other "
        "panes stay hidden (the window starts as a single unsplit pane) "
        "until a pipeline handoff first reveals them, splitting them into "
        "existence in their template-defined position.",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=DEFAULT_MAX_ITEMS,
        help="Circuit breaker for a conductor-driven multi-feature run "
        f"(default: {DEFAULT_MAX_ITEMS}): the conductor pane stops and "
        "reports after auto-advancing through this many backlog items, "
        "regardless of backlog state. Ignored by templates without a "
        "conductor pane (e.g. 'native').",
    )
    parser.add_argument(
        "--tmux",
        action="store_true",
        help="Use the tmux backend for this run instead of the default "
        "iTerm2 (the supported way to run claudespace in Ghostty). Same "
        "effect as [terminal] backend = \"tmux\" in "
        "~/.config/claudespace/config.toml, for this invocation only - "
        "iTerm2 stays the default when this is omitted.",
    )
    parser.add_argument(
        "--cmux",
        action="store_true",
        help="Use the cmux backend for this run instead of the default "
        "iTerm2. Same effect as [terminal] backend = \"cmux\" in "
        "~/.config/claudespace/config.toml, for this invocation only - "
        "iTerm2 stays the default when this is omitted. Mutually "
        "exclusive with --tmux.",
    )
    parser.add_argument(
        "--list-templates",
        action="store_true",
        help="List available template names and exit.",
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help="tmux backend only: list every restorable/running tmux-backed "
        "workspace (session, root folder, roles present) - waits briefly "
        "for an in-flight autorestore first, same as a normal attach - "
        "then prompts which one to attach to. Non-interactive (piped/no "
        "tty) just lists them and exits without prompting.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging."
    )
    return parser


async def _run(
    backend: TerminalBackend,
    *,
    root: str,
    template: str,
    force_new: bool,
    auto_handoff: bool,
    lazy: bool,
    think: bool,
    max_items: int,
    just_launched_terminal: bool,
) -> None:
    await workspace.open_workspace(
        backend,
        root,
        template,
        force_new,
        auto_handoff=auto_handoff,
        lazy=lazy,
        think=think,
        max_items=max_items,
        just_launched_terminal=just_launched_terminal,
    )


async def _fetch_restorable(backend: TerminalBackend, out: list) -> None:
    from claudespace.backends.tmux import TmuxBackend

    assert isinstance(backend, TmuxBackend)  # guarded by the caller
    out.extend(await backend.list_all_workspaces())


def _print_restorable(entries: list[dict]) -> None:
    for i, entry in enumerate(entries, start=1):
        roles = ", ".join(entry["roles"]) or "(none tagged)"
        print(f"[{i}] {entry['session']}  root={entry['workspace']}  roles={roles}")


def _read_line(prompt: str) -> str | None:
    """``input(prompt)``, or ``None`` on Ctrl-C/Ctrl-D - the one place that
    reads a line from the user in ``--restore``'s picker, so every prompt
    it shows shares the same cancellation handling instead of each call
    site having to remember its own try/except (a single-entry prompt
    missing this exact guard, while a multi-entry one right next to it had
    it, is what this factoring exists to make impossible again).
    """
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _prompt_selection(entries: list[dict]) -> dict | None:
    """Ask which entry to attach to. Returns ``None`` if the user declined
    (blank input, Ctrl-C/Ctrl-D) or input isn't interactive - callers treat
    that as "just list them, attach nothing" rather than guessing.
    """
    if not sys.stdin.isatty():
        return None
    if len(entries) == 1:
        reply = _read_line(f"Attach to {entries[0]['session']}? [Y/n] ")
        if reply is None:
            return None
        return None if reply.strip().lower() in ("n", "no") else entries[0]
    reply = _read_line(f"Attach to which? [1-{len(entries)}, blank to skip] ")
    if reply is None:
        return None
    reply = reply.strip()
    if not reply:
        return None
    try:
        index = int(reply)
    except ValueError:
        print(f"Not a number: {reply!r}")
        return None
    if not (1 <= index <= len(entries)):
        print(f"Out of range: {index}")
        return None
    return entries[index - 1]


def _run_restore_listing() -> None:
    """``claudespace --restore``: list every tmux-backed session found
    (waiting briefly for an in-flight autorestore first, same as a normal
    attach), then - interactively - attach to whichever one is picked.
    """
    from claudespace.backends.tmux import TmuxBackend

    persist, persist_interval_minutes = load_tmux_persistence()
    viewer = load_tmux_viewer()
    backend = TmuxBackend(
        viewer=viewer, persist=persist, persist_interval_minutes=persist_interval_minutes
    )
    entries: list[dict] = []
    try:
        backend.run(functools.partial(_fetch_restorable, out=entries))
    except Exception:
        logger.exception("Failed to list restorable tmux sessions")
        sys.exit(1)

    if not entries:
        print("No claudespace tmux sessions found.")
        return

    _print_restorable(entries)
    chosen = _prompt_selection(entries)
    if chosen is None:
        print()
        print("Attach with: claudespace --tmux --root <root>")
        print("         or: tmux -L claudespace attach -t <session>")
        return

    logger.info("Attaching to %s...", chosen["session"])
    utils.launch_viewer(chosen["session"], viewer=viewer)


async def _run_watchdog(
    backend: TerminalBackend, *, root: str, interval: float, stall_after: float
) -> None:
    # No per-window instance UUID is available from a bare `claudespace
    # watchdog` invocation (that's only ever minted at `build_workspace`
    # time) - matches by root marker alone, same fallback build_workspace's
    # older-pane handling already relies on. Ambiguous only when two
    # windows are open against the same resolved root simultaneously.
    await watchdog.run_watchdog(
        backend,
        root=os.path.abspath(os.path.expanduser(root)),
        instance=None,
        interval_seconds=interval,
        stall_after_seconds=stall_after,
    )


def _resolve_backend(*, force_tmux: bool = False, force_cmux: bool = False) -> TerminalBackend:
    """Resolve the configured terminal backend once, at CLI entry (AD5) -
    everything downstream (workspace build, watchdog) is threaded this same
    instance rather than re-resolving config independently.

    ``--tmux``/``--cmux`` (``force_tmux``/``force_cmux``) override config/env
    for this invocation only, the same way ``CLAUDESPACE_TERMINAL`` does -
    iTerm2 remains the default and the config-file selection is still what a
    plain ``claudespace`` (no flag) uses. The two are mutually exclusive -
    passing both is a usage error, not a silent pick.
    """
    if force_tmux and force_cmux:
        logger.error("--tmux and --cmux are mutually exclusive.")
        sys.exit(1)
    name = "tmux" if force_tmux else "cmux" if force_cmux else None
    try:
        return get_backend(name)
    except ValueError as exc:
        logger.error(exc)
        sys.exit(1)


def _check_tmux_persistence() -> None:
    """Informational-only doctor check for the tmux backend's vendored
    resurrect/continuum plugins (Increment 2, Implementation Order step
    11). Never affects doctor's overall exit code - tmux is an entirely
    optional backend, unlike the iTerm2 checks above it.
    """
    from claudespace.backends import tmux_cli, tmux_persist

    if not tmux_cli.is_tmux_available():
        return
    if tmux_persist.plugins_present():
        logger.info(
            "tmux backend: vendored resurrect/continuum plugins found at %s",
            tmux_persist.PLUGINS_DIR,
        )
    else:
        logger.warning(
            "tmux backend: vendored resurrect/continuum plugins not found at "
            "%s - run 'claudespace-sync-assets' to install them (session "
            "persistence across a reboot won't work until then; the tmux "
            "backend itself is otherwise unaffected).",
            tmux_persist.PLUGINS_DIR,
        )


def _ensure_terminal_launched(backend: TerminalBackend) -> bool:
    """Cold-launch iTerm2 if it isn't already running, and run claudespace's
    own preflight checks against it (Python API enablement etc, see
    ``environment.py``).

    Returns whether we just launched it (``just_launched_terminal``, threaded
    through to ``workspace.open_workspace`` so it knows whether the default
    empty window it finds afterwards is stray chrome to clean up).

    Only meaningful for the iTerm2 path: the tmux backend builds entirely
    headlessly against a detached tmux server (AD3) and only spawns a
    viewer terminal afterwards, in ``TmuxBackend.activate_window`` - there is
    nothing to cold-launch before that, so this is a no-op for it. The cmux
    backend has its own reachability preflight in ``CmuxBackend.run`` (D4) -
    cmux must already be running for that to pass, so there is likewise
    nothing for this function to cold-launch.
    """
    from claudespace.backends.cmux import CmuxBackend
    from claudespace.backends.tmux import TmuxBackend

    if isinstance(backend, (TmuxBackend, CmuxBackend)):
        return False

    was_running = utils.is_iterm_running()
    environment.ensure_environment(iterm_was_running=was_running)
    if not was_running:
        logger.info("iTerm2 is not running - launching it")
        utils.launch_iterm()
    return not was_running


def _apply_manual_override(args: argparse.Namespace) -> None:
    """``--manual`` is the single "fully supervised" toggle: disables both
    auto-handoff (already its own dest, set ``False`` by the flag itself)
    and autonomous (``--think``) mode, which now defaults on. ``--manual``
    wins over an explicit ``--think``, since it's the more conservative
    choice - mutates ``args`` in place.
    """
    if not args.auto_handoff:
        args.think = False


def main() -> None:
    """Entrypoint installed as the ``claudespace`` console script."""
    parser = _build_parser()
    args = parser.parse_args()
    utils.setup_logging(args.verbose)
    _apply_manual_override(args)

    environment.require_macos()

    if args.command == "update":
        update.run_update()
        return

    if args.command == "doctor":
        ok = environment.run_doctor_checks(
            iterm_was_running=utils.is_iterm_running(),
            assume_yes=args.yes,
            launch=args.launch,
        )
        _check_tmux_persistence()
        if ok:
            logger.info("claudespace is ready. Run 'claudespace' in any project folder.")
        sys.exit(0 if ok else 1)

    if args.command == "uninstall":
        assets_sync.uninstall()
        return

    if args.command == "watchdog":
        backend = _resolve_backend(force_tmux=args.tmux, force_cmux=args.cmux)
        from claudespace.backends.iterm import ItermBackend

        if isinstance(backend, ItermBackend):
            environment.ensure_environment(iterm_was_running=utils.is_iterm_running())
        runner = functools.partial(
            _run_watchdog, root=args.root, interval=args.interval, stall_after=args.stall_after
        )
        try:
            backend.run(runner)
        except KeyboardInterrupt:
            return
        except Exception:
            logger.exception("Watchdog failed for '%s'", args.root)
            sys.exit(1)
        return

    if args.list_templates:
        for template_name in list_templates():
            print(template_name)
        return

    if args.restore:
        _run_restore_listing()
        return

    try:
        get_template(args.template)
    except KeyError as exc:
        logger.error(exc)
        sys.exit(1)

    backend = _resolve_backend(force_tmux=args.tmux, force_cmux=args.cmux)
    just_launched_terminal = _ensure_terminal_launched(backend)

    runner = functools.partial(
        _run,
        root=args.root,
        template=args.template,
        force_new=args.new,
        auto_handoff=args.auto_handoff,
        lazy=args.lazy,
        think=args.think,
        max_items=args.max_items,
        just_launched_terminal=just_launched_terminal,
    )
    try:
        backend.run(runner)
    except Exception:
        logger.exception("Failed to build workspace for '%s'", args.root)
        sys.exit(1)


if __name__ == "__main__":
    main()
