"""Install bundled slash-commands and their prompts into the user's home dir.

Ships the ``planner``/``implementer``/``principal``/``researcher``/``reviewer``
command+prompt pairs so any clone or pipx install of claudespace gets them
registered globally, without the installer having to know their contents.

Commands go to ``<config>/commands`` (global slash-commands) and the ``Stop``
hook into ``<config>/settings.json``, for *every* Claude Code config home on
the machine - see ``claude_config_dirs``. A user with several profiles
(``~/.claude``, ``~/.claudeMax``, ...) can point a claudespace template at a
pane that runs under any of them, and a profile missing the hook silently
never hands off.

Prompts go to ``~/.ai/prompts``, which is shared: each command references it
by absolute path, so one copy serves every config home and every cwd.

Existing files are always overwritten with the bundled version, so re-running
this after an upgrade picks up fixes - any local edits to a prompt or command
are not preserved.

The hook is a fast no-op outside claudespace panes (``handoff.py`` bails out
immediately if ``CLAUDESPACE_ROLE`` isn't set), so it's safe to install once
for every Claude Code session rather than per-project.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from importlib import resources
from pathlib import Path

from claudespace import __version__
from claudespace.config import (
    USER_TEMPLATES_PATH,
    ensure_agentic_template_seeded,
    ensure_native_template_seeded,
    migrate_role_commands,
)

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = Path.home() / ".claude"
COMMANDS_DEST = DEFAULT_CONFIG_DIR / "commands"
PROMPTS_DEST = Path.home() / ".ai" / "prompts"
SETTINGS_DEST = DEFAULT_CONFIG_DIR / "settings.json"

# Where the first-run sync sentinel lives (AD5) - one file per version, so an
# upgrade (either channel) re-triggers a sync on the next real invocation
# instead of needing its own explicit sync call.
SYNC_SENTINEL_DIR = Path.home() / ".config" / "claudespace"

HANDOFF_HOOK_COMMAND = "claudespace-handoff"
LEGACY_HANDOFF_HOOK_COMMAND = "claudespace:handoff"

# PreToolUse guard that stops a read-only role writing to code (see guard.py).
# Matched against the write tools only, so it never runs for a Read or Bash.
GUARD_HOOK_COMMAND = "claudespace-guard"
GUARD_HOOK_MATCHER = "Edit|Write|NotebookEdit|MultiEdit"


def claude_config_dirs() -> list[Path]:
    """Every Claude Code config home to install the hook and commands into.

    Claude Code reads its settings from ``$CLAUDE_CONFIG_DIR``, defaulting to
    ``~/.claude``. Users routinely run more than one - a separate profile per
    account or plan, reached through a shell alias like::

        alias claudemax='CLAUDE_CONFIG_DIR=$HOME/.claudeMax claude'

    and a claudespace template can point a pane at exactly such an alias. A
    pane launched that way reads its hooks from *that* profile, so installing
    only into ``~/.claude`` leaves those panes with no handoff hook at all -
    or worse, whatever stale one that profile happens to still carry, which
    is how a long-deleted ``claudespace:handoff`` kept firing and failing.

    Discovery: ``$CLAUDE_CONFIG_DIR`` if set, always ``~/.claude``, plus any
    sibling ``~/.claude*`` directory that already has a ``settings.json``
    (i.e. a real profile, not an unrelated dotfile). Set
    ``CLAUDESPACE_CONFIG_DIRS`` to a ``:``-separated list to override the
    discovery entirely.
    """
    override = os.environ.get("CLAUDESPACE_CONFIG_DIRS")
    if override:
        candidates = [Path(p).expanduser() for p in override.split(os.pathsep) if p]
    else:
        candidates = []
        env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        if env_dir:
            candidates.append(Path(env_dir).expanduser())
        candidates.append(DEFAULT_CONFIG_DIR)
        candidates += [
            path
            for path in sorted(Path.home().glob(".claude*"))
            if path.is_dir() and (path / "settings.json").is_file()
        ]

    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in candidates:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key not in seen:
            seen.add(key)
            ordered.append(path)
    return ordered


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


def _copy_tree(src_dir: resources.abc.Traversable, dest_dir: Path) -> int:
    """Like ``_copy_all`` but recursive - used for the vendored tmux-plugin
    trees (Increment 2), which are real multi-directory plugin repos, not a
    flat pile of files. Replaces ``dest_dir`` wholesale each time, same
    "always overwritten, no local edits preserved" policy as everything
    else this module syncs.
    """
    with resources.as_file(src_dir) as src_path:
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.copytree(src_path, dest_dir)
    return sum(1 for p in dest_dir.rglob("*") if p.is_file())


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


def _read_settings(settings_path: Path) -> dict:
    """Parse ``~/.claude/settings.json``, or raise a message the user can act on.

    This file belongs to Claude Code, not to claudespace - it can be
    hand-edited, written by another tool, or left truncated by a crash. An
    unguarded ``json.loads`` here used to abort the whole install with a
    bare ``JSONDecodeError`` traceback *after* the package was already
    installed, leaving a half-configured machine and no clue which file was
    at fault.
    """
    if not settings_path.exists():
        return {}
    try:
        return json.loads(settings_path.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{settings_path} is not valid JSON ({exc}). claudespace needs to "
            "add its handoff Stop hook there. Fix or move that file, then "
            "re-run 'claudespace-sync-assets'."
        ) from exc


def _write_settings(settings_path: Path, settings: dict) -> None:
    """Write ``settings`` back, keeping a timestamped backup of what was there.

    The write is a full ``json.dumps`` rewrite, so any comments, key order
    or formatting the user had is flattened. Backing the original up first
    makes that recoverable instead of silent.
    """
    if settings_path.exists():
        backup = settings_path.with_name(
            f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        shutil.copyfile(settings_path, backup)
        logger.info("Backed up %s to %s", settings_path, backup)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")


def _install_handoff_hook(settings_path: Path) -> bool:
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
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = _read_settings(settings_path)

    hooks = settings.setdefault("hooks", {})
    stop_hooks = hooks.setdefault("Stop", [])

    removed_legacy = _remove_hook_command(stop_hooks, LEGACY_HANDOFF_HOOK_COMMAND)

    if _hook_already_installed(stop_hooks):
        if removed_legacy:
            _write_settings(settings_path, settings)
        return removed_legacy

    stop_hooks.append(
        {"hooks": [{"type": "command", "command": HANDOFF_HOOK_COMMAND}]}
    )
    _write_settings(settings_path, settings)
    return True


def _install_guard_hook(settings_path: Path) -> bool:
    """Add the PreToolUse guard hook, matched to the write tools only.

    Idempotent, and merged into whatever hooks already exist. Like the Stop
    hook it is installed globally: ``guard.py`` exits silently unless
    ``CLAUDESPACE_ROLE`` names a restricted role, so it costs an unrelated
    session nothing.
    """
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = _read_settings(settings_path)
    pre_hooks = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])

    for entry in pre_hooks:
        for hook in entry.get("hooks", []):
            if hook.get("command") == GUARD_HOOK_COMMAND:
                return False

    pre_hooks.append(
        {
            "matcher": GUARD_HOOK_MATCHER,
            "hooks": [{"type": "command", "command": GUARD_HOOK_COMMAND}],
        }
    )
    _write_settings(settings_path, settings)
    return True


def remove_guard_hook(settings_path: Path) -> bool:
    """Drop the PreToolUse guard hook from ``settings_path``."""
    if not settings_path.exists():
        return False
    settings = _read_settings(settings_path)
    pre_hooks = settings.get("hooks", {}).get("PreToolUse", [])
    if not pre_hooks or not _remove_hook_command(pre_hooks, GUARD_HOOK_COMMAND):
        return False
    if not pre_hooks:
        settings["hooks"].pop("PreToolUse", None)
    if not settings.get("hooks"):
        settings.pop("hooks", None)
    _write_settings(settings_path, settings)
    return True


def remove_handoff_hook(settings_path: Path) -> bool:
    """Drop claudespace's Stop hook from ``~/.claude/settings.json``.

    The hook is global - it fires for every Claude Code session on the
    machine, not just claudespace panes. Without this, uninstalling the
    package (``pipx uninstall claudespace``) leaves a Stop hook behind that
    points at a command which no longer exists, so every turn of every
    session on the machine fails a hook forever. Returns ``True`` if
    anything was removed.
    """
    if not settings_path.exists():
        return False
    settings = _read_settings(settings_path)
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
    _write_settings(settings_path, settings)
    return True


def _remove_bundled(subdir: str, dest_dir: Path) -> int:
    """Delete this package's own bundled files from ``dest_dir``."""
    removed = 0
    for entry in resources.files("claudespace.assets").joinpath(subdir).iterdir():
        if not entry.is_file():
            continue
        dest = dest_dir / entry.name
        if dest.exists():
            dest.unlink()
            removed += 1
    return removed


