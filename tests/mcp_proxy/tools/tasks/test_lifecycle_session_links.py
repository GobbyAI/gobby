"""Lifecycle MCP callers persist their action history in isolated storage."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks import (
    _crud,
    _escalation_coordinator,
    _lifecycle_claim,
    _lifecycle_close_finalization,
    _lifecycle_status,
    _session,
    _stage_review,
)
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.mcp_proxy.tools.tasks._session import create_session_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.session_context import session_context_for_test
from gobby.workflows import observers

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
    for name in (
        "reopen_task",
        "de_escalate_task",
        "submit_for_review",
        "approve_review",
        "reject_review",
        "record_plan_enhancement",
    ):
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
    monkeypatch.setattr(
        _stage_review,
        "evaluate_task_scope",
        MagicMock(return_value=SimpleNamespace(accepted=True, scope_justification=None)),
    )
    monkeypatch.setattr(ctx, "get_project_repo_path", MagicMock(return_value=None))
    registry = InternalToolRegistry(name="lifecycle-links")
    _lifecycle_status.register_reopen_task(registry, ctx)
    _lifecycle_status.register_de_escalate_task(registry, ctx)
    _stage_review.register_review_stage_tools(registry, ctx)
    with session_context_for_test(caller_session.id):
        yield LinkCaller(ctx, registry, task, caller_session.id, owner_session.id)


CASES = [
    pytest.param("reopen_task", {}, "reopened", id="reopen"),
    pytest.param("de_escalate_task", {"reason": "Resolved"}, "de_escalated", id="de-escalate"),
    pytest.param(
        "submit_for_review", {"stage_name": "implementation"}, "needs_review", id="submit"
    ),
    pytest.param(
        "approve_review", {"stage_name": "implementation"}, "review_approved", id="approve"
    ),
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


@pytest.mark.parametrize("action", ["worked_on", "unknown"])
async def test_explicit_link_validates_user_action(
    caller: LinkCaller, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    monkeypatch.setattr(_session, "require_claim_authority", lambda *_args: None)
    registry = create_session_registry(caller.ctx)
    before = caller.ctx.session_task_manager.get_task_sessions(caller.task.id)

    result = await registry.call(
        "link_task_to_session", {"task_id": caller.task.id, "action": action}
    )

    after = caller.ctx.session_task_manager.get_task_sessions(caller.task.id)
    if action == "unknown":
        assert "Invalid action 'unknown'" in result["error"]
        assert after == before
    else:
        assert result == {}
        assert any(
            row["session_id"] == caller.caller_id and row["action"] == action for row in after
        )


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


@pytest.mark.asyncio
async def test_reopen_session_resolution_failure_remains_best_effort(
    caller: LinkCaller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(caller.ctx, "resolve_session_id", MagicMock(side_effect=ValueError("gone")))
    result = await caller.registry.call("reopen_task", {"task_id": caller.task.id})
    assert result == {}
    rows = caller.ctx.session_task_manager.get_task_sessions(caller.task.id)
    assert {row["session_id"] for row in rows if row["action"] == "reopened"} == {caller.owner_id}


async def _invoke_other_link_caller(
    caller: LinkCaller,
    site: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = caller.ctx
    if site.startswith("create-"):
        monkeypatch.setattr(_crud, "targets_not_found_for_request", MagicMock(return_value=None))
        monkeypatch.setattr(
            _crud, "confirm_claiming_session_activity", MagicMock(return_value=True)
        )
        result = await _crud.create_crud_registry(ctx).call(
            "create_task",
            {
                "title": "Created link attribution",
                "category": "manual",
                "claim": True,
                "validation_criteria": "Session history records created and claimed links.",
            },
        )
        assert "error" not in result
    elif site == "claim":
        monkeypatch.setattr(
            _lifecycle_claim,
            "confirm_claiming_session_activity",
            MagicMock(return_value=True),
        )
        monkeypatch.setattr(_lifecycle_claim, "_claim_scope_conflicts", MagicMock(return_value=[]))
        _lifecycle_claim.register_claim_task(caller.registry, ctx)
        result = await caller.registry.call(
            "claim_task", {"task_id": caller.task.id, "force": True}
        )
        assert "error" not in result
    elif site == "escalate":
        _escalation_coordinator.link_escalating_session(ctx, caller.task.id, caller.caller_id)
    elif site == "close":
        evaluation = CloseEvaluation(
            requested_task_id=caller.task.id,
            task_id=caller.task.id,
            resolved_session_id=caller.caller_id,
        )
        _lifecycle_close_finalization._cleanup_closed_claim(ctx, evaluation, [])
    elif site == "observe-claim":
        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            source=SessionSource.CLAUDE,
            session_id=caller.owner_id,
            timestamp=datetime.now(UTC),
            data={
                "mcp_server": "gobby-tasks",
                "mcp_tool": "claim_task",
                "tool_input": {"arguments": {"task_id": caller.task.id}},
                "tool_output": {"success": True},
            },
        )
        observers.detect_task_claim(
            event,
            {},
            caller.owner_id,
            ctx.session_task_manager,
            ctx.task_manager,
            caller.task.project_id,
        )
    elif site == "preserve-claim":
        monkeypatch.setattr(observers, "_sessions_share_lineage", MagicMock(return_value=True))
        assert observers._preserve_lineage_claim(
            caller.task,
            caller.task.id,
            "#1",
            caller.owner_id,
            ctx.task_manager,
            ctx.session_manager,
            ctx.session_task_manager,
        )
    else:
        raise AssertionError(f"Unknown caller site {site}")


@pytest.mark.parametrize("invalid", [False, True], ids=["valid", "unknown-action"])
@pytest.mark.parametrize(
    "site,action",
    [
        ("create-created", "created"),
        ("create-claimed", "claimed"),
        ("claim", "claimed"),
        ("escalate", "escalated"),
        ("close", "closed"),
        ("observe-claim", "worked_on"),
        ("preserve-claim", "claimed"),
    ],
)
@pytest.mark.asyncio
async def test_other_callers_persist_valid_actions_and_expose_unknown_actions(
    caller: LinkCaller,
    site: str,
    action: str,
    invalid: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link_task = caller.ctx.session_task_manager.link_task
    attempted: list[tuple[str, str, str]] = []

    def link_with_action_fault(session_id: str, task_id: str, passed_action: str) -> None:
        attempted.append((session_id, task_id, passed_action))
        link_task(
            session_id, task_id, "unknown" if invalid and passed_action == action else passed_action
        )

    with patch.object(
        caller.ctx.session_task_manager, "link_task", side_effect=link_with_action_fault
    ):
        if invalid:
            with pytest.raises(ValueError, match="Invalid action 'unknown'"):
                await _invoke_other_link_caller(caller, site, monkeypatch)
        else:
            await _invoke_other_link_caller(caller, site, monkeypatch)
    matching_calls = [
        (sid, tid) for sid, tid, passed_action in attempted if passed_action == action
    ]
    assert matching_calls
    if not invalid:
        for sid, tid in matching_calls:
            rows = caller.ctx.session_task_manager.get_task_sessions(tid)
            assert any(row["session_id"] == sid and row["action"] == action for row in rows)
