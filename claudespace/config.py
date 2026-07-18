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
