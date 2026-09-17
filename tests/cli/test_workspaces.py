"""The workspaces, panes, and nodes CLI groups (plan gclient-workspaces 2.4).

Every command posts to the daemon's gobby-workspaces registry with the local CLI
token, so the tests answer with a fake daemon and read back the exact tool calls.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner, Result

from gobby.cli.workspaces import nodes, panes, workspaces

pytestmark = pytest.mark.unit

TOKEN = "workspaces-cli-local-token"
WORKSPACE = {
    "id": "11111111-1111-4111-8111-111111111111",
    "machine_id": "22222222-2222-4222-8222-222222222222",
    "ref": 1,
    "name": "agents",
    "focused_project_id": None,
    "focused_tab_id": None,
    "created_at": "2026-09-17T00:00:00+00:00",
    "updated_at": "2026-09-17T00:00:00+00:00",
}
TAB = {
    "id": "33333333-3333-4333-8333-333333333333",
    "workspace_id": WORKSPACE["id"],
    "ref": 1,
    "title": "work",
    "project_id": "44444444-4444-4444-8444-444444444444",
    "worktree_id": None,
    "position": 0,
    "focused_pane_id": None,
    "layout": {"kind": "pane", "pane_id": "55555555-5555-4555-8555-555555555555"},
    "created_at": "2026-09-17T00:00:00+00:00",
    "updated_at": "2026-09-17T00:00:00+00:00",
}
PANE = {
    "id": "55555555-5555-4555-8555-555555555555",
    "tab_id": TAB["id"],
    "ref": 3,
    "terminal_id": "66666666-6666-4666-8666-666666666666",
    "owns_terminal": True,
    "label": "main",
    "created_at": "2026-09-17T00:00:00+00:00",
    "updated_at": "2026-09-17T00:00:00+00:00",
}
NODE = {
    "id": WORKSPACE["machine_id"],
    "ref": 1,
    "hostname": "studio",
    "label": "studio",
    "owner_user_id": "77777777-7777-4777-8777-777777777777",
    "local": True,
}
SPLIT = {"success": True, "panes": [PANE], "tabs": [TAB]}
OTHER_TAB = {**TAB, "id": "77777777-7777-4777-8777-777777777777", "ref": 2, "title": "other"}


@dataclass(frozen=True)
class _Call:
    """One recorded tool post: the tool name, its arguments, and its headers."""

    tool: str
    arguments: dict[str, Any]
    headers: dict[str, str]


@dataclass
class _FakeDaemon:
    """Answers gobby-workspaces tool posts from a queue of payloads."""

    payloads: list[dict[str, Any]] = field(default_factory=list)
    calls: list[_Call] = field(default_factory=list)

    def post(self, url: str, **kwargs: Any) -> MagicMock:
        self.calls.append(
            _Call(url.rsplit("/", 1)[-1], dict(kwargs["json"]), dict(kwargs["headers"]))
        )
        payload = self.payloads.pop(0) if self.payloads else {"success": True}
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = payload
        return response

    @property
    def tools(self) -> list[str]:
        return [call.tool for call in self.calls]

    @property
    def arguments(self) -> list[dict[str, Any]]:
        return [call.arguments for call in self.calls]


@pytest.fixture
def daemon() -> Iterator[_FakeDaemon]:
    fake = _FakeDaemon()
    with (
        patch("gobby.cli.workspaces.httpx.post", side_effect=fake.post),
        patch(
            "gobby.cli.workspaces.daemon_auth_headers",
            return_value={"Authorization": f"Bearer {TOKEN}"},
        ),
    ):
        yield fake


def _run(group: Any, arguments: list[str]) -> Result:
    return CliRunner().invoke(group, arguments)


def test_panes_split_resolves_ref_node_and_direction(daemon: _FakeDaemon) -> None:
    daemon.payloads = [SPLIT, SPLIT]

    adopted = _run(
        panes, ["split", "w1:t1:p2", "--right", "--terminal", "term-9", "--node", "studio"]
    )

    assert adopted.exit_code == 0, adopted.output
    assert daemon.tools == ["split_pane"]
    assert daemon.arguments[0] == {
        "pane": "w1:t1:p2",
        "axis": "horizontal",
        "terminal_id": "term-9",
        "node": "studio",
    }
    assert f"p{PANE['ref']}" in adopted.output

    # A full ref names its own node, so --node never reaches the daemon.
    full_ref = _run(panes, ["split", "n2:w1:t1:p2", "--below", "--node", "n5"])

    assert full_ref.exit_code == 0, full_ref.output
    assert daemon.arguments[1] == {"pane": "n2:w1:t1:p2", "axis": "vertical"}

    missing = _run(panes, ["split", "w1:t1:p2"])
    both = _run(panes, ["split", "w1:t1:p2", "--right", "--below"])

    assert (missing.exit_code, both.exit_code) == (2, 2)
    assert "exactly one" in missing.output
    assert "exactly one" in both.output
    assert daemon.tools == ["split_pane", "split_pane"]


def test_panes_split_left_swaps_the_new_pane_into_place(daemon: _FakeDaemon) -> None:
    swapped_tab = {**TAB, "title": "swapped"}
    daemon.payloads = [SPLIT, {"success": True, "tab": swapped_tab}]

    result = _run(panes, ["split", "w1:t1:p2", "--left", "--json"])

    assert result.exit_code == 0, result.output
    assert daemon.tools == ["split_pane", "swap_panes"]
    assert daemon.arguments[0] == {"pane": "w1:t1:p2", "axis": "horizontal"}
    assert daemon.arguments[1] == {"pane": PANE["id"], "other": "w1:t1:p2"}
    # The printed payload carries the tab as the swap left it, not the pre-swap copy.
    assert json.loads(result.output)["tabs"] == [swapped_tab]


def test_panes_move_carries_the_tab_and_the_direction(daemon: _FakeDaemon) -> None:
    daemon.payloads = [{"success": True, "panes": [PANE], "tabs": [TAB]}]

    plain = _run(panes, ["move", "w1:t1:p2", "--tab", "w1:t2", "--node", "n3"])

    assert plain.exit_code == 0, plain.output
    assert daemon.arguments[0] == {"pane": "w1:t1:p2", "tab": "w1:t2", "node": "n3"}

    # The move renumbers a pane that changes tab (w1:t1:p2 becomes w1:t2:p3), so the
    # follow-up swap addresses the moved pane by the id the move returned.
    moved_pane = {**PANE, "ref": 3, "tab_id": OTHER_TAB["id"]}
    swapped_tab = {**OTHER_TAB, "title": "swapped"}
    daemon.payloads = [
        {"success": True, "panes": [moved_pane], "tabs": [TAB, OTHER_TAB]},
        {"success": True, "tab": swapped_tab},
    ]

    beside = _run(panes, ["move", "w1:t1:p2", "--tab", "w1:t2", "--above", "w1:t2:p1", "--json"])

    assert beside.exit_code == 0, beside.output
    assert daemon.tools == ["move_pane", "move_pane", "swap_panes"]
    assert daemon.arguments[1] == {
        "pane": "w1:t1:p2",
        "tab": "w1:t2",
        "beside": "w1:t2:p1",
        "axis": "vertical",
    }
    assert daemon.arguments[2] == {"pane": PANE["id"], "other": "w1:t2:p1"}
    assert json.loads(beside.output)["tabs"] == [TAB, swapped_tab]


def test_panes_move_refuses_two_directions(daemon: _FakeDaemon) -> None:
    result = _run(
        panes, ["move", "w1:t1:p2", "--tab", "w1:t2", "--above", "w1:t2:p1", "--right", "w1:t2:p3"]
    )

    assert result.exit_code == 2
    assert "one direction" in result.output
    assert daemon.calls == []


def test_panes_input_read_and_wait_address_the_pane(daemon: _FakeDaemon) -> None:
    daemon.payloads = [
        {"success": True, "idempotency_key": "k1", "indeterminate": False, "detail": None},
        {"success": True, "idempotency_key": "k2", "indeterminate": True, "detail": "busy"},
        {"success": True, "text": "build ok", "truncated": False, "dropped_bytes": None},
        {
            "success": True,
            "matched": True,
            "reason": "matched",
            "snapshot": {"text": "done", "truncated": False},
        },
    ]

    typed = _run(panes, ["send", "w1:t1:p2", "make", "--submit", "--idempotency-key", "k1"])
    keyed = _run(panes, ["send", "w1:t1:p2", "c-c", "--key", "--idempotency-key", "k2"])
    read = _run(panes, ["read", "w1:t1:p2", "--lines", "10"])
    waited = _run(panes, ["wait", "w1:t1:p2", "done$", "--timeout", "12"])

    assert [typed.exit_code, keyed.exit_code, read.exit_code, waited.exit_code] == [0, 0, 0, 0]
    assert daemon.tools == ["send_text", "send_pane_keys", "read_pane", "wait_for_pane_output"]
    assert daemon.arguments[0] == {
        "pane": "w1:t1:p2",
        "text": "make",
        "submit": True,
        "idempotency_key": "k1",
    }
    assert daemon.arguments[1] == {
        "pane": "w1:t1:p2",
        "keys": "c-c",
        "literal": False,
        "idempotency_key": "k2",
    }
    assert daemon.arguments[2] == {"pane": "w1:t1:p2", "lines": 10}
    assert daemon.arguments[3] == {"pane": "w1:t1:p2", "pattern": "done$", "timeout_seconds": 12.0}
    assert "indeterminate" in keyed.output
    assert "build ok" in read.output
    assert "matched" in waited.output
    assert "done" in waited.output

    conflicting = _run(panes, ["send", "w1:t1:p2", "enter", "--key", "--submit"])

    assert conflicting.exit_code == 2
    assert "--submit applies to text" in conflicting.output
    assert len(daemon.calls) == 4


def test_panes_close_swap_and_rename_use_their_tools(daemon: _FakeDaemon) -> None:
    daemon.payloads = [
        {"success": True, "removed_panes": [PANE], "tabs": [TAB]},
        {"success": True, "tab": TAB},
        {"success": True, "pane": PANE},
        {"success": True, "tab": TAB},
    ]

    closed = _run(panes, ["close", "w1:t1:p2"])
    swapped = _run(panes, ["swap", "w1:t1:p2", "w1:t1:p3", "--node", "n4"])
    labelled = _run(panes, ["rename", "w1:t1:p2", "main"])
    cleared = _run(panes, ["rename", "w1:t1"])

    assert [closed.exit_code, swapped.exit_code, labelled.exit_code, cleared.exit_code] == [
        0,
        0,
        0,
        0,
    ]
    assert daemon.tools == [
        "close_pane",
        "swap_panes",
        "rename_workspace_item",
        "rename_workspace_item",
    ]
    assert daemon.arguments[1] == {"pane": "w1:t1:p2", "other": "w1:t1:p3", "node": "n4"}
    assert daemon.arguments[2] == {"ref": "w1:t1:p2", "name": "main"}
    assert daemon.arguments[3] == {"ref": "w1:t1"}
    assert "main" in labelled.output


def test_workspaces_group_lists_shows_creates_and_deletes(daemon: _FakeDaemon) -> None:
    daemon.payloads = [
        {"success": True, "node_ref": 1, "workspaces": [WORKSPACE]},
        {
            "success": True,
            "workspace": {**WORKSPACE, "node_ref": 1},
            "tabs": [TAB],
            "panes": [PANE],
        },
        {"success": True, "workspace": WORKSPACE},
        {"success": True, "workspace": WORKSPACE},
    ]

    listed = _run(workspaces, ["list", "--node", "n2"])
    shown = _run(workspaces, ["show", "w1"])
    created = _run(workspaces, ["create", "agents", "--node", "studio"])
    deleted = _run(workspaces, ["delete", "n2:w1", "--node", "n5"])

    assert [listed.exit_code, shown.exit_code, created.exit_code, deleted.exit_code] == [0, 0, 0, 0]
    assert daemon.tools == [
        "list_workspaces",
        "get_workspace",
        "create_workspace",
        "close_workspace",
    ]
    assert daemon.arguments == [
        {"node": "n2"},
        {"workspace": "w1"},
        {"name": "agents", "node": "studio"},
        {"workspace": "n2:w1"},
    ]
    assert "agents" in listed.output
    assert f"t{TAB['ref']}" in shown.output
    assert f"p{PANE['ref']}" in shown.output
    assert "agents" in created.output
    assert "w1" in deleted.output


def test_nodes_list_marks_the_local_node(daemon: _FakeDaemon) -> None:
    daemon.payloads = [{"success": True, "nodes": [NODE, {**NODE, "ref": 2, "local": False}]}]

    result = _run(nodes, ["list"])

    assert result.exit_code == 0, result.output
    assert daemon.tools == ["list_nodes"]
    assert daemon.arguments[0] == {}
    assert "n1" in result.output
    assert "n2" in result.output
    assert "local" in result.output


def test_json_output_prints_the_daemon_payload(daemon: _FakeDaemon) -> None:
    payload = {"success": True, "nodes": [NODE]}
    daemon.payloads = [payload, {"success": True, "node_ref": 1, "workspaces": [WORKSPACE]}, SPLIT]

    listed = _run(nodes, ["list", "--json"])
    spaces = _run(workspaces, ["list", "--json"])
    split = _run(panes, ["split", "w1:t1:p2", "--right", "--json"])

    assert [listed.exit_code, spaces.exit_code, split.exit_code] == [0, 0, 0]
    assert json.loads(listed.output) == payload
    assert json.loads(spaces.output)["workspaces"] == [WORKSPACE]
    assert json.loads(split.output)["panes"] == [PANE]


def test_local_token_is_sent_and_never_echoed(daemon: _FakeDaemon) -> None:
    daemon.payloads = [{"success": True, "nodes": [NODE]}]

    result = _run(nodes, ["list", "--json"])

    assert result.exit_code == 0, result.output
    assert daemon.calls[0].headers == {"Authorization": f"Bearer {TOKEN}"}
    assert TOKEN not in result.output


def test_tool_failures_and_transport_errors_exit_nonzero(daemon: _FakeDaemon) -> None:
    daemon.payloads = [{"success": False, "error": "Pane w1:t1:p9 not found", "code": "not_found"}]

    refused = _run(panes, ["close", "w1:t1:p9"])

    assert refused.exit_code == 1
    assert "Pane w1:t1:p9 not found" in refused.output

    with patch("gobby.cli.workspaces.httpx.post", side_effect=httpx.ConnectError("refused")):
        offline = _run(nodes, ["list"])

    assert offline.exit_code == 1
    assert "Cannot connect to Gobby daemon" in offline.output
