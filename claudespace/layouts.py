"""Pane layout builders.

A layout takes the single session iTerm2 gives you in a fresh window and
splits it into the named panes a workspace config expects. Each layout
declares the exact set of roles it produces; ``workspace.py`` checks a
config's pane roles against this set before building, so a mismatched
config fails fast with a clear error instead of silently misplacing panes.

To add a layout: write an async function ``(root) -> dict[str, Session]``
and register it in ``LAYOUTS`` below. Nothing else needs to change.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import iterm2

LayoutBuilder = Callable[["iterm2.Session"], Awaitable[dict[str, "iterm2.Session"]]]


@dataclass(frozen=True, slots=True)
class Layout:
    """A registered layout: the roles it produces and how to build them."""

    roles: frozenset[str]
    build: LayoutBuilder


async def _main_left_grid_right(root: "iterm2.Session") -> dict[str, "iterm2.Session"]:
    """Layout matching the requested workspace shape.

    ┌────────────┬──────────────┬──────────────┐
    │            │ implementer  │ planner      │
    │  principal ├──────────────┼──────────────┤
    │            │ reviewer     │ researcher   │
    └────────────┴──────────────┴──────────────┘

    ``vertical=True`` splits create a left/right divider (side by side);
    ``vertical=False`` splits create a top/bottom divider (stacked).
    """
    principal = root
    middle_col = await principal.async_split_pane(vertical=True)
    right_col = await middle_col.async_split_pane(vertical=True)

    implementer = middle_col
    reviewer = await middle_col.async_split_pane(vertical=False)

    planner = right_col
    researcher = await right_col.async_split_pane(vertical=False)

    return {
        "principal": principal,
        "implementer": implementer,
        "reviewer": reviewer,
        "planner": planner,
        "researcher": researcher,
    }


LAYOUTS: dict[str, Layout] = {
    "main_left_grid_right": Layout(
        roles=frozenset(
            {"principal", "implementer", "reviewer", "planner", "researcher"}
        ),
        build=_main_left_grid_right,
    ),
}


def get_layout(name: str) -> Layout:
    """Look up a registered layout by name.

    Raises ``KeyError`` with the list of known layout names if ``name`` is
    not registered.
    """
    try:
        return LAYOUTS[name]
    except KeyError:
        known = ", ".join(sorted(LAYOUTS)) or "(none registered)"
        raise KeyError(f"Unknown layout '{name}'. Known layouts: {known}") from None
