"""Task selection fence enforced by a rule through its condition helper (#25621).

A synthetic rule carries the fence so the tests pin the helper contract and the
engine path, never a bundled rule's name. Bundled YAML is validated generically.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
RULE_NAME = "synthetic-selection-fence"
CALL_KEY = "claude:toolu_running"
WHEN = """
event.data.get('tool_name') in ('call_tool', 'mcp__gobby__call_tool')
and tool_input.get('server_name') == 'gobby-tasks'
and (
  (tool_input.get('tool_name') == 'claim_task'
   and pending_selection_fence_calls(tool_input.get('task_id')))
  or (tool_input.get('tool_name') == 'create_task'
      and str(tool_input.get('claim')).lower() == 'true'
      and pending_selection_fence_calls(None))
)
"""
REASON = (
    "Synthetic fence: {{ pending_selection_fence_calls("
    "tool_input.get('task_id') if tool_input.get('tool_name') == 'claim_task' else None"
    ") | join(', ') }}"
)


@pytest.fixture
def tasks(temp_db: HubDatabase, sample_project: dict[str, Any]) -> tuple[Task, Task]:
    RuleDefinitionManager(temp_db).create(
        RULE_NAME,
        {
            "event": "before_tool",
            "when": WHEN,
            "effects": [{"type": "block", "reason": REASON}],
        },
        tags=["default"],
    )
    manager = LocalTaskManager(temp_db)
    return (
        manager.create_task(sample_project["id"], "Bound task"),
        manager.create_task(sample_project["id"], "Target task"),
    )


def _binding(task_id: str) -> dict[str, Any]:
    call: dict[str, Any] = {
        "started_at": 1.0,
        "task_id": task_id,
        "tool_name": "Bash",
        "pending": True,
        "background": False,
        "fences_switch": True,
        "anonymous": False,
        "source": "claude",
    }
    return {"task_tool_bindings": {CALL_KEY: call}, "active_task_id": task_id}


def _event(project_id: str, tool_name: str, arguments: dict[str, Any]) -> HookEvent:
    data: dict[str, Any] = {
        "tool_name": "mcp__gobby__call_tool",
        "tool_input": {
            "server_name": "gobby-tasks",
            "tool_name": tool_name,
            "arguments": arguments,
        },
    }
    normalize_tool_fields(data)
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={},
        project_id=project_id,
    )


async def _evaluate(db: HubDatabase, event: HookEvent, variables: dict[str, Any]) -> HookResponse:
    engine = RuleEngine(db, task_manager=LocalTaskManager(db))
    return await engine.evaluate(event, session_id=SESSION_ID, variables=variables)


def _ref(task: Task) -> str:
    return f"#{task.seq_num}"


@pytest.mark.asyncio
@pytest.mark.parametrize("by_uuid", [False, True], ids=["seq-ref", "uuid"])
async def test_pending_fencing_call_for_another_task_blocks_claim(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tasks: tuple[Task, Task],
    by_uuid: bool,
) -> None:
    bound, target = tasks
    task_ref = target.id if by_uuid else _ref(target)
    response = await _evaluate(
        temp_db,
        _event(sample_project["id"], "claim_task", {"task_id": task_ref}),
        _binding(bound.id),
    )
    assert response.decision == "block"
    assert f"Synthetic fence: {CALL_KEY}" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["same-task", "already-active", "non-fencing", "completed", "unbound"],
)
async def test_claim_is_allowed_without_a_foreign_pending_fencing_call(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tasks: tuple[Task, Task],
    case: str,
) -> None:
    bound, target = tasks
    variables = _binding(bound.id)
    call = variables["task_tool_bindings"][CALL_KEY]
    claim_ref = _ref(target)
    if case == "same-task":
        claim_ref = _ref(bound)
    elif case == "already-active":
        variables["active_task_id"] = target.id
    elif case == "non-fencing":
        call["fences_switch"] = False
    elif case == "completed":
        call["pending"] = False
    elif case == "unbound":
        call["task_id"] = None
    response = await _evaluate(
        temp_db,
        _event(sample_project["id"], "claim_task", {"task_id": claim_ref}),
        variables,
    )
    assert response.decision == "allow", response.reason


@pytest.mark.asyncio
async def test_unresolvable_claim_target_fails_closed(
    temp_db: HubDatabase, sample_project: dict[str, Any], tasks: tuple[Task, Task]
) -> None:
    bound, _target = tasks
    response = await _evaluate(
        temp_db,
        _event(sample_project["id"], "claim_task", {"task_id": "#999999"}),
        _binding(bound.id),
    )
    assert response.decision == "block"
    assert CALL_KEY in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(("claim", "decision"), [(True, "block"), (False, "allow")])
async def test_create_with_claim_is_fenced_by_any_pending_fencing_call(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tasks: tuple[Task, Task],
    claim: bool,
    decision: str,
) -> None:
    bound, _target = tasks
    response = await _evaluate(
        temp_db,
        _event(sample_project["id"], "create_task", {"title": "New", "claim": claim}),
        _binding(bound.id),
    )
    assert response.decision == decision
    assert (CALL_KEY in (response.reason or "")) is claim


@pytest.mark.asyncio
async def test_disabled_rule_leaves_claim_unfenced(
    temp_db: HubDatabase, sample_project: dict[str, Any], tasks: tuple[Task, Task]
) -> None:
    bound, target = tasks
    rules = RuleDefinitionManager(temp_db)
    row = rules.get_by_name(RULE_NAME)
    assert row is not None
    rules.update(row.id, enabled=False)
    response = await _evaluate(
        temp_db,
        _event(sample_project["id"], "claim_task", {"task_id": _ref(target)}),
        _binding(bound.id),
    )
    assert response.decision == "allow", response.reason