def uninstall() -> None:
    """Reverse what ``sync_assets`` installed, except the user's own files.

    Removes the Stop hook and bundled commands from *every* config home
    ``sync_assets`` installs into (see ``claude_config_dirs``), not just
    ``~/.claude`` - a hook left behind in a second profile fails on every
    turn of every session that profile runs. Does *not* touch
    ``~/.config/claudespace/templates.toml`` (the user's own templates) or
    any ``.claudespace/`` marker directory inside a project.
    """
    hooks_removed = 0
    removed_files = 0
    for config_dir in claude_config_dirs():
        if remove_handoff_hook(config_dir / "settings.json"):
            hooks_removed += 1
            logger.info("Removed the handoff Stop hook from %s", config_dir)
        if remove_guard_hook(config_dir / "settings.json"):
            hooks_removed += 1
            logger.info("Removed the read-only guard hook from %s", config_dir)
        removed_files += _remove_bundled("commands", config_dir / "commands")

    removed_files += _remove_bundled("prompts", PROMPTS_DEST)

    logger.info(
        "Removed %d bundled file(s) and %d Stop hook(s)",
        removed_files,
        hooks_removed,
    )
    logger.info(
        "Left %s alone - delete it yourself if you want your templates gone too.",
        USER_TEMPLATES_PATH,
    )


