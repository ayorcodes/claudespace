"""Command-line interface for the workspace launcher."""

from __future__ import annotations

import argparse
import functools
import logging
import os
import sys
from typing import TYPE_CHECKING

from claudespace import (
    assets_sync,
    connect,
    environment,
    update,
    utils,
    watchdog,
    workspace,
)
from claudespace.config import DEFAULT_TEMPLATE, get_template, list_templates
from claudespace.iterm import DEFAULT_MAX_ITEMS
from claudespace.watchdog import DEFAULT_INTERVAL_SECONDS, DEFAULT_STALL_AFTER_SECONDS

if TYPE_CHECKING:
    import iterm2

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claudespace",
        description="Build or attach to an iTerm2 development workspace for a folder.",
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
        "through your first real run.",
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
        f"flagged as possibly stalled (default: {DEFAULT_STALL_AFTER_SECONDS}).",
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
        help="Disable auto-handoff: pipeline handoffs between panes "
        "(researcher->planner->principal->implementer->reviewer), including "
        "rejected/blocked bounces, only prefill the next pane's input - you "
        "press enter to advance. By default, all handoffs auto-submit.",
    )
    parser.add_argument(
        "--think",
        action="store_true",
        help="Autonomous mode: roles never stop to ask you clarifying "
        "questions. The planner, instead of pausing on an open product "
        "question, answers it the way a 30-year staff engineer at a "
        "top-tier shop would and records the answer as an explicit "
        "decision in the Planning Brief. Use when you're away from the "
        "machine and don't want the pipeline stalling on a prompt. A run "
        "without this flag turns the mode back off for that workspace.",
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
        "--list-templates",
        action="store_true",
        help="List available template names and exit.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging."
    )
    return parser


async def _run(
    connection: "iterm2.Connection",
    *,
    root: str,
    template: str,
    force_new: bool,
    auto_handoff: bool,
    lazy: bool,
    think: bool,
    max_items: int,
    just_launched_iterm: bool,
) -> None:
    await workspace.open_workspace(
        connection,
        root,
        template,
        force_new,
        auto_handoff=auto_handoff,
        lazy=lazy,
        think=think,
        max_items=max_items,
        just_launched_iterm=just_launched_iterm,
    )


async def _run_watchdog(
    connection: "iterm2.Connection", *, root: str, interval: float, stall_after: float
) -> None:
    # No per-window instance UUID is available from a bare `claudespace
    # watchdog` invocation (that's only ever minted at `build_workspace`
    # time) - matches by root marker alone, same fallback build_workspace's
    # older-pane handling already relies on. Ambiguous only when two
    # windows are open against the same resolved root simultaneously.
    await watchdog.run_watchdog(
        connection,
        root=os.path.abspath(os.path.expanduser(root)),
        instance=None,
        interval_seconds=interval,
        stall_after_seconds=stall_after,
    )


def main() -> None:
    """Entrypoint installed as the ``claudespace`` console script."""
    parser = _build_parser()
    args = parser.parse_args()
    utils.setup_logging(args.verbose)

    environment.require_macos()

    if args.command == "update":
        update.run_update()
        return

    if args.command == "doctor":
        ok = environment.check_environment(
            iterm_was_running=utils.is_iterm_running(),
            assume_yes=args.yes,
            launch=args.launch,
        )
        if ok:
            logger.info("claudespace is ready. Run 'claudespace' in any project folder.")
        sys.exit(0 if ok else 1)

    if args.command == "uninstall":
        assets_sync.uninstall()
        return

    if args.command == "watchdog":
        environment.ensure_environment(iterm_was_running=utils.is_iterm_running())
        runner = functools.partial(
            _run_watchdog, root=args.root, interval=args.interval, stall_after=args.stall_after
        )
        try:
            connect.run(runner)
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

    try:
        get_template(args.template)
    except KeyError as exc:
        logger.error(exc)
        sys.exit(1)

    iterm_was_running = utils.is_iterm_running()
    environment.ensure_environment(iterm_was_running=iterm_was_running)

    if not iterm_was_running:
        logger.info("iTerm2 is not running - launching it")
        utils.launch_iterm()

    runner = functools.partial(
        _run,
        root=args.root,
        template=args.template,
        force_new=args.new,
        auto_handoff=args.auto_handoff,
        lazy=args.lazy,
        think=args.think,
        max_items=args.max_items,
        just_launched_iterm=not iterm_was_running,
    )
    try:
        connect.run(runner)
    except Exception:
        logger.exception("Failed to build workspace for '%s'", args.root)
        sys.exit(1)


if __name__ == "__main__":
    main()
