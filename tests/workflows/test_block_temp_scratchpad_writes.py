"""Tests for block-temp-scratchpad-writes (#25218).

Every agent writes only under its assigned workspace folder or its task worktree.
Writes into provider scratchpads, $TMPDIR, /tmp, or /private/var/folders are blocked
for every definition that selects the default audience.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[2]
RULE = "block-temp-scratchpad-writes"
RULE_PATH = (
    ROOT / "src/gobby/install/shared/workflows/rules/roles/block-temp-scratchpad-writes.yaml"
)
SESSION_ID = "55555555-5555-4555-8555-555555555555"

PROJECT = "/Users/dev/.gobby/worktrees/gobby/task-25218-example"
WORKSPACE = (
    "/Users/dev/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/seat/session-1"
)
PROVIDER_SCRATCHPAD = (
    "/private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-chsh0qk7/"
    "claude-501/-Users-dev--gobby-worktrees-gobby-task-25218-example/"
    "1cadf6cb-8490-4857-8375-54a5f22e1411/scratchpad"
)

SEATS: list[str | None] = [
    "assistant",
    "orchestrator",
    "developer",
    "researcher",
    "test-writer",
    None,
]


def _seat_id(seat: str | None) -> str:
    return seat or "no-agent-type"


TEMP_WRITE_PATHS = [
    pytest.param("/tmp/gobby-scratch/notes.md", id="tmp"),
    pytest.param("/private/tmp/gobby-scratch/notes.md", id="private-tmp"),
    pytest.param(f"{tempfile.gettempdir()}/gobby-scratch/notes.md", id="current-tempdir"),
    pytest.param(f"{PROVIDER_SCRATCHPAD}/notes.md", id="provider-scratchpad"),
    pytest.param("/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/notes.md", id="var-folders"),
]

NON_TEMP_WRITE_PATHS = [
    pytest.param(f"{WORKSPACE}/notes.md", id="own-workspace-folder"),
    pytest.param(f"{PROJECT}/src/app.py", id="task-worktree-source"),
    pytest.param(f"{PROJECT}/.gobby/plans/notes.md", id="project-plans"),
    pytest.param(f"{PROJECT}/scratchpad/notes.md", id="marker-named-folder-in-worktree"),
]

TEMP_SHELL_WRITES = [
    pytest.param("echo hi > /tmp/gobby-scratch/out.txt", id="redirect-tmp"),
    pytest.param(f"printf hi > {PROVIDER_SCRATCHPAD}/out.txt", id="redirect-provider-scratchpad"),
    pytest.param("cp src/app.py /tmp/gobby-scratch/app.py", id="copy-into-tmp"),
]

NON_TEMP_SHELL_COMMANDS = [
    pytest.param("cat /tmp/gobby-scratch/out.txt", id="read-tmp"),
    pytest.param("echo hi > notes.md", id="redirect-worktree-relative"),
    pytest.param(f"echo hi > {WORKSPACE}/out.txt", id="redirect-own-workspace"),
]

ENGINE_SEATS = ["assistant", "orchestrator", "developer"]


def _rule() -> dict[str, Any]:
    assert RULE_PATH.exists(), f"missing bundled rule template {RULE_PATH.name}"
    rules = yaml.safe_load(RULE_PATH.read_text())["rules"]
    assert RULE in rules
    rule: dict[str, Any] = rules[RULE]
    return rule


def _variables(seat: str | None) -> dict[str, Any]:
    variables: dict[str, Any] = {"_agent_workspace_dir": WORKSPACE}
    if seat is not None:
        variables["_agent_type"] = seat
    return variables


def _normalized(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "cwd": PROJECT,
        "project_path": PROJECT,
    }
    normalize_tool_fields(data)
    return data


def _rule_fires(data: dict[str, Any], seat: str | None) -> bool:
    evaluator = SafeExpressionEvaluator(
        {
            "event": SimpleNamespace(data=data),
            "tool_input": data["tool_input"],
            "variables": _variables(seat),
        },
        build_condition_helpers(),
    )
    return bool(evaluator.evaluate(_rule()["when"]))


def _sync_bundled(db: HubDatabase) -> object:
    result = sync_bundled_rules(db, get_bundled_rules_path())
    db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    return result


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    return temp_db


async def _evaluate_claimed(
    db: HubDatabase, tool_name: str, tool_input: dict[str, Any], seat: str
) -> HookResponse:
    data = _normalized(tool_name, tool_input)
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data,
    )
    return await RuleEngine(db).evaluate(
        event,
        session_id=SESSION_ID,
        variables={
            "require_task_before_edit": True,
            "task_claimed": True,
            "plan_mode": False,
            **_variables(seat),
        },
    )


# --- Bundled template shape -------------------------------------------------


def test_template_is_default_tagged_enabled_and_names_the_workspace_folder() -> None:
    rule = _rule()
    tags = yaml.safe_load(RULE_PATH.read_text())["tags"]

    assert "default" in tags
    assert rule["enabled"] is True
    assert rule["event"] == "before_tool"
    assert [effect["type"] for effect in rule["effects"]] == ["block"]
    reason = rule["effects"][0]["reason"]
    assert "agent workspace folder" in reason
    assert "Agent Workspace section" in reason


@pytest.mark.parametrize("seat", SEATS, ids=_seat_id)
def test_template_fires_for_every_seat_without_keying_on_agent_type(seat: str | None) -> None:
    data = _normalized("Write", {"file_path": "/tmp/gobby-scratch/notes.md", "content": "x"})
    assert _rule_fires(data, seat) is True


# --- Selection: every agent definition and the hub-synced row ---------------


# --- Evaluator: Write and Edit into temp roots -------------------------------


@pytest.mark.parametrize("seat", SEATS, ids=_seat_id)
@pytest.mark.parametrize("path", TEMP_WRITE_PATHS)
def test_write_into_temp_root_is_blocked_for_every_seat(path: str, seat: str | None) -> None:
    data = _normalized("Write", {"file_path": path, "content": "x"})
    assert _rule_fires(data, seat) is True


@pytest.mark.parametrize("seat", SEATS, ids=_seat_id)
def test_edit_into_temp_root_is_blocked_for_every_seat(seat: str | None) -> None:
    data = _normalized(
        "Edit",
        {
            "file_path": f"{tempfile.gettempdir()}/gobby-scratch/notes.md",
            "old_string": "a",
            "new_string": "b",
        },
    )
    assert _rule_fires(data, seat) is True


def test_live_tempdir_is_a_temp_root_even_outside_static_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom = "/Users/dev/custom-tmp"
    monkeypatch.setenv("TMPDIR", custom)
    monkeypatch.setattr(tempfile, "tempdir", custom)

    data = _normalized("Write", {"file_path": f"{custom}/notes.md", "content": "x"})
    assert _rule_fires(data, "developer") is True


# --- Evaluator: writes that must still pass ----------------------------------


@pytest.mark.parametrize("seat", SEATS, ids=_seat_id)
@pytest.mark.parametrize("path", NON_TEMP_WRITE_PATHS)
def test_workspace_and_worktree_writes_are_not_blocked(path: str, seat: str | None) -> None:
    data = _normalized("Write", {"file_path": path, "content": "x"})
    assert _rule_fires(data, seat) is False


@pytest.mark.parametrize("seat", SEATS, ids=_seat_id)
def test_reading_a_temp_file_is_not_blocked(seat: str | None) -> None:
    data = _normalized("Read", {"file_path": "/tmp/gobby-scratch/notes.md"})
    assert _rule_fires(data, seat) is False


# --- Evaluator: shell writes -------------------------------------------------


@pytest.mark.parametrize("seat", SEATS, ids=_seat_id)
@pytest.mark.parametrize("command", TEMP_SHELL_WRITES)
def test_shell_write_into_temp_root_is_blocked_for_every_seat(
    command: str, seat: str | None
) -> None:
    data = _normalized("Bash", {"command": command})
    assert _rule_fires(data, seat) is True


@pytest.mark.parametrize("seat", SEATS, ids=_seat_id)
@pytest.mark.parametrize("command", NON_TEMP_SHELL_COMMANDS)
def test_shell_read_or_non_temp_write_is_not_blocked(command: str, seat: str | None) -> None:
    data = _normalized("Bash", {"command": command})
    assert _rule_fires(data, seat) is False


# --- Engine on the isolated hub: claimed task does not bypass the block -----


@pytest.mark.asyncio
@pytest.mark.parametrize("seat", ENGINE_SEATS)
async def test_engine_blocks_write_tool_into_tmp_for_a_claimed_task(
    db: HubDatabase, seat: str
) -> None:
    _sync_bundled(db)

    response = await _evaluate_claimed(
        db, "Write", {"file_path": "/tmp/gobby-scratch/notes.md", "content": "x"}, seat
    )

    assert response.decision == "block"
    assert f"[{RULE}]" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("seat", ENGINE_SEATS)
async def test_engine_blocks_shell_write_through_tmpdir_for_a_claimed_task(
    db: HubDatabase, seat: str
) -> None:
    _sync_bundled(db)

    response = await _evaluate_claimed(
        db,
        "Bash",
        {"command": 'echo hi > "$TMPDIR/gobby-scratch/out.txt"'},
        seat,
    )

    assert response.decision == "block"
    assert f"[{RULE}]" in (response.reason or "")


@pytest.mark.asyncio
async def test_engine_allows_write_into_the_assigned_workspace_for_a_claimed_task(
    db: HubDatabase,
) -> None:
    _sync_bundled(db)

    response = await _evaluate_claimed(
        db, "Write", {"file_path": f"{WORKSPACE}/notes.md", "content": "x"}, "assistant"
    )

    assert response.decision == "allow"