def sync_assets() -> None:
    """Copy bundled commands/prompts into place, overwriting any that exist."""
    assets = resources.files("claudespace.assets")

    config_dirs = claude_config_dirs()
    commands_copied = 0
    for config_dir in config_dirs:
        commands_copied += _copy_all(assets.joinpath("commands"), config_dir / "commands")
        if _install_handoff_hook(config_dir / "settings.json"):
            logger.info("Installed the handoff Stop hook in %s", config_dir)
        if _install_guard_hook(config_dir / "settings.json"):
            logger.info("Installed the read-only guard hook in %s", config_dir)

    prompts_copied = _copy_all(assets.joinpath("prompts"), PROMPTS_DEST)
    commands_migrated = migrate_role_commands()
    native_seeded = ensure_native_template_seeded()
    agentic_seeded = ensure_agentic_template_seeded()

    # Vendored tmux-resurrect/tmux-continuum (Increment 2) - imported here,
    # not at module scope, so a pip/pipx install without the tmux backend's
    # extra surface still imports this module fine (there is no extra
    # dependency, but this keeps the import graph the same shape as the
    # rest of the module's lazy backend imports).
    from claudespace.backends import tmux_persist
    from claudespace.config import load_tmux_persistence

    plugins_copied = _copy_tree(assets.joinpath("tmux-plugins"), tmux_persist.PLUGINS_DIR)
    persist, persist_interval_minutes = load_tmux_persistence()
    tmux_persist.write_conf(persist=persist, interval_minutes=persist_interval_minutes)

    logger.info(
        "Synced %d command(s) across %d Claude config dir(s) (%s), "
        "%d prompt(s) to %s, %d tmux-plugin file(s) to %s",
        commands_copied,
        len(config_dirs),
        ", ".join(str(d) for d in config_dirs),
        prompts_copied,
        PROMPTS_DEST,
        plugins_copied,
        tmux_persist.PLUGINS_DIR,
    )
    if commands_migrated:
        logger.info(
            "Migrated retired claudespace-<role> commands in %s", USER_TEMPLATES_PATH
        )
    if native_seeded:
        logger.info("Seeded 'native' template in %s", USER_TEMPLATES_PATH)
    if agentic_seeded:
        logger.info("Seeded 'agentic' template in %s", USER_TEMPLATES_PATH)


def _sync_sentinel_path(version: str = __version__) -> Path:
    return SYNC_SENTINEL_DIR / f".asset-sync-{version}"


def sync_if_needed(version: str = __version__) -> bool:
    """Run ``sync_assets()`` once per version (AD5).

    Both the npm and pipx channels now trigger asset sync the same way: from
    ``cli.main()`` on the first real invocation after install or upgrade,
    rather than from the installer/postinstall (D4 - postinstall may run as
    root, and this must run as the user). Guarded by a per-version sentinel
    file rather than a per-install one, so bumping the version re-syncs on
    the next invocation regardless of channel. Returns whether sync actually
    ran. A failed sync does not write the sentinel, so the next invocation
    retries rather than silently staying out of date.
    """
    sentinel = _sync_sentinel_path(version)
    if sentinel.exists():
        return False
    sync_assets()
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("")
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sync_assets()
