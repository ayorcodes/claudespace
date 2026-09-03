"""Template definitions.

A ``Template`` is the *shape* of a workspace - a layout plus the command
each role runs. It knows nothing about any particular folder; ``workspace``
is always pointed at a folder via ``--root`` (defaulting to the current
directory), so the same template works from anywhere.

Add a new AI role or change what a role runs by editing a ``Template``
here - no launcher code needs to change.

Users can also add their own templates without touching this file (and
without losing them on ``claudespace update``) by dropping a TOML file at
``~/.config/claudespace/templates.toml``. See ``load_user_templates`` for
the file format.

The ``native`` template itself lives in ``templates.toml`` too - see
``ensure_native_template_seeded``, which is called by ``sync_assets()`` on
every install/update so a missing or stale file always gets ``native``
written as its first entry.
"""

from __future__ import annotations

import logging
import os
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# AD5: which terminal claudespace drives. No general config file existed
# before the tmux backend - this is the first key in it. Absent file or key
# defaults to iTerm2 (FR1/AC8), never a silent third option.
CONFIG_PATH = Path.home() / ".config" / "claudespace" / "config.toml"
DEFAULT_TERMINAL_BACKEND = "iterm2"
KNOWN_TERMINAL_BACKENDS = frozenset({"iterm2", "tmux"})

DEFAULT_TMUX_VIEWER = "ghostty"


def load_terminal_backend(
    path: Path | None = None, *, env: dict[str, str] | None = None
) -> str:
    """Which terminal backend to use: ``CLAUDESPACE_TERMINAL`` env var (for
    tests/one-offs - the Planning Brief forbids a per-command flag as the
    primary UX, but an env override for testing is not that), else
    ``config.toml``'s ``[terminal] backend``, else ``"iterm2"``.

    Raises ``ValueError`` naming the bad value if either source names
    something other than a known backend - a fast, named startup error
    rather than a silent fallback (mirrors ``get_template``'s style).

    ``path`` defaults to the module-level ``CONFIG_PATH`` - resolved here,
    not as the parameter's default value, so tests can monkeypatch
    ``config.CONFIG_PATH`` and have callers that pass no ``path`` (like
    ``backends.get_backend``) pick it up.
    """
    if path is None:
        path = CONFIG_PATH
    env = os.environ if env is None else env

    env_value = env.get("CLAUDESPACE_TERMINAL")
    if env_value:
        if env_value not in KNOWN_TERMINAL_BACKENDS:
            raise ValueError(
                f"Unknown CLAUDESPACE_TERMINAL '{env_value}'. Known backends: "
                f"{', '.join(sorted(KNOWN_TERMINAL_BACKENDS))}"
            )
        return env_value

    if not path.exists():
        return DEFAULT_TERMINAL_BACKEND

    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML in '{path}': {exc}") from exc

    value = data.get("terminal", {}).get("backend")
    if value is None:
        return DEFAULT_TERMINAL_BACKEND
    if value not in KNOWN_TERMINAL_BACKENDS:
        raise ValueError(
            f"Unknown terminal backend '{value}' in '{path}'. Known backends: "
            f"{', '.join(sorted(KNOWN_TERMINAL_BACKENDS))}"
        )
    return value


def load_tmux_viewer(path: Path | None = None) -> str:
    """``[terminal.tmux] viewer`` from ``config.toml`` (AD5) - which
    terminal ``TmuxBackend`` spawns running ``tmux attach``. Defaults to
    ``"ghostty"``, the backend's whole reason for existing; unrelated to
    ``load_terminal_backend`` (env has no override for this - it's a
    cosmetic choice, not something worth a second env var).
    """
    if path is None:
        path = CONFIG_PATH
    if not path.exists():
        return DEFAULT_TMUX_VIEWER
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML in '{path}': {exc}") from exc
    return data.get("terminal", {}).get("tmux", {}).get("viewer") or DEFAULT_TMUX_VIEWER


DEFAULT_TMUX_PERSIST = True
DEFAULT_TMUX_PERSIST_INTERVAL_MINUTES = 15


def load_tmux_persistence(path: Path | None = None) -> tuple[bool, int]:
    """``(persist, persist_interval_minutes)`` from ``[terminal.tmux]`` in
    ``config.toml`` (Increment 2, AD12) - whether the tmux backend loads
    tmux-resurrect/tmux-continuum on its private socket, and how often
    continuum autosaves. Defaults to on, every 15 minutes: negligible risk
    on a socket the user's own tmux never touches (AD8), and durability
    across a reboot is exactly what this was built for.

    ``persist = false`` is the documented off-switch - live (Increment 1)
    behavior is then unchanged, since ``TmuxBackend`` doesn't even write a
    private config in that case (see ``backends/tmux_persist.write_conf``).
    """
    if path is None:
        path = CONFIG_PATH
    if not path.exists():
        return DEFAULT_TMUX_PERSIST, DEFAULT_TMUX_PERSIST_INTERVAL_MINUTES
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML in '{path}': {exc}") from exc
    tmux_table = data.get("terminal", {}).get("tmux", {})
    persist = tmux_table.get("persist")
    if persist is None:
        persist = DEFAULT_TMUX_PERSIST
    interval = tmux_table.get("persist_interval_minutes", DEFAULT_TMUX_PERSIST_INTERVAL_MINUTES)
    return bool(persist), int(interval)


