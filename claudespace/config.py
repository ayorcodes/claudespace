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
import tomllib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


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
    (see ``iterm.build_workspace``). Ignored outside ``--lazy`` mode, where
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

# Built-in templates that ship purely as Python fallbacks in case
# ``templates.toml`` is unreadable or hasn't been seeded yet. The
# authoritative copy of "native" lives in ``templates.toml`` once
# ``ensure_native_template_seeded`` has run (see below).
TEMPLATES: dict[str, Template] = {
    # claudespace-principal/claudespace-implementer/claudespace-reviewer/
    # claudespace-planner/claudespace-researcher are console-scripts
    # installed by this package (see roles.py), each pinned to its own
    # model and effort level.
    "native": Template(
        layout="main_left_grid_right",
        panes=(
            PaneConfig(role="principal", command="claudespace-principal"),
            PaneConfig(role="implementer", command="claudespace-implementer"),
            PaneConfig(role="reviewer", command="claudespace-reviewer"),
            PaneConfig(role="planner", command="claudespace-planner"),
            PaneConfig(role="researcher", command="claudespace-researcher"),
        ),
    ),
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
        panes=(
            PaneConfig(role="conductor", command="claudespace-conductor"),
            PaneConfig(role="principal", command="claudespace-principal"),
            PaneConfig(role="implementer", command="claudespace-implementer"),
            PaneConfig(role="reviewer", command="claudespace-reviewer"),
            PaneConfig(role="planner", command="claudespace-planner"),
            PaneConfig(role="researcher", command="claudespace-researcher"),
        ),
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

NATIVE_TEMPLATE_TOML = '''[templates.native]
layout = "main_left_grid_right"

[[templates.native.panes]]
role = "principal"
command = "claudespace-principal"

[[templates.native.panes]]
role = "implementer"
command = "claudespace-implementer"

[[templates.native.panes]]
role = "reviewer"
command = "claudespace-reviewer"

[[templates.native.panes]]
role = "planner"
command = "claudespace-planner"

[[templates.native.panes]]
role = "researcher"
command = "claudespace-researcher"
'''

AGENTIC_TEMPLATE_TOML = '''[templates.agentic]
layout = "conductor_main_left_grid_right"
entry_role = "conductor"

[[templates.agentic.panes]]
role = "conductor"
command = "claudespace-conductor"

[[templates.agentic.panes]]
role = "principal"
command = "claudespace-principal"

[[templates.agentic.panes]]
role = "implementer"
command = "claudespace-implementer"

[[templates.agentic.panes]]
role = "reviewer"
command = "claudespace-reviewer"

[[templates.agentic.panes]]
role = "planner"
command = "claudespace-planner"

[[templates.agentic.panes]]
role = "researcher"
command = "claudespace-researcher"
'''


USER_TEMPLATES_PATH = Path.home() / ".config" / "claudespace" / "templates.toml"

# Console-script names used to be colon-separated (``claudespace:researcher``),
# which uv's wheel installer misparses (it splits on the first colon and
# rejects the entry point). Renamed to dashes, but anyone who ran
# ensure_native_template_seeded()/ensure_agentic_template_seeded() before
# that rename has the old names permanently baked into their
# templates.toml - those seeders only write when a template is *missing*,
# so a plain reinstall/update never touches an already-seeded file. This
# maps each stale name to its replacement so migrate_legacy_command_names()
# can repair existing files in place.
_LEGACY_COMMAND_RENAMES = {
    "claudespace:principal": "claudespace-principal",
    "claudespace:planner": "claudespace-planner",
    "claudespace:implementer": "claudespace-implementer",
    "claudespace:reviewer": "claudespace-reviewer",
    "claudespace:researcher": "claudespace-researcher",
    "claudespace:conductor": "claudespace-conductor",
    "claudespace:sync-assets": "claudespace-sync-assets",
    "claudespace:handoff": "claudespace-handoff",
    "claudespace:update": "claudespace-update",
}


def migrate_legacy_command_names(path: Path = USER_TEMPLATES_PATH) -> bool:
    """Rewrite any old colon-named claudespace commands in ``templates.toml``.

    Called by ``sync_assets()`` on every install/update, before the
    native/agentic seeders. Does a plain string substitution rather than a
    parse/rewrite round-trip through ``tomllib`` (which has no writer and
    would require a TOML-writing dependency) - safe here because the old
    names are a fixed, known set of literal strings that only ever appear
    as ``command = "claudespace:<role>"`` values, never as substrings of
    anything a user would plausibly write themselves.

    Returns ``True`` if the file was modified.
    """
    if not path.exists():
        return False

    existing = path.read_text()
    updated = existing
    for old, new in _LEGACY_COMMAND_RENAMES.items():
        updated = updated.replace(f'"{old}"', f'"{new}"')

    if updated == existing:
        return False

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
        command = "claudespace-principal"

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
