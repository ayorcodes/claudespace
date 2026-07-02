"""Command-line interface for the workspace launcher."""

from __future__ import annotations

import argparse
import functools
import logging
import os
import sys

import iterm2

from claudespace import environment, utils, workspace
from claudespace.config import DEFAULT_TEMPLATE, TEMPLATES, get_template

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claudespace",
        description="Build or attach to an iTerm2 development workspace for a folder.",
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
        "--list-templates",
        action="store_true",
        help="List available template names and exit.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging."
    )
    return parser


async def _run(
    connection: iterm2.Connection, *, root: str, template: str, force_new: bool
) -> None:
    await workspace.open_workspace(connection, root, template, force_new)


def main() -> None:
    """Entrypoint installed as the ``claudespace`` console script."""
    parser = _build_parser()
    args = parser.parse_args()
    utils.setup_logging(args.verbose)

    environment.require_macos()

    if args.list_templates:
        for template_name in sorted(TEMPLATES):
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
        _run, root=args.root, template=args.template, force_new=args.new
    )
    try:
        iterm2.run_until_complete(runner, retry=True)
    except Exception:
        logger.exception("Failed to build workspace for '%s'", args.root)
        sys.exit(1)


if __name__ == "__main__":
    main()
