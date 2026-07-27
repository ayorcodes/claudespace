"""Per-role color themes.

Each role gets a distinct iTerm2 color scheme so a glance at a pane tells
you which role it is, even before reading its prompt. Every role's
*background* is its own dark, hue-tinted shade (dark enough to keep light
foreground text readable everywhere), and the *accent* colors (tab color,
cursor, selection, bold, link, bright-ANSI slots) pick up a brighter version
of the same hue. Foreground text color is the same light gray across all
roles, so contrast/readability doesn't vary pane to pane - only the hue does.

Colors are Nord-derived and chosen for role fit:

- principal (slate blue):   oversees everything - neutral, authoritative
- planner (amber):          ideation/planning - warm, energetic
- researcher (teal):        investigation - cool, calm
- implementer (green):      building - "go"/action color
- reviewer (rose):          critique - draws attention without alarm
"""

from __future__ import annotations

from dataclasses import dataclass

import iterm2

# Shared across every role so body text contrast is identical everywhere.
_FOREGROUND = iterm2.Color(229, 233, 240)  # Nord5, light gray
_SELECTED_TEXT = iterm2.Color(46, 52, 64)  # Nord0, dark - for contrast on selection


@dataclass(frozen=True, slots=True)
class RoleTheme:
    """A role's background and accent colors."""

    background: iterm2.Color
    accent: iterm2.Color
    accent_bright: iterm2.Color


ROLE_THEMES: dict[str, RoleTheme] = {
    "principal": RoleTheme(
        background=iterm2.Color(36, 44, 58),  # dark slate blue
        accent=iterm2.Color(94, 129, 172),  # Nord10
        accent_bright=iterm2.Color(129, 161, 193),  # Nord9
    ),
    "planner": RoleTheme(
        background=iterm2.Color(56, 42, 33),  # dark amber-brown
        accent=iterm2.Color(208, 135, 112),  # Nord12
        accent_bright=iterm2.Color(235, 203, 139),  # Nord13
    ),
    "researcher": RoleTheme(
        background=iterm2.Color(28, 46, 51),  # dark teal
        accent=iterm2.Color(136, 192, 208),  # Nord8
        accent_bright=iterm2.Color(143, 188, 187),  # Nord7
    ),
    "implementer": RoleTheme(
        background=iterm2.Color(34, 48, 36),  # dark green
        accent=iterm2.Color(163, 190, 140),  # Nord14
        accent_bright=iterm2.Color(180, 205, 158),
    ),
    "reviewer": RoleTheme(
        background=iterm2.Color(51, 32, 36),  # dark rose
        accent=iterm2.Color(191, 97, 106),  # Nord11
        accent_bright=iterm2.Color(180, 142, 173),  # Nord15
    ),
}


def build_role_profile(role: str) -> iterm2.LocalWriteOnlyProfile:
    """Build the write-only profile patch that themes ``role``'s pane.

    Applied via ``session.async_set_profile_properties`` at workspace build
    time. Unknown roles get no theming - callers should only call this for
    roles present in ``ROLE_THEMES``.
    """
    theme = ROLE_THEMES[role]
    profile = iterm2.LocalWriteOnlyProfile()

    profile.set_background_color(theme.background)
    profile.set_foreground_color(_FOREGROUND)
    profile.set_selected_text_color(_SELECTED_TEXT)
    profile.set_selection_color(theme.accent)
    profile.set_cursor_color(theme.accent)
    profile.set_cursor_text_color(theme.background)
    profile.set_bold_color(theme.accent_bright)
    profile.set_use_bold_color(True)
    profile.set_link_color(theme.accent_bright)

    profile.set_use_tab_color(True)
    profile.set_tab_color(theme.accent)

    # Bright ANSI blue/cyan slots carry the accent so text explicitly
    # colored by the shell/app (e.g. prompts, highlights) picks it up too.
    profile.set_ansi_12_color(theme.accent)
    profile.set_ansi_14_color(theme.accent_bright)

    return profile


def banner_command(role: str) -> str:
    """Shell snippet that prints a colored role banner before ``claude`` starts.

    Claude Code's TUI paints its own full-screen background once it starts,
    which hides the profile background color set by ``build_role_profile``
    almost entirely - only chrome like tab/cursor color survives. Printing
    this banner first gives each pane a role-identifying splash of color
    that's visible regardless of what the TUI does to the screen afterwards
    (it scrolls up into terminal history, out of the TUI's alt-screen).

    Uses a 24-bit truecolor background escape (``\\033[48;2;R;G;Bm``)
    supported by iTerm2, reset with ``\\033[0m``. Returns a ``printf``
    command suitable for splicing into the ``&&``-chained pane launch
    command in ``iterm.py``.
    """
    theme = ROLE_THEMES[role]
    r, g, b = theme.accent.red, theme.accent.green, theme.accent.blue
    label = f" {role.upper()} "
    return f"printf '\\033[48;2;{r};{g};{b}m\\033[30m\\033[1m{label}\\033[0m\\n'"
