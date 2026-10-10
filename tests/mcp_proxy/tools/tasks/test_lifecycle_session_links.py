"""Lifecycle MCP callers persist their action history in isolated storage."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks import _lifecycle_status, _stage_review
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._session import create_session_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit
MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@dataclass
class LinkCaller:
    ctx: RegistryContext
    registry: InternalToolRegistry
    task: Task
    caller_id: str
    owner_id: str


@pytest.fixture
def caller(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[LinkCaller]:
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", MACHINE_ID)
    manager = LocalTaskManager(temp_db)
    ctx = RegistryContext(task_manager=manager)
    caller_session = ctx.session_manager.register(
        external_id="lifecycle-caller",
        machine_id=MACHINE_ID,
        source="cli",
        project_id=sample_project["id"],
    )
    owner_session = ctx.session_manager.register(
        external_id="lifecycle-owner",
        machine_id=MACHINE_ID,
        source="cli",
        project_id=sample_project["id"],
    )
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Lifecycle attribution",
        created_in_session_id=caller_session.id,
        claimed_by_session_id=owner_session.id,
        validation_criteria="Lifecycle action links are returned by get_task_sessions.",
    )
    # Exercise the MCP attribution boundary; lifecycle transitions have separate tests.
    for name in ("reopen_task", "de_escalate_task", "reject_review", "record_plan_enhancement"):
        monkeypatch.setattr(manager, name, MagicMock(return_value=task))
    monkeypatch.setattr(_lifecycle_status, "require_claim_authority", lambda *_args: None)
    monkeypatch.setattr(_lifecycle_status, "projected_task_state", lambda *_args: "escalated")
    for module in (_lifecycle_status, _stage_review):
        monkeypatch.setattr(module, "clear_prior_claim_session_variables", MagicMock())
        monkeypatch.setattr(module, "notify_parent_on_task_state_change", MagicMock())
    for name in (
        "_auto_link_session_commits",
        "_release_current_agent_dispatch_mutex",
        "_schedule_signoff_relay",
        "schedule_dispatcher_tick",
    ):
        monkeypatch.setattr(_stage_review, name, MagicMock())
    monkeypatch.setattr(_stage_review, "_dispatch_run_kwargs", MagicMock(return_value={}))
    monkeypatch.setattr(_stage_review, "_operation_response", MagicMock(return_value={}))
    registry = InternalToolRegistry(name="lifecycle-links")
    _lifecycle_status.register_reopen_task(registry, ctx)
    _lifecycle_status.register_de_escalate_task(registry, ctx)
    _stage_review.register_review_stage_tools(registry, ctx)
    with session_context_for_test(caller_session.id):
        yield LinkCaller(ctx, registry, task, caller_session.id, owner_session.id)


CASES = [
    pytest.param("reopen_task", {}, "reopened", id="reopen"),
    pytest.param("de_escalate_task", {"reason": "Resolved"}, "de_escalated", id="de-escalate"),
    pytest.param("reject_review", {"stage_name": "implementation"}, "review_rejected", id="reject"),
    pytest.param(
        "record_plan_enhancement",
        {"round_number": 1, "converged": False, "suggestions": ["Clarify acceptance"]},
        "plan_enhanced",
        id="enhance-with-suggestions",
    ),
    pytest.param(
        "record_plan_enhancement",
        {"round_number": 1, "converged": True},
        "plan_enhanced",
        id="enhance-converged",
    ),
]


@pytest.mark.parametrize("tool,arguments,action", CASES)
@pytest.mark.asyncio
async def test_lifecycle_actions_are_returned_by_get_task_sessions(
    caller: LinkCaller,
    tool: str,
    arguments: dict[str, Any],
    action: str,
) -> None:
    result = await caller.registry.call(tool, {"task_id": caller.task.id, **arguments})
    assert "error" not in result
    sessions = await create_session_registry(caller.ctx).call(
        "get_task_sessions",
        {"task_id": caller.task.id},
    )
    expected_sessions = (
        {caller.caller_id, caller.owner_id}
        if tool == "reopen_task"
        else {
            caller.caller_id,
        }
    )
    assert {
        row["session_id"] for row in sessions["sessions"] if row["action"] == action
    } == expected_sessions


@pytest.mark.parametrize("tool,arguments,action", CASES)
@pytest.mark.asyncio
async def test_invalid_link_actions_are_visible_to_the_caller(
    caller: LinkCaller,
    tool: str,
    arguments: dict[str, Any],
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior_links = caller.ctx.session_task_manager.get_task_sessions(caller.task.id)
    monkeypatch.setattr(
        caller.ctx.session_task_manager,
        "VALID_ACTIONS",
        caller.ctx.session_task_manager.VALID_ACTIONS - {action},
    )
    if tool == "reopen_task":
        result = await caller.registry.call(tool, {"task_id": caller.task.id, **arguments})
        assert "Invalid action 'reopened'" in result["error"]
    else:
        with pytest.raises(ValueError, match="Invalid action"):
            await caller.registry.call(tool, {"task_id": caller.task.id, **arguments})
    assert caller.ctx.session_task_manager.get_task_sessions(caller.task.id) == prior_links


@pytest.mark.asyncio
async def test_reopen_caller_link_error_is_visible_after_owner_link(caller: LinkCaller) -> None:
    link_task = caller.ctx.session_task_manager.link_task

    def link_owner_only(session_id: str, task_id: str, action: str) -> None:
        if session_id == caller.caller_id:
            raise ValueError("Invalid action 'reopened'")
        link_task(session_id, task_id, action)

    with patch.object(caller.ctx.session_task_manager, "link_task", side_effect=link_owner_only):
        result = await caller.registry.call("reopen_task", {"task_id": caller.task.id})
    assert "Invalid action 'reopened'" in result["error"]
    rows = caller.ctx.session_task_manager.get_task_sessions(caller.task.id)
    assert {row["session_id"] for row in rows if row["action"] == "reopened"} == {caller.owner_id}


@pytest.mark.asyncio
async def test_database_link_failure_remains_best_effort(caller: LinkCaller) -> None:
    with patch.object(
        caller.ctx.session_task_manager,
        "link_task",
        side_effect=RuntimeError("storage unavailable"),
    ):
        result = await caller.registry.call("reopen_task", {"task_id": caller.task.id})
    assert result == {}
