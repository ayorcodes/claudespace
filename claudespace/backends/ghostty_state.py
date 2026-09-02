"""File-backed workspace/pane state for the Ghostty backend (AD3).

iTerm2 stores all workspace state as session user-variables. Ghostty's
AppleScript surface has no equivalent - properties are read-only past
creation (see the design doc's "External API facts"). Instead, state is
persisted to one JSON file per workspace ``marker`` under the XDG state
dir, keyed further by ``instance`` so two windows opened against the same
resolved root stay distinguishable, mirroring iTerm2's ``INSTANCE_VAR``
identity model exactly.

Concurrency: Stop hooks in different panes can write concurrently (a
reveal inserting a ``roles`` entry while another pane stamps ``run_doc``).
Every write is a read-modify-write under an ``fcntl.flock`` on the file,
committed via temp-file + ``os.replace`` (atomic) - writes touch disjoint
keys, so the lock only has to prevent a lost read-modify-write, not
serialize unrelated updates.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, TypedDict

STATE_DIR = Path(
    os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
) / "claudespace" / "ghostty"


class InstanceState(TypedDict, total=False):
    auto_handoff: bool
    lazy: bool
    template: str
    run_doc: str
    run_started: float
    roles: dict[str, str]


class WorkspaceState(TypedDict, total=False):
    marker: str
    instances: dict[str, InstanceState]


def _state_path(marker: str) -> Path:
    digest = hashlib.sha1(marker.encode("utf-8")).hexdigest()
    return STATE_DIR / f"{digest}.json"


def _empty_state(marker: str) -> WorkspaceState:
    return {"marker": marker, "instances": {}}


def _read_locked(path: Path) -> WorkspaceState | None:
    """Read and parse ``path``, tolerating a missing or corrupt file by
    returning ``None`` rather than raising - a handoff must never crash
    because Ghostty's state file got truncated by a crash mid-write.
    """
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "instances" not in data:
        return None
    return data  # type: ignore[return-value]


def _write_atomic(path: Path, state: WorkspaceState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.remove(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _with_lock(marker: str, mutate) -> WorkspaceState:
    """Open (creating if needed) ``marker``'s state file, take an exclusive
    flock, hand the current state to ``mutate`` (which returns the new
    state), write it back atomically, and return it - all while the lock is
    held, so a concurrent writer can't interleave a read-modify-write.
    """
    path = _state_path(marker)
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_CREAT so a first-ever write has something to flock(); never
    # truncated here - the atomic replace above is the only writer of
    # content.
    lock_fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            current = _read_locked(path) or _empty_state(marker)
            updated = mutate(current)
            _write_atomic(path, updated)
            return updated
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def load(marker: str) -> WorkspaceState | None:
    """Read ``marker``'s state without locking (a plain read races a
    concurrent writer only in the sense that it might see the state either
    just before or just after one atomic write - never a torn file, since
    writes are atomic-replace). ``None`` if missing/corrupt/absent.
    """
    return _read_locked(_state_path(marker))


def get_instance(marker: str, instance: str) -> InstanceState | None:
    state = load(marker)
    if state is None:
        return None
    return state.get("instances", {}).get(instance)


def update_instance(marker: str, instance: str, **fields: Any) -> InstanceState:
    """Merge ``fields`` into ``instance``'s state, creating the instance
    entry (and the workspace file) if either is new. Returns the updated
    instance state.
    """

    def mutate(state: WorkspaceState) -> WorkspaceState:
        state.setdefault("marker", marker)
        instances = state.setdefault("instances", {})
        entry = dict(instances.get(instance, {}))
        entry.update(fields)
        instances[instance] = entry  # type: ignore[assignment]
        return state

    updated = _with_lock(marker, mutate)
    return updated["instances"][instance]


def set_role_pane(marker: str, instance: str, role: str, terminal_id: str) -> None:
    """Record ``role``'s pane id for ``instance``, merging into whatever
    ``roles`` map already exists rather than replacing it - concurrent
    reveals of different roles must not clobber each other.
    """

    def mutate(state: WorkspaceState) -> WorkspaceState:
        state.setdefault("marker", marker)
        instances = state.setdefault("instances", {})
        entry = dict(instances.get(instance, {}))
        roles = dict(entry.get("roles", {}))
        roles[role] = terminal_id
        entry["roles"] = roles
        instances[instance] = entry  # type: ignore[assignment]
        return state

    _with_lock(marker, mutate)


def prune_instance(marker: str, instance: str) -> None:
    """Drop ``instance``'s entry entirely - used when its terminal ids have
    all gone stale (Ghostty quit/reopened) and a fresh build is about to
    replace it.
    """

    def mutate(state: WorkspaceState) -> WorkspaceState:
        state.get("instances", {}).pop(instance, None)
        return state

    _with_lock(marker, mutate)


def find_instance_by_role_pane(marker: str, terminal_id: str) -> str | None:
    """Which instance (if any) owns a pane with this terminal id - used to
    recover ``instance`` when a caller only knows its own pane's id."""
    state = load(marker)
    if state is None:
        return None
    for instance, entry in state.get("instances", {}).items():
        if terminal_id in entry.get("roles", {}).values():
            return instance
    return None
