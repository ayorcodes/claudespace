"""Install bundled slash-commands and their prompts into the user's home dir.

Ships the ``planner``/``implementer``/``principal``/``researcher``/``reviewer``
command+prompt pairs so any clone or pipx install of claudespace gets them
registered globally, without the installer having to know their contents.

Commands go to ``~/.claude/commands`` (global slash-commands); prompts go to
``~/.ai/prompts`` (referenced by relative path from each command). Existing
files are left untouched - this only fills in what's missing, so local edits
to a prompt are never clobbered by a reinstall.

Also registers a global ``Stop`` hook that calls ``claudespace:handoff``
after every turn. The hook itself is a fast no-op outside claudespace panes
(``handoff.py`` bails out immediately if ``CLAUDESPACE_ROLE`` isn't set), so
it's safe to install once for every Claude Code session on the machine
rather than per-project.
"""

from __future__ import annotations

import json
import logging
import shutil
from importlib import resources
from pathlib import Path

logger = logging.getLogger(__name__)

COMMANDS_DEST = Path.home() / ".claude" / "commands"
PROMPTS_DEST = Path.home() / ".ai" / "prompts"
SETTINGS_DEST = Path.home() / ".claude" / "settings.json"

HANDOFF_HOOK_COMMAND = "claudespace:handoff"


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


def _hook_already_installed(stop_hooks: list) -> bool:
    for entry in stop_hooks:
        for hook in entry.get("hooks", []):
            if hook.get("command") == HANDOFF_HOOK_COMMAND:
                return True
    return False


def _install_handoff_hook() -> bool:
    """Add the claudespace handoff Stop hook to ``~/.claude/settings.json``.

    Merges into whatever settings already exist rather than overwriting the
    file, and is idempotent - re-running never adds a duplicate entry.
    Returns ``True`` if the hook was newly added.
    """
    SETTINGS_DEST.parent.mkdir(parents=True, exist_ok=True)
    settings = {}
    if SETTINGS_DEST.exists():
        settings = json.loads(SETTINGS_DEST.read_text())

    hooks = settings.setdefault("hooks", {})
    stop_hooks = hooks.setdefault("Stop", [])

    if _hook_already_installed(stop_hooks):
        return False

    stop_hooks.append(
        {"hooks": [{"type": "command", "command": HANDOFF_HOOK_COMMAND}]}
    )
    SETTINGS_DEST.write_text(json.dumps(settings, indent=2) + "\n")
    return True


def sync_assets() -> None:
    """Copy bundled commands/prompts into place, skipping files that exist."""
    assets = resources.files("claudespace.assets")

    commands_copied = _copy_missing(assets.joinpath("commands"), COMMANDS_DEST)
    prompts_copied = _copy_missing(assets.joinpath("prompts"), PROMPTS_DEST)
    hook_installed = _install_handoff_hook()

    logger.info(
        "Synced %d command(s) to %s, %d prompt(s) to %s",
        commands_copied,
        COMMANDS_DEST,
        prompts_copied,
        PROMPTS_DEST,
    )
    if hook_installed:
        logger.info("Installed claudespace handoff Stop hook in %s", SETTINGS_DEST)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sync_assets()
