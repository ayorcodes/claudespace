"""Template definitions.

A ``Template`` is the *shape* of a workspace - a layout plus the command
each role runs. It knows nothing about any particular folder; ``workspace``
is always pointed at a folder via ``--root`` (defaulting to the current
directory), so the same template works from anywhere.

Add a new AI role or change what a role runs by editing a ``Template``
here - no launcher code needs to change.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    # claudespace:planner/claudespace:implementer/claudespace:reviewer/claudespace:memory are console-scripts
    # installed by this package (see roles.py), each pinned to its own
    # model; scratch is always plain claude sonnet.
    "native": Template(
        layout="main_left_grid_right",
        panes=(
            PaneConfig(role="planner", command="claudespace:planner"),
            PaneConfig(role="implementer", command="claudespace:implementer"),
            PaneConfig(role="reviewer", command="claudespace:reviewer"),
            PaneConfig(role="memory", command="claudespace:memory"),
            PaneConfig(role="scratch", command="claude --model sonnet"),
        ),
    ),
}


def get_template(name: str) -> Template:
    """Look up a registered template by name.

    Raises ``KeyError`` with the list of known template names if ``name``
    is not registered.
    """
    try:
        return TEMPLATES[name]
    except KeyError:
        known = ", ".join(sorted(TEMPLATES)) or "(none registered)"
        raise KeyError(f"Unknown template '{name}'. Known templates: {known}") from None