@dataclass(frozen=True, slots=True)
class PaneConfig:
    """A single pane's identity and startup command.

    ``role`` must match one of the role names expected by the template's
    chosen layout (see ``layouts.py``) - it is both the pane marker used for
    duplicate-detection and the key layouts use to place panes on screen.
    """

    role: str
    command: str


@dataclass(frozen=True, slots=True)
class Template:
    """A reusable workspace shape: a layout plus each role's command.

    ``entry_role`` is which role's pane is shown first in ``--lazy``
    workspaces - the only pane visible until it hands off to another role
    (see each backend's ``build_workspace``). Ignored outside ``--lazy`` mode, where
    every pane in ``panes`` is launched immediately as today.
    """

    layout: str
    panes: tuple[PaneConfig, ...]
    entry_role: str = "researcher"

    def __post_init__(self) -> None:
        roles = {pane.role for pane in self.panes}
        if self.entry_role not in roles:
            raise ValueError(
                f"Template's entry_role '{self.entry_role}' is not one of its "
                f"panes' roles {sorted(roles)}"
            )


DEFAULT_TEMPLATE = "native"

# The model and effort each role runs at, as a plain ``claude`` invocation.
# This is the single source of truth: the seeded ``templates.toml`` entries
# below are generated from it, so a user reading or editing that file sees
# the real command rather than an opaque wrapper script.
#
# ``--append-system-prompt-file`` is deliberately absent - ``backends/common.py``'s
# ``_command_with_baked_persona`` appends it per pane, since only it knows
# which role a given pane is.
ROLE_COMMANDS: dict[str, str] = {
    "conductor": "claude --model claude-opus-5 --effort medium --permission-mode auto",
    "principal": "claude --model claude-opus-5 --effort medium --permission-mode auto",
    "planner": "claude --model claude-opus-5 --effort medium --permission-mode auto",
    "implementer": "claude --model claude-sonnet-5 --effort medium --permission-mode auto",
    "reviewer": "claude --model claude-sonnet-5 --effort medium --permission-mode auto",
    "researcher": "claude --model claude-sonnet-5 --effort low --permission-mode auto",
}

# Roles whose prompts forbid them from touching code, enforced by the
# PreToolUse guard hook (see ``guard.py``) rather than by their prompt alone.
#
# Telling a role "you do not implement" is a request, not a boundary: a
# researcher pane was observed rewriting a component mid-investigation,
# because editing is the obvious way to act on what it just found and every
# pane runs with ``--permission-mode auto``, so nothing prompted first.
#
# ``--disallowed-tools Edit`` does not solve this - the model simply reaches
# for ``Write`` instead, which overwrites an existing file just as well - and
# denying ``Write`` too would stop these roles persisting the one artifact
# each of them exists to produce. The distinction that actually matters is
# *what* is being written, not *which tool* writes it, so the guard hook
# filters by path instead.
READ_ONLY_ROLES: frozenset[str] = frozenset(
    {"researcher", "planner", "principal", "reviewer"}
)

_NATIVE_ROLES = ("principal", "implementer", "reviewer", "planner", "researcher")
_AGENTIC_ROLES = ("conductor",) + _NATIVE_ROLES


def _panes(roles: tuple[str, ...]) -> tuple[PaneConfig, ...]:
    return tuple(PaneConfig(role=role, command=ROLE_COMMANDS[role]) for role in roles)


def _template_toml(name: str, layout: str, roles: tuple[str, ...], entry_role: str = "") -> str:
    """Render a ``[templates.<name>]`` table from ``ROLE_COMMANDS``."""
    lines = [f"[templates.{name}]", f'layout = "{layout}"']
    if entry_role:
        lines.append(f'entry_role = "{entry_role}"')
    for role in roles:
        lines += ["", f"[[templates.{name}.panes]]", f'role = "{role}"', f'command = "{ROLE_COMMANDS[role]}"']
    return "\n".join(lines) + "\n"


