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
    # opclaude is a claude-compatible CLI that routes to non-Anthropic
    # models via --model; panes call it directly rather than through a
    # claudespace: console-script.
    "opclaude": Template(
        layout="main_left_grid_right",
        panes=(
            PaneConfig(role="principal", command='opclaude --model "claude-ol-glm-5.2[1000K]"'),
            PaneConfig(role="implementer", command='opclaude --model "claude-ol-minimax-m3[512K]"'),
            PaneConfig(role="reviewer", command='opclaude --model "claude-ol-minimax-m3[512K]"'),
            PaneConfig(role="planner", command='opclaude --model "claude-ol-glm-5.2[1000K]"'),
            PaneConfig(role="researcher", command='opclaude --model "claude-ol-deepseek-v4-flash[1000K]"'),
        ),
    ),
}


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
