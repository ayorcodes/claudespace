"""Command-line interface for the workspace launcher."""

from __future__ import annotations

import argparse
import functools
import logging
import os
import sys

import iterm2

from claudespace import environment, update, utils, workspace
from claudespace.config import DEFAULT_TEMPLATE, get_template, list_templates
from claudespace.iterm import DEFAULT_MAX_ITEMS

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
        "(researcher->planner->principal->implementer->reviewer) only "
        "prefill the next pane's input - you press enter to advance. "
        "By default, successful handoffs auto-submit. Rejected/blocked "
        "handoffs always prefill-only, regardless of this setting.",
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
    connection: iterm2.Connection,
    *,
    root: str,
    template: str,
    force_new: bool,
    auto_handoff: bool,
    lazy: bool,
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
        max_items=max_items,
        just_launched_iterm=just_launched_iterm,
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
        max_items=args.max_items,
        just_launched_iterm=not iterm_was_running,
    )
    try:
        iterm2.run_until_complete(runner, retry=True)
    except Exception:
        logger.exception("Failed to build workspace for '%s'", args.root)
        sys.exit(1)


if __name__ == "__main__":
    main()