# Built-in templates that ship purely as Python fallbacks in case
# ``templates.toml`` is unreadable or hasn't been seeded yet. The
# authoritative copy of "native" lives in ``templates.toml`` once
# ``ensure_native_template_seeded`` has run (see below).
TEMPLATES: dict[str, Template] = {
    "native": Template(layout="main_left_grid_right", panes=_panes(_NATIVE_ROLES)),
    # Opt-in template for unattended multi-feature runs: adds a conductor
    # pane on top of the same five pipeline roles native uses. Not merged
    # into native - a project that just wants the single-feature pipeline
    # shouldn't get a 6th pane it never uses. See conductor.prompt.md and
    # pipeline.py's reviewer -> conductor alt_next_roles for how the
    # backlog-driven outer loop works. entry_role is conductor, not the
    # Template default of researcher, since conductor is where a run
    # starts (backlog generation) in this template.
    "agentic": Template(
        layout="conductor_main_left_grid_right",
        entry_role="conductor",
        panes=_panes(_AGENTIC_ROLES),
    ),
}

# Canonical pane definitions for every role the pipeline itself knows how to
# route to (see pipeline.py's PIPELINE), independent of any one template.
# "agentic" is the superset of every built-in role, so it doubles as the
# canonical registry rather than duplicating the list.
#
# Used as a fallback when a handoff needs to reveal a role a workspace's own
# template doesn't define - most notably conductor, which "native" leaves
# out (see reviewer.prompt.md's "Post-review follow-up" section) - so that
# role can still be spun up on demand instead of the handoff silently going
# nowhere. See iterm.reveal_role and handoff._reveal_destination.
CANONICAL_PANES: dict[str, PaneConfig] = {
    pane.role: pane for pane in TEMPLATES["agentic"].panes
}

NATIVE_TEMPLATE_TOML = _template_toml("native", "main_left_grid_right", _NATIVE_ROLES)

AGENTIC_TEMPLATE_TOML = _template_toml(
    "agentic", "conductor_main_left_grid_right", _AGENTIC_ROLES, entry_role="conductor"
)


USER_TEMPLATES_PATH = Path.home() / ".config" / "claudespace" / "templates.toml"

# Every pane command claudespace ever shipped that has to become a plain
# ``claude`` invocation, mapped to its replacement.
#
# Roles used to launch through per-role console scripts
# (``claudespace-principal`` and friends) that hardcoded a model and effort
# in Python. That put role configuration in two places at once - the script
# and templates.toml - and made the model a role ran at invisible to anyone
# reading their own template. The scripts are gone; ROLE_COMMANDS is the
# only place a default model lives, and a template spells out its own
# command in full.
#
# The colon-separated spelling (``claudespace:researcher``) predates even
# that: uv's wheel installer misparses a colon in an entry-point name, so
# they were renamed to dashes. Both spellings are mapped here because the
# seeders only write when a template is *missing*, so an already-seeded
# file is never touched by a plain reinstall and can still carry either.
_LEGACY_ROLE_COMMANDS: dict[str, str] = {
    legacy: ROLE_COMMANDS[role]
    for role in ROLE_COMMANDS
    for legacy in (f"claudespace-{role}", f"claudespace:{role}")
}


def migrate_role_commands(path: Path = USER_TEMPLATES_PATH) -> bool:
    """Rewrite retired ``claudespace-<role>`` pane commands in ``templates.toml``.

    Called by ``sync_assets()`` on every install/update, before the
    native/agentic seeders. Does a plain string substitution rather than a
    parse/rewrite round-trip through ``tomllib`` (which has no writer and
    would require a TOML-writing dependency) - safe here because the retired
    names are a fixed, known set of literal strings that only ever appear as
    a whole ``command = "..."`` value, never as substrings of anything a
    user would plausibly write themselves.

    A timestamped backup is written next to the file before it is modified:
    this rewrites a file the user hand-edits, and the substitution is only
    as good as the assumption above.

    Returns ``True`` if the file was modified.
    """
    if not path.exists():
        return False

    existing = path.read_text()
    updated = existing
    for old, new in _LEGACY_ROLE_COMMANDS.items():
        # Match the full quoted value so a longer command that merely
        # mentions the old name (a user's own wrapper, say) is left alone.
        updated = updated.replace(f'"{old}"', f'"{new}"')
        updated = updated.replace(f"'{old}'", f'"{new}"')

    if updated == existing:
        return False

    backup = path.with_suffix(f".toml.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    backup.write_text(existing)
    logger.info("Backed up %s to %s before migrating role commands", path, backup)
    path.write_text(updated)
    return True


def load_user_templates(path: Path = USER_TEMPLATES_PATH) -> dict[str, Template]:
    """Load user-defined templates from a TOML file, if present.

    Expected format - one ``[templates.<name>]`` table per template, each
    with a ``layout`` (must match a name registered in ``layouts.py``), a
    list of ``panes`` tables giving each pane's ``role`` and ``command``,
    and an optional ``entry_role`` (defaults to "researcher") naming which
    pane is shown first in ``--lazy`` workspaces::

        [templates.my-template]
        layout = "main_left_grid_right"
        entry_role = "researcher"

        [[templates.my-template.panes]]
        role = "principal"
        command = "claude --model claude-opus-5 --effort medium"

        [[templates.my-template.panes]]
        role = "implementer"
        command = "claude --model claude-sonnet-5"

    A missing file yields no user templates. A malformed file raises
    ``ValueError`` naming the problem so it fails fast at startup rather
    than surfacing as a confusing error later.
    """
    if not path.exists():
        return {}

    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML in '{path}': {exc}") from exc

    templates: dict[str, Template] = {}
    for name, table in data.get("templates", {}).items():
        try:
            layout = table["layout"]
            panes = tuple(
                PaneConfig(role=pane["role"], command=pane["command"])
                for pane in table["panes"]
            )
            entry_role = table.get("entry_role", "researcher")
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"Template '{name}' in '{path}' is missing a required field "
                f"(each template needs 'layout' and 'panes', each pane "
                f"needs 'role' and 'command'): {exc}"
            ) from exc
        try:
            templates[name] = Template(layout=layout, panes=panes, entry_role=entry_role)
        except ValueError as exc:
            raise ValueError(f"Template '{name}' in '{path}': {exc}") from exc

    return templates


