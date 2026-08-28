"""Install bundled slash-commands and their prompts into the user's home dir.

Ships the ``planner``/``implementer``/``principal``/``researcher``/``reviewer``
command+prompt pairs so any clone or pipx install of claudespace gets them
registered globally, without the installer having to know their contents.

Commands go to ``~/.claude/commands`` (global slash-commands); prompts go to
``~/.ai/prompts`` (referenced by each command via its absolute ``~/.ai/prompts``
path, so commands work regardless of the project's cwd). Existing
files are always overwritten with the bundled version, so re-running this
after an upgrade picks up fixes - any local edits to a prompt or command are
not preserved.

Also registers a global ``Stop`` hook that calls ``claudespace-handoff``
after every turn. The hook itself is a fast no-op outside claudespace panes
(``handoff.py`` bails out immediately if ``CLAUDESPACE_ROLE`` isn't set), so
it's safe to install once for every Claude Code session on the machine
rather than per-project.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from importlib import resources
from pathlib import Path

from claudespace.config import (
    USER_TEMPLATES_PATH,
    ensure_agentic_template_seeded,
    ensure_native_template_seeded,
    migrate_role_commands,
)

logger = logging.getLogger(__name__)

COMMANDS_DEST = Path.home() / ".claude" / "commands"
PROMPTS_DEST = Path.home() / ".ai" / "prompts"
SETTINGS_DEST = Path.home() / ".claude" / "settings.json"

HANDOFF_HOOK_COMMAND = "claudespace-handoff"
LEGACY_HANDOFF_HOOK_COMMAND = "claudespace:handoff"


def _copy_all(src_dir: resources.abc.Traversable, dest_dir: Path) -> int:
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for entry in src_dir.iterdir():
        if not entry.is_file():
            continue
        dest = dest_dir / entry.name
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


def _remove_hook_command(stop_hooks: list, command: str) -> bool:
    """Drop any ``hooks`` entry whose command matches, in place.

    An entry is removed only if every hook inside it matches ``command``,
    so a hand-edited entry that bundles the handoff hook alongside other
    commands is left alone rather than silently losing its other hooks.
    Returns ``True`` if anything was removed.
    """
    removed = False
    for entry in list(stop_hooks):
        entry_hooks = entry.get("hooks", [])
        if entry_hooks and all(h.get("command") == command for h in entry_hooks):
            stop_hooks.remove(entry)
            removed = True
    return removed


def _read_settings() -> dict:
    """Parse ``~/.claude/settings.json``, or raise a message the user can act on.

    This file belongs to Claude Code, not to claudespace - it can be
    hand-edited, written by another tool, or left truncated by a crash. An
    unguarded ``json.loads`` here used to abort the whole install with a
    bare ``JSONDecodeError`` traceback *after* the package was already
    installed, leaving a half-configured machine and no clue which file was
    at fault.
    """
    if not SETTINGS_DEST.exists():
        return {}
    try:
        return json.loads(SETTINGS_DEST.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{SETTINGS_DEST} is not valid JSON ({exc}). claudespace needs to "
            "add its handoff Stop hook there. Fix or move that file, then "
            "re-run 'claudespace-sync-assets'."
        ) from exc


def _write_settings(settings: dict) -> None:
    """Write ``settings`` back, keeping a timestamped backup of what was there.

    The write is a full ``json.dumps`` rewrite, so any comments, key order
    or formatting the user had is flattened. Backing the original up first
    makes that recoverable instead of silent.
    """
    if SETTINGS_DEST.exists():
        backup = SETTINGS_DEST.with_name(
            f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        shutil.copyfile(SETTINGS_DEST, backup)
        logger.info("Backed up %s to %s", SETTINGS_DEST, backup)
    SETTINGS_DEST.write_text(json.dumps(settings, indent=2) + "\n")


def _install_handoff_hook() -> bool:
    """Add the claudespace handoff Stop hook to ``~/.claude/settings.json``.

    Merges into whatever settings already exist rather than overwriting the
    file, and is idempotent - re-running never adds a duplicate entry.

    Also drops any hook entry still pointing at the old colon-named
    ``claudespace:handoff`` command (renamed to ``claudespace-handoff`` -
    see ``config.migrate_role_commands`` for why) before checking whether
    the current command is already installed, so an update replaces the
    stale entry in place instead of leaving it alongside a second, working
    one.

    Returns ``True`` if the file was newly added or an existing entry was
    modified.
    """
    SETTINGS_DEST.parent.mkdir(parents=True, exist_ok=True)
    settings = _read_settings()

    hooks = settings.setdefault("hooks", {})
    stop_hooks = hooks.setdefault("Stop", [])

    removed_legacy = _remove_hook_command(stop_hooks, LEGACY_HANDOFF_HOOK_COMMAND)

    if _hook_already_installed(stop_hooks):
        if removed_legacy:
            _write_settings(settings)
        return removed_legacy

    stop_hooks.append(
        {"hooks": [{"type": "command", "command": HANDOFF_HOOK_COMMAND}]}
    )
    _write_settings(settings)
    return True


def remove_handoff_hook() -> bool:
    """Drop claudespace's Stop hook from ``~/.claude/settings.json``.

    The hook is global - it fires for every Claude Code session on the
    machine, not just claudespace panes. Without this, uninstalling the
    package (``pipx uninstall claudespace``) leaves a Stop hook behind that
    points at a command which no longer exists, so every turn of every
    session on the machine fails a hook forever. Returns ``True`` if
    anything was removed.
    """
    if not SETTINGS_DEST.exists():
        return False
    settings = _read_settings()
    stop_hooks = settings.get("hooks", {}).get("Stop", [])
    if not stop_hooks:
        return False

    removed = _remove_hook_command(stop_hooks, HANDOFF_HOOK_COMMAND)
    removed |= _remove_hook_command(stop_hooks, LEGACY_HANDOFF_HOOK_COMMAND)
    if not removed:
        return False

    # Leave no empty scaffolding behind that wasn't there before.
    if not stop_hooks:
        settings["hooks"].pop("Stop", None)
    if not settings.get("hooks"):
        settings.pop("hooks", None)
    _write_settings(settings)
    return True


def uninstall() -> None:
    """Reverse what ``sync_assets`` installed, except the user's own files.

    Removes the global Stop hook and the bundled command/prompt files. Does
    *not* touch ``~/.config/claudespace/templates.toml`` (the user's own
    templates, which they may have customized heavily) or any
    ``.claudespace/`` marker directory inside a project.
    """
    hook_removed = remove_handoff_hook()

    removed_files = 0
    assets = resources.files("claudespace.assets")
    for subdir, dest_dir in (("commands", COMMANDS_DEST), ("prompts", PROMPTS_DEST)):
        for entry in assets.joinpath(subdir).iterdir():
            if not entry.is_file():
                continue
            dest = dest_dir / entry.name
            if dest.exists():
                dest.unlink()
                removed_files += 1

    logger.info(
        "Removed %d bundled file(s)%s",
        removed_files,
        " and the handoff Stop hook" if hook_removed else "",
    )
    logger.info(
        "Left %s alone - delete it yourself if you want your templates gone too.",
        USER_TEMPLATES_PATH,
    )


def sync_assets() -> None:
    """Copy bundled commands/prompts into place, overwriting any that exist."""
    assets = resources.files("claudespace.assets")

    commands_copied = _copy_all(assets.joinpath("commands"), COMMANDS_DEST)
    prompts_copied = _copy_all(assets.joinpath("prompts"), PROMPTS_DEST)
    hook_installed = _install_handoff_hook()
    commands_migrated = migrate_role_commands()
    native_seeded = ensure_native_template_seeded()
    agentic_seeded = ensure_agentic_template_seeded()

    logger.info(
        "Synced %d command(s) to %s, %d prompt(s) to %s",
        commands_copied,
        COMMANDS_DEST,
        prompts_copied,
        PROMPTS_DEST,
    )
    if hook_installed:
        logger.info("Installed claudespace handoff Stop hook in %s", SETTINGS_DEST)
    if commands_migrated:
        logger.info(
            "Migrated retired claudespace-<role> commands in %s", USER_TEMPLATES_PATH
        )
    if native_seeded:
        logger.info("Seeded 'native' template in %s", USER_TEMPLATES_PATH)
    if agentic_seeded:
        logger.info("Seeded 'agentic' template in %s", USER_TEMPLATES_PATH)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sync_assets()
