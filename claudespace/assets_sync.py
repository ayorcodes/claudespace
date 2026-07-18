"""Install bundled slash-commands and their prompts into the user's home dir.

Ships the ``planner``/``implementer``/``principal``/``researcher``/``reviewer``
command+prompt pairs so any clone or pipx install of claudespace gets them
registered globally, without the installer having to know their contents.

Commands go to ``~/.claude/commands`` (global slash-commands); prompts go to
``~/.ai/prompts`` (referenced by relative path from each command). Existing
files are left untouched - this only fills in what's missing, so local edits
to a prompt are never clobbered by a reinstall.
"""

from __future__ import annotations

import logging
import shutil
from importlib import resources
from pathlib import Path

logger = logging.getLogger(__name__)

COMMANDS_DEST = Path.home() / ".claude" / "commands"
PROMPTS_DEST = Path.home() / ".ai" / "prompts"


def _copy_missing(src_dir: resources.abc.Traversable, dest_dir: Path) -> int:
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for entry in src_dir.iterdir():
        if not entry.is_file():
            continue
        dest = dest_dir / entry.name
        if dest.exists():
            continue
        with resources.as_file(entry) as src_path:
            shutil.copyfile(src_path, dest)
        copied += 1
    return copied


def sync_assets() -> None:
    """Copy bundled commands/prompts into place, skipping files that exist."""
    assets = resources.files("claudespace.assets")

    commands_copied = _copy_missing(assets.joinpath("commands"), COMMANDS_DEST)
    prompts_copied = _copy_missing(assets.joinpath("prompts"), PROMPTS_DEST)

    logger.info(
        "Synced %d command(s) to %s, %d prompt(s) to %s",
        commands_copied,
        COMMANDS_DEST,
        prompts_copied,
        PROMPTS_DEST,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sync_assets()