def ensure_native_template_seeded(path: Path = USER_TEMPLATES_PATH) -> bool:
    """Ensure ``templates.toml`` has ``native`` defined, as its first entry.

    Called by ``sync_assets()`` on every install/update. Three cases:

    - File missing: create it with just ``native``.
    - File exists but has no ``[templates.native]`` table: prepend it, so
      ``native`` sorts first when the file is read top-to-bottom.
    - File already defines ``native``: leave it untouched, since the user
      may have customized it.

    Returns ``True`` if the file was created or modified.
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(NATIVE_TEMPLATE_TOML)
        return True

    existing = path.read_text()
    try:
        data = tomllib.loads(existing)
    except tomllib.TOMLDecodeError:
        # Leave malformed files alone - load_user_templates() will raise a
        # clear error naming the problem when the user actually loads it.
        return False

    if "native" in data.get("templates", {}):
        return False

    separator = "\n" if existing.startswith("\n") or not existing else "\n\n"
    path.write_text(NATIVE_TEMPLATE_TOML.rstrip("\n") + separator + existing)
    return True


def ensure_agentic_template_seeded(path: Path = USER_TEMPLATES_PATH) -> bool:
    """Ensure ``templates.toml`` has ``agentic`` defined.

    Mirrors ``ensure_native_template_seeded`` but kept as a separate
    function/entry rather than folded into it - ``agentic`` is opt-in (a
    project must still pass ``--template agentic`` to use it; seeding it
    only makes it discoverable via ``--list-templates`` and usable without
    the user hand-writing its TOML themselves), and keeping the two seed
    paths independent means a future built-in template doesn't require
    touching every existing seeding function.

    Same three cases as the native seeder: create the file with just
    ``agentic`` if missing, append the ``[templates.agentic]`` table if the
    file exists but lacks one (after any content already there, so it
    doesn't fight ``ensure_native_template_seeded`` for the "first entry"
    position), or leave it untouched if the user already defined
    ``agentic`` themselves.

    Returns ``True`` if the file was created or modified.
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(AGENTIC_TEMPLATE_TOML)
        return True

    existing = path.read_text()
    try:
        data = tomllib.loads(existing)
    except tomllib.TOMLDecodeError:
        return False

    if "agentic" in data.get("templates", {}):
        return False

    separator = "\n" if existing.startswith("\n") or not existing else "\n\n"
    path.write_text(existing.rstrip("\n") + separator + AGENTIC_TEMPLATE_TOML.lstrip("\n"))
    return True


def _all_templates() -> dict[str, Template]:
    """Built-in templates merged with user templates (user templates win)."""
    merged = dict(TEMPLATES)
    user_templates = load_user_templates()
    if user_templates:
        overridden = sorted(set(user_templates) & set(TEMPLATES))
        if overridden:
            logger.info(
                "User templates override built-in template(s): %s",
                ", ".join(overridden),
            )
        merged.update(user_templates)
    return merged


def get_template(name: str) -> Template:
    """Look up a registered template by name, built-in or user-defined.

    Raises ``KeyError`` with the list of known template names if ``name``
    is not registered.
    """
    all_templates = _all_templates()
    try:
        return all_templates[name]
    except KeyError:
        known = ", ".join(sorted(all_templates)) or "(none registered)"
        raise KeyError(f"Unknown template '{name}'. Known templates: {known}") from None


def list_templates() -> list[str]:
    """Sorted names of all templates, built-in and user-defined."""
    return sorted(_all_templates())
