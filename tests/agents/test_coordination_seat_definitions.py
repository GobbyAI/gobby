"""Behavior tests for the coordination seats.

Each seat here is synthetic and built inline or seeded for the test, so no test reads a
bundled definition. Prompt wording, field values, sync state and reference paths are not
pinned. Each test carries only the send target, admission or rule under test.
"""

from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.condition_helpers_sessions import send_message_target_allowed
from tests.fixtures.agent_definitions import seed_seat
from tests.workflows.test_seat_step_loops import SeatRun

pytestmark = pytest.mark.unit

EDIT = {"file_path": "src/gobby/example.py", "old_string": "a", "new_string": "b"}


def _seat(name: str, steps: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": name,
        "prompts": {"agent": f"Synthetic {name} seat."},
        "workflows": {"rule_selectors": {"include": [], "exclude": []}},
        "step_workflow": {"variables": {}, "steps": steps},
    }
    body.update(fields)
    return body


# A read-only coordination seat: the write tools are blocked and spawning is not allowlisted.
READ_ONLY_SEAT = _seat(
    "read-only-seat",
    [
        {
            "name": "consolidate",
            "allowed_tools": ["mcp__gobby__call_tool"],
            "allowed_mcp_tools": ["gobby-tasks:get_task", "gobby-agents:send_message"],
        }
    ],
    blocked_tools=["Edit", "Write"],
    blocked_mcp_tools=["gobby-agents:kill_agent"],
)

# An operator-facing seat with no write block: its edits are admitted.
OPERATOR_SEAT = _seat(
    "operator-seat",
    [{"name": "serve", "allowed_tools": ["Edit", "mcp__gobby__call_tool"]}],
)


def test_spawned_caller_reaches_only_the_targets_its_definition_names(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    seed_seat(temp_db, "synthetic-reporter", send_message_targets=["parent", "session"])
    sessions = SessionManager(temp_db)
    caller = sessions.register(
        external_id="synthetic-reporter-caller",
        machine_id=require_machine_id(),
        source="claude",
        project_id=str(sample_project["id"]),
        agent_depth=1,
    )
    assert send_message_target_allowed(sessions, caller.id, "synthetic-reporter", "session") is True
    assert send_message_target_allowed(sessions, caller.id, "synthetic-reporter", "global") is False


async def test_read_only_seat_refuses_write_tools_spawns_and_kills(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "read-only-seat", definition=READ_ONLY_SEAT)
    assert await run.admits("gobby-tasks", "get_task")
    assert await run.admits("gobby-agents", "send_message")
    assert not await run.admits("gobby-agents", "spawn_agent")
    assert not await run.admits("gobby-agents", "kill_agent")
    assert not await run.admits_native("Edit", EDIT)
    assert not await run.admits_native("Write", EDIT)


async def test_operator_seat_without_a_write_block_admits_edits(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "operator-seat", definition=OPERATOR_SEAT)
    assert await run.admits_native("Edit", EDIT)
