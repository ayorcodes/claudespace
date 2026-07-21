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
    """A reusable workspace shape: a layout plus each role's command."""

    layout: str
    panes: tuple[PaneConfig, ...]


DEFAULT_TEMPLATE = "native"

# Built-in templates that ship purely as Python fallbacks in case
# ``templates.toml`` is unreadable or hasn't been seeded yet. The
# authoritative copy of "native" lives in ``templates.toml`` once
# ``ensure_native_template_seeded`` has run (see below).
TEMPLATES: dict[str, Template] = {
    # claudespace:principal/claudespace:implementer/claudespace:reviewer/
    # claudespace:planner/claudespace:researcher are console-scripts
    # installed by this package (see roles.py), each pinned to its own
    # model and effort level.
    "native": Template(
        layout="main_left_grid_right",
        panes=(
            PaneConfig(role="principal", command="claudespace:principal"),
            PaneConfig(role="implementer", command="claudespace:implementer"),
            PaneConfig(role="reviewer", command="claudespace:reviewer"),
            PaneConfig(role="planner", command="claudespace:planner"),
            PaneConfig(role="researcher", command="claudespace:researcher"),
        ),
    ),
}

NATIVE_TEMPLATE_TOML = '''[templates.native]
layout = "main_left_grid_right"

[[templates.native.panes]]
role = "principal"
command = "claudespace:principal"

[[templates.native.panes]]
role = "implementer"
command = "claudespace:implementer"

[[templates.native.panes]]
role = "reviewer"
command = "claudespace:reviewer"

[[templates.native.panes]]
role = "planner"
command = "claudespace:planner"

[[templates.native.panes]]
role = "researcher"
command = "claudespace:researcher"
'''


USER_TEMPLATES_PATH = Path.home() / ".config" / "claudespace" / "templates.toml"


def load_user_templates(path: Path = USER_TEMPLATES_PATH) -> dict[str, Template]:
    """Load user-defined templates from a TOML file, if present.

    Expected format - one ``[templates.<name>]`` table per template, each
    with a ``layout`` (must match a name registered in ``layouts.py``) and
    a list of ``panes`` tables giving each pane's ``role`` and ``command``::

        [templates.my-template]
        layout = "main_left_grid_right"

        [[templates.my-template.panes]]
        role = "principal"
        command = "claudespace:principal"

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
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"Template '{name}' in '{path}' is missing a required field "
                f"(each template needs 'layout' and 'panes', each pane "
                f"needs 'role' and 'command'): {exc}"
            ) from exc
        templates[name] = Template(layout=layout, panes=panes)

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
