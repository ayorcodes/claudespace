"""Which package manager installed claudespace: npm or pipx (D6).

A user can end up with both a pipx and an npm install on the same machine,
each with its own console scripts on PATH. ``doctor`` needs to warn about
that, and ``claudespace update`` needs to route to the right upgrade command
instead of unconditionally pipx-installing over an npm install. Both consume
this module rather than duplicating detection, so the two call sites can't
drift.
"""

from __future__ import annotations

import os
import sys

CHANNEL_MARKER_NAME = ".claudespace-channel"


def _marker_path() -> str:
    return os.path.join(sys.prefix, CHANNEL_MARKER_NAME)


def write_channel_marker(channel: str) -> None:
    """Record which channel provisioned the running venv (npm postinstall
    calls this; the pipx path leaves no marker, see ``installed_channel``).
    """
    with open(_marker_path(), "w") as f:
        f.write(channel)


def installed_channel() -> str:
    """``"npm"`` or ``"pipx"`` for the venv this process is running in.

    The marker file is the primary signal, written by npm's provisioner at
    install time. When it's absent (pre-existing pipx installs never write
    one, and always will not - the pipx path is left markerless by design),
    fall back to a path heuristic against ``sys.prefix``: pipx venvs live
    under ``.../pipx/venvs/...``, npm-provisioned venvs live under a
    ``node_modules`` package directory. An unrecognised layout defaults to
    ``"pipx"``, since every install that predates this module is pipx.
    """
    try:
        with open(_marker_path()) as f:
            marker = f.read().strip()
        if marker in ("npm", "pipx"):
            return marker
    except OSError:
        pass

    prefix = sys.prefix
    if f"{os.sep}pipx{os.sep}venvs{os.sep}" in prefix:
        return "pipx"
    if f"{os.sep}node_modules{os.sep}" in prefix:
        return "npm"
    return "pipx"


class Channel:
    """A detected install of claudespace: which channel, and where its
    ``claudespace`` binary resolves from.
    """

    def __init__(self, name: str, binary_path: str):
        self.name = name
        self.binary_path = binary_path

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Channel)
            and self.name == other.name
            and self.binary_path == other.binary_path
        )

    def __repr__(self) -> str:
        return f"Channel({self.name!r}, {self.binary_path!r})"


def competing_installs() -> list[Channel]:
    """Every distinct claudespace install found on PATH right now.

    Walks PATH looking for every ``claudespace`` executable, classifying
    each by the same path heuristic ``installed_channel`` uses (a pipx venv
    path vs. an npm ``node_modules`` path) - not the running process's own
    marker, since this has to report on binaries other than the one that's
    currently executing. Returns one ``Channel`` per distinct resolved path,
    in PATH order, so callers can report which one wins.
    """
    seen: set[str] = set()
    found: list[Channel] = []
    path_env = os.environ.get("PATH", "")
    for directory in path_env.split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, "claudespace")
        if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            continue
        try:
            resolved = os.path.realpath(candidate)
        except OSError:
            resolved = candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        if f"{os.sep}pipx{os.sep}venvs{os.sep}" in resolved:
            found.append(Channel("pipx", candidate))
        elif f"{os.sep}node_modules{os.sep}" in resolved:
            found.append(Channel("npm", candidate))
        else:
            found.append(Channel("unknown", candidate))
    return found
