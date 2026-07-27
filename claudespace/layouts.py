"""Pane layout builders.

A layout takes the single session iTerm2 gives you in a fresh window and
splits it into the named panes a workspace config expects. Each layout
declares the exact set of roles it produces; ``workspace.py`` checks a
config's pane roles against this set before building, so a mismatched
config fails fast with a clear error instead of silently misplacing panes.

This is only used by eager (non-``--lazy``) workspaces, which build every
pane up front in one go. ``--lazy`` workspaces don't use a fixed layout at
all - splitting a fixed grid cell into existence unavoidably creates its
sibling cells too (there is no way to carve one rectangle out of a grid
without the others appearing alongside it), which defeats the point of
lazy mode (no empty panes). Instead a lazy workspace grows organically:
each newly revealed pane splits directly off of whichever pane handed off
to it. See ``iterm.py``'s ``reveal_role``.

To add a layout: describe its shape as a ``SplitNode`` tree and register it
in ``LAYOUTS`` below. Nothing else needs to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import iterm2


@dataclass(frozen=True, slots=True)
class SplitNode:
    """One node of a layout's binary split tree.

    A leaf names the ``role`` that occupies it. An internal node instead
    has ``first``/``second`` children, produced by splitting this node's
    session: ``first`` keeps the session that was split (the "old" pane,
    which shrinks but keeps its identity), ``second`` is the new session
    ``async_split_pane`` returns. ``vertical=True`` splits create a
    left/right divider (``first`` left, ``second`` right); ``vertical=False``
    splits create a top/bottom divider (``first`` top, ``second`` bottom).
    """

    role: str | None = None
    vertical: bool = True
    first: "SplitNode | None" = None
    second: "SplitNode | None" = None

    def __post_init__(self) -> None:
        is_leaf = self.role is not None
        has_children = self.first is not None and self.second is not None
        if is_leaf == has_children:
            raise ValueError(
                "SplitNode must have either 'role' or both children, not both/neither"
            )

    def roles(self) -> frozenset[str]:
        if self.role is not None:
            return frozenset({self.role})
        assert self.first is not None and self.second is not None
        return self.first.roles() | self.second.roles()


@dataclass(frozen=True, slots=True)
class Layout:
    """A registered layout: its split-tree shape."""

    tree: SplitNode

    @property
    def roles(self) -> frozenset[str]:
        return self.tree.roles()

    async def build(self, root: "iterm2.Session") -> dict[str, "iterm2.Session"]:
        """Materialize every role's pane, splitting the tree all at once."""
        sessions: dict[str, "iterm2.Session"] = {}

        async def visit(node: SplitNode, session: "iterm2.Session") -> None:
            if node.role is not None:
                sessions[node.role] = session
                return
            assert node.first is not None and node.second is not None
            second_session = await session.async_split_pane(vertical=node.vertical)
            await visit(node.first, session)
            await visit(node.second, second_session)

        await visit(self.tree, root)
        return sessions


# ┌────────────┬──────────────┬──────────────┐
# │            │ implementer  │ planner      │
# │  principal ├──────────────┼──────────────┤
# │            │ reviewer     │ researcher   │
# └────────────┴──────────────┴──────────────┘
_MAIN_LEFT_GRID_RIGHT = SplitNode(
    vertical=True,
    first=SplitNode(role="principal"),
    second=SplitNode(
        vertical=True,
        first=SplitNode(
            vertical=False,
            first=SplitNode(role="implementer"),
            second=SplitNode(role="reviewer"),
        ),
        second=SplitNode(
            vertical=False,
            first=SplitNode(role="planner"),
            second=SplitNode(role="researcher"),
        ),
    ),
)


LAYOUTS: dict[str, Layout] = {
    "main_left_grid_right": Layout(tree=_MAIN_LEFT_GRID_RIGHT),
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
