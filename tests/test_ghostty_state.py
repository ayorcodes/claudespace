"""``backends/ghostty_state.py`` (AD3): file-backed state store round-trip,
instance isolation, concurrent-writer safety, and corrupt/missing-file
tolerance.
"""

from __future__ import annotations

import json
import threading

import pytest

from claudespace.backends import ghostty_state


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ghostty_state, "STATE_DIR", tmp_path / "ghostty")
    return tmp_path / "ghostty"


def test_missing_file_yields_none():
    assert ghostty_state.load("/some/root") is None
    assert ghostty_state.get_instance("/some/root", "inst-1") is None


def test_update_instance_round_trips():
    ghostty_state.update_instance(
        "/root", "inst-1", auto_handoff=True, lazy=False, template="native"
    )
    entry = ghostty_state.get_instance("/root", "inst-1")
    assert entry == {"auto_handoff": True, "lazy": False, "template": "native"}


def test_update_instance_merges_rather_than_replaces():
    ghostty_state.update_instance("/root", "inst-1", auto_handoff=True)
    ghostty_state.update_instance("/root", "inst-1", lazy=True)
    entry = ghostty_state.get_instance("/root", "inst-1")
    assert entry == {"auto_handoff": True, "lazy": True}


def test_set_role_pane_merges_into_the_roles_map():
    ghostty_state.set_role_pane("/root", "inst-1", "researcher", "term-1")
    ghostty_state.set_role_pane("/root", "inst-1", "planner", "term-2")
    entry = ghostty_state.get_instance("/root", "inst-1")
    assert entry["roles"] == {"researcher": "term-1", "planner": "term-2"}


def test_instances_are_isolated_by_key():
    ghostty_state.update_instance("/root", "inst-1", template="native")
    ghostty_state.update_instance("/root", "inst-2", template="agentic")
    assert ghostty_state.get_instance("/root", "inst-1")["template"] == "native"
    assert ghostty_state.get_instance("/root", "inst-2")["template"] == "agentic"


def test_workspaces_are_isolated_by_marker():
    ghostty_state.update_instance("/root-a", "inst-1", template="native")
    ghostty_state.update_instance("/root-b", "inst-1", template="agentic")
    assert ghostty_state.get_instance("/root-a", "inst-1")["template"] == "native"
    assert ghostty_state.get_instance("/root-b", "inst-1")["template"] == "agentic"


def test_prune_instance_removes_the_entry():
    ghostty_state.update_instance("/root", "inst-1", template="native")
    ghostty_state.prune_instance("/root", "inst-1")
    assert ghostty_state.get_instance("/root", "inst-1") is None


def test_corrupt_file_is_tolerated_as_absent(_state_dir):
    path = ghostty_state._state_path("/root")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json")
    assert ghostty_state.load("/root") is None
    # A write after corruption starts fresh rather than raising.
    ghostty_state.update_instance("/root", "inst-1", template="native")
    assert ghostty_state.get_instance("/root", "inst-1")["template"] == "native"


def test_find_instance_by_role_pane():
    ghostty_state.set_role_pane("/root", "inst-1", "researcher", "term-1")
    assert ghostty_state.find_instance_by_role_pane("/root", "term-1") == "inst-1"
    assert ghostty_state.find_instance_by_role_pane("/root", "term-missing") is None


def test_concurrent_writers_to_disjoint_keys_lose_nothing():
    # Simulates two Stop hooks in different panes writing concurrently - a
    # reveal inserting a `roles` entry while another pane stamps `run_doc`.
    barrier = threading.Barrier(2)

    def _write_role(role: str, terminal_id: str) -> None:
        barrier.wait(timeout=5)
        for _ in range(20):
            ghostty_state.set_role_pane("/root", "inst-1", role, terminal_id)

    threads = [
        threading.Thread(target=_write_role, args=("researcher", "term-r")),
        threading.Thread(target=_write_role, args=("planner", "term-p")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    entry = ghostty_state.get_instance("/root", "inst-1")
    assert entry["roles"] == {"researcher": "term-r", "planner": "term-p"}


def test_state_file_is_valid_json_on_disk(_state_dir):
    ghostty_state.update_instance("/root", "inst-1", template="native")
    path = ghostty_state._state_path("/root")
    data = json.loads(path.read_text())
    assert data["marker"] == "/root"
    assert "inst-1" in data["instances"]
