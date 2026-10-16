"""A developer seat's step binding follows its live task claim (#23886)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.mcp_proxy.tools.internal import InternalRegistryManager
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._transitions import claim_task
from gobby.utils.project_context import reset_project_context, set_project_context
from gobby.utils.session_context import (
    reset_seeded_contexts,
    resolve_and_seed_contexts,
    session_context_for_test,
)
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.step_tool_input import inner_tool_input
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.observers import reconcile_claimed_tasks
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_context import get_active_step_workflow_context
from gobby.workflows.step_instances import AgentStepInstanceManager, build_step_instance
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.conftest import _init_git_repo

pytestmark = pytest.mark.unit

MACHINE_ID = "21000000-0000-4000-8000-000000000001"
COMMIT = "fed94b9482"
_DEVELOPER = (
    Path(__file__).resolve().parents[2] / "src/gobby/install/shared/workflows/agents/developer.yaml"
)


@dataclass(frozen=True)
class _Seats:
    db: HubDatabase
    former: str
    successor: str
    task_id: str
    ref: str


@pytest.fixture
def seats(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_Seats]:
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", MACHINE_ID)
    sessions = SessionManager(temp_db)
    former, successor = (
        sessions.register(
            external_id=name,
            machine_id=MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
        ).id
        for name in ("binding-former", "binding-successor")
    )
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        title="Moved claim",
        validation_criteria="The former owner stops implementing it.",
    )
    for session_id in (former, successor):
        SessionVariableManager(temp_db).merge_variables(session_id, {"is_spawned_agent": True})
    token = set_project_context(sample_project)
    try:
        yield _Seats(
            db=temp_db,
            former=former,
            successor=successor,
            task_id=task.id,
            ref=f"#{task.seq_num}",
        )
    finally:
        reset_project_context(token)


def _seat(seats: _Seats, session_id: str, step: str, *, bound: bool = True) -> None:
    """Persist a developer instance at ``step``, bound to the task when ``bound``."""
    body = AgentDefinitionBody.model_validate(yaml.safe_load(_DEVELOPER.read_text("utf-8")))
    assert body.step_workflow is not None
    variables = dict(body.step_workflow.variables)
    if bound:
        variables.update(
            task_claimed=True,
            assigned_task_id=seats.task_id,
            assigned_task_ref=seats.ref,
            skills_routed=True,
            required_skills_loaded=True,
            additional_skills_loaded=True,
        )
    instance = build_step_instance(
        body,
        session_id=session_id,
        step_workflow_id=None,
        variables=variables,
        current_step=step,
    )
    AgentStepInstanceManager(seats.db).save(instance)


def _current_step(db: HubDatabase, session_id: str) -> str:
    variables = SessionVariableManager(db).get_variables(session_id)
    context = get_active_step_workflow_context(db, session_id, variables)
    assert context is not None
    return context.current_step


def _instance_state(db: HubDatabase, session_id: str) -> tuple[str | None, dict[str, Any]]:
    instance = AgentStepInstanceManager(db).get_for_session(session_id)
    assert instance is not None
    return instance.current_step, instance.variables


def _after_tool(
    session_id: str,
    tool: str,
    arguments: dict[str, Any],
    output: dict[str, Any],
) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {"server_name": "gobby-tasks", "tool_name": tool, "arguments": arguments},
            "tool_output": {"success": True, "result": output},
        },
        metadata={},
    )


def _transfer(seats: _Seats) -> None:
    claim_task(seats.db, seats.task_id, seats.successor, expected_owner=seats.former)


def _force_transfer(seats: _Seats) -> None:
    claim_task(seats.db, seats.task_id, seats.successor, force=True)


def _release(seats: _Seats) -> None:
    LocalTaskManager(seats.db).release_task_claim(seats.task_id)


def _before_escalation(seats: _Seats, task_ref: str | None) -> HookEvent:
    event = _after_tool(
        seats.former, "escalate_task", {"task_id": task_ref, "reason": "Handed off"}, {}
    )
    return replace(event, event_type=HookEventType.BEFORE_TOOL)


@pytest.mark.parametrize("use_ref", [False, True])
async def test_submit_handoff_releases_binding_and_allows_next_claim(
    seats: _Seats, use_ref: bool
) -> None:
    manager = LocalTaskManager(seats.db)
    registry = create_task_registry(manager)
    claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, "submit")
    task_ref = seats.ref if use_ref else seats.task_id
    event = _before_escalation(seats, task_ref)
    engine = RuleEngine(seats.db)
    assert engine._check_step_tool_enforcement(event, seats.former, {}) is None

    with session_context_for_test(seats.former):
        result = await registry.call(
            "escalate_task", {"task_id": task_ref, "reason": "Handed off: reviewer LAND"}
        )
    assert result == {}
    await engine._process_step_after_tool(
        _after_tool(seats.former, "escalate_task", {"task_id": task_ref}, result),
        seats.former,
        {},
    )
    assert _current_step(seats.db, seats.former) == "claim"
    step, state = _instance_state(seats.db, seats.former)
    assert step == "claim"
    assert state["assigned_task_id"] is None and state["task_claimed"] is False
    handed_off = manager.get_task(seats.task_id)
    assert handed_off.is_escalated and handed_off.claimed_by_session_id is None
    next_task = manager.create_task(
        handed_off.project_id,
        "Next directed task",
        validation_criteria="The released developer can claim this task.",
    )
    claim_event = replace(
        _after_tool(seats.former, "claim_task", {"task_id": next_task.id}, {}),
        event_type=HookEventType.BEFORE_TOOL,
    )
    assert engine._check_step_tool_enforcement(claim_event, seats.former, {}) is None
    with session_context_for_test(seats.former):
        claimed = await registry.call("claim_task", {"task_id": next_task.id})
    assert claimed["success"] is True


@pytest.mark.parametrize("task_ref", ["#999999", "foreign-task", None])
def test_submit_refuses_handoff_of_an_unassigned_task(seats: _Seats, task_ref: str | None) -> None:
    claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, "submit")
    response = RuleEngine(seats.db)._check_step_tool_enforcement(
        _before_escalation(seats, task_ref), seats.former, {}
    )
    assert response is not None and response.decision == "block"
    assert "Hand off only the assigned task" in str(response.reason)
    assert _current_step(seats.db, seats.former) == "submit"


@pytest.mark.parametrize("argument_key", ["arguments", "args"])
@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("assigned", [False, True])
def test_submit_condition_uses_inner_task_despite_outer_shadow(
    seats: _Seats, argument_key: str, encoded: bool, assigned: bool
) -> None:
    claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, "submit")
    inner_ref = seats.task_id if assigned else "foreign-task"
    arguments = {"task_id": inner_ref, "reason": "Handed off"}
    event = _before_escalation(seats, inner_ref)
    event.data["tool_input"] = {
        "server_name": "gobby-tasks",
        "tool_name": "escalate_task",
        "task_id": "foreign-task" if assigned else seats.task_id,
        argument_key: json.dumps(arguments) if encoded else arguments,
    }
    response = RuleEngine(seats.db)._check_step_tool_enforcement(event, seats.former, {})
    if assigned:
        assert response is None
    else:
        assert response is not None and response.decision == "block"
        assert "Hand off only the assigned task" in str(response.reason)
    assert _current_step(seats.db, seats.former) == "submit"


@pytest.mark.parametrize("arguments", [None, "{broken", "[]", []])
def test_unreadable_inner_arguments_do_not_fall_back_to_outer(arguments: Any) -> None:
    assert inner_tool_input({"task_id": "allowed", "arguments": arguments}) == {}


def test_missing_proxy_arguments_do_not_use_outer_keys() -> None:
    assert (
        inner_tool_input(
            {"server_name": "gobby-tasks", "tool_name": "escalate_task", "task_id": "allowed"}
        )
        == {}
    )


def _task_proxy(seats: _Seats, *, full_hooks: bool = False) -> ToolProxyService:
    """Use real proxy dispatch with step enforcement as the workflow adapter."""
    engine = RuleEngine(seats.db)
    handler = WorkflowHookHandler(
        rule_engine=engine,
        task_manager=LocalTaskManager(seats.db),
        session_manager=SessionManager(seats.db),
        config=SimpleNamespace(workflow=SimpleNamespace(enabled=True, timeout=5.0)),
    )

    def evaluate(event: HookEvent) -> HookResponse:
        if full_hooks:
            event.metadata["_platform_session_id"] = seats.former
            return asyncio.run(handler._evaluate_rules(event))
        if event.event_type == HookEventType.BEFORE_TOOL:
            return engine._check_step_tool_enforcement(event, seats.former, {}) or HookResponse(
                decision="allow"
            )
        asyncio.run(engine._process_step_after_tool(event, seats.former, {}))
        return HookResponse(decision="allow")

    registries = InternalRegistryManager()
    registries.add_registry(create_task_registry(LocalTaskManager(seats.db)))
    sessions = SessionManager(seats.db)
    hook_manager = SimpleNamespace(
        _session_manager=sessions,
        _workflow_handler=SimpleNamespace(evaluate=evaluate),
    )
    manager = cast(
        MCPClientManager,
        SimpleNamespace(session_manager=sessions, project_id=None),
    )
    return ToolProxyService(
        manager,
        internal_manager=registries,
        hook_manager_resolver=lambda: cast(HookManager, hook_manager),
    )


@pytest.mark.parametrize("foreign", [False, True])
async def test_submit_proxy_handoff_checks_resolved_project_task(
    seats: _Seats, foreign: bool, tmp_path: Path
) -> None:
    tasks = LocalTaskManager(seats.db)
    own_task = tasks.get_task(seats.task_id)
    LocalProjectCheckoutManager(seats.db).register(MACHINE_ID, own_task.project_id, str(tmp_path))
    other_project = LocalProjectManager(seats.db).create("shadow-project")
    other_task = tasks.create_task(
        other_project.id,
        "Same number in another project",
        validation_criteria="The developer cannot hand this task off.",
    )
    assert other_task.seq_num == own_task.seq_num
    claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, "submit")
    project_id = other_project.id if foreign else own_task.project_id
    arguments = {"task_id": seats.ref, "reason": "Handed off: reviewer LAND"}
    provider_event = _before_escalation(seats, seats.ref)
    provider_event.data["tool_input"]["project_id"] = project_id
    engine = RuleEngine(seats.db)
    response = engine._check_step_tool_enforcement(provider_event, seats.former, {})
    if foreign:
        assert response is not None and response.decision == "block"
    else:
        assert response is None

    # The real request seed honors explicit project context before proxy dispatch.
    tokens = await resolve_and_seed_contexts(
        seats.former, SessionManager(seats.db), project_ref=project_id, db=seats.db
    )
    try:
        result = await _task_proxy(seats).call_tool(
            "gobby-tasks",
            "escalate_task",
            arguments,
            session_id=seats.former,
            project_id=project_id,
            enforce_workflow=True,
        )
    finally:
        reset_seeded_contexts(tokens)
    if foreign:
        assert result["success"] is False
        assert result["error_code"] == "TOOL_BLOCKED"
        assert "Hand off only the assigned task" in result["error"]
        assert tasks.get_task(seats.task_id).claimed_by_session_id == seats.former
        assert _current_step(seats.db, seats.former) == "submit"
    else:
        assert result == {}
        assert tasks.get_task(seats.task_id).is_escalated
        assert tasks.get_task(seats.task_id).claimed_by_session_id is None
        assert _current_step(seats.db, seats.former) == "claim"
    assert not tasks.get_task(other_task.id).is_escalated


@pytest.mark.parametrize(
    ("agent", "step", "tool", "flag", "output", "next_step"),
    [
        ("epic-reviewer", "claim", "claim_task", "task_claimed", {}, "load_skill"),
        (
            "epic-reviewer",
            "claim",
            "get_task",
            "closed_epic",
            {"state": {"is_closed": True}},
            "load_skill",
        ),
        (
            "tech-writer",
            "implement",
            "close_task",
            "implementation_complete",
            {"closed": True},
            "terminate",
        ),
    ],
)
@pytest.mark.parametrize("assigned", [False, True])
async def test_dispatched_seat_handlers_match_resolved_assigned_task(
    seats: _Seats,
    agent: str,
    step: str,
    tool: str,
    flag: str,
    output: dict[str, Any],
    next_step: str,
    assigned: bool,
) -> None:
    body = AgentDefinitionBody.model_validate(
        yaml.safe_load(_DEVELOPER.with_name(f"{agent}.yaml").read_text("utf-8"))
    )
    assert body.step_workflow is not None
    variables = {
        **body.step_workflow.variables,
        "assigned_task_id": seats.ref,
        "assigned_task_uuid": seats.task_id,
    }
    instance = build_step_instance(
        body,
        session_id=seats.former,
        step_workflow_id=None,
        variables=variables,
        current_step=step,
    )
    AgentStepInstanceManager(seats.db).save(instance)
    foreign = LocalTaskManager(seats.db).create_task(
        LocalTaskManager(seats.db).get_task(seats.task_id).project_id,
        "Unassigned handler target",
        validation_criteria="It cannot advance the dispatched seat.",
    )
    task_ref = seats.ref if assigned else f"#{foreign.seq_num}"
    await RuleEngine(seats.db)._process_step_after_tool(
        _after_tool(seats.former, tool, {"task_id": task_ref}, output), seats.former, {}
    )
    current_step, state = _instance_state(seats.db, seats.former)
    assert state[flag] is assigned
    assert current_step == (next_step if assigned else step)


@pytest.mark.parametrize("use_ref", [False, True])
async def test_implement_already_implemented_close_reaches_checklist(
    seats: _Seats, tmp_path: Path, use_ref: bool
) -> None:
    """No candidate is needed to request a no-work close of the assigned claim."""
    task = LocalTaskManager(seats.db).get_task(seats.task_id)
    LocalProjectCheckoutManager(seats.db).register(MACHINE_ID, task.project_id, str(tmp_path))
    claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, "implement")
    before = _instance_state(seats.db, seats.former)
    assert before[1]["implementation_complete"] is False
    assert before[1]["_developer_handoff_tasks"] == []
    assert not LocalTaskManager(seats.db).get_task(seats.task_id).commits

    with session_context_for_test(seats.former):
        result = await _task_proxy(seats, full_hooks=True).call_tool(
            "gobby-tasks",
            "close_task",
            {
                "task_id": seats.ref if use_ref else seats.task_id,
                "reason": "already_implemented",
                "project_path": str(tmp_path),
                "preview": True,
                "response_detail": "diagnostic",
            },
            session_id=seats.former,
            enforce_workflow=True,
        )

    # The real checklist, rather than step enforcement, rejects missing evidence.
    assert result["preview"] is True
    assert result["can_close"] is False
    assert result["closed"] is False
    assert any(gate["status"] == "failed" for gate in result["checklist"])
    assert _instance_state(seats.db, seats.former) == before
    assert LocalTaskManager(seats.db).get_task(seats.task_id).closed_at is None


@pytest.mark.parametrize("use_ref", [False, True])
async def test_bounced_candidate_cannot_close_as_already_implemented(
    seats: _Seats, tmp_path: Path, use_ref: bool
) -> None:
    """A bounce after a sent candidate returns to rework, not the no-work close."""
    task = LocalTaskManager(seats.db).get_task(seats.task_id)
    LocalProjectCheckoutManager(seats.db).register(MACHINE_ID, task.project_id, str(tmp_path))
    claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, "implement")
    instance = AgentStepInstanceManager(seats.db).get_for_session(seats.former)
    assert instance is not None
    instance.variables.update(implementation_complete=True, candidate_sent=True, bounced=True)
    AgentStepInstanceManager(seats.db).save(instance)
    before = _instance_state(seats.db, seats.former)

    with session_context_for_test(seats.former):
        result = await _task_proxy(seats, full_hooks=True).call_tool(
            "gobby-tasks",
            "close_task",
            {
                "task_id": seats.ref if use_ref else seats.task_id,
                "reason": "already_implemented",
                "project_path": str(tmp_path),
                "preview": True,
            },
            session_id=seats.former,
            enforce_workflow=True,
        )

    assert result["error_code"] == "TOOL_BLOCKED"
    assert "A candidate was already sent for this task" in result["error"]
    assert _instance_state(seats.db, seats.former) == before
    assert LocalTaskManager(seats.db).get_task(seats.task_id).closed_at is None


@pytest.mark.parametrize("reviewed", [False, True])
@pytest.mark.parametrize("use_ref", [False, True])
async def test_implement_no_work_close_releases_binding(
    seats: _Seats, use_ref: bool, reviewed: bool
) -> None:
    """A successful close or its reviewed observation completes the claim loop."""
    claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, "implement")
    engine = RuleEngine(seats.db)
    task_ref = seats.ref if use_ref else seats.task_id
    if reviewed:
        await engine._process_step_after_tool(
            _after_tool(
                seats.former,
                "close_task",
                {"task_id": task_ref, "reason": "already_implemented"},
                {"closed": False, "error": "close_review_required"},
            ),
            seats.former,
            {},
        )
        assert _current_step(seats.db, seats.former) == "implement"
        event = _after_tool(
            seats.former,
            "get_task",
            {"task_id": task_ref},
            {"id": seats.task_id, "state": {"is_closed": True}},
        )
    else:
        event = _after_tool(
            seats.former,
            "close_task",
            {"task_id": task_ref, "reason": "already_implemented", "preview": False},
            {"closed": True},
        )
    # Feed the completion result through the real handler/transition engine.
    await engine._process_step_after_tool(event, seats.former, {})
    step, variables = _instance_state(seats.db, seats.former)
    assert step == "claim"
    for flag in (
        "task_claimed",
        "implementation_complete",
        "additional_skills_loaded",
        "skills_routed",
        "candidate_sent",
        "bounced",
    ):
        assert variables[flag] is False
    assert variables["assigned_task_id"] is None
    assert variables["assigned_task_ref"] is None


@pytest.mark.parametrize("use_ref", [False, True])
@pytest.mark.parametrize(
    ("assigned", "reason"),
    [
        (True, None),
        (True, "completed"),
        (True, "obsolete"),
        (False, None),
        (False, "completed"),
        (False, "obsolete"),
        (False, "already_implemented"),
    ],
)
async def test_implement_close_blocks_other_tasks_and_dispositions(
    seats: _Seats, tmp_path: Path, use_ref: bool, assigned: bool, reason: str | None
) -> None:
    tasks = LocalTaskManager(seats.db)
    claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, "implement")
    task = tasks.get_task(seats.task_id)
    LocalProjectCheckoutManager(seats.db).register(MACHINE_ID, task.project_id, str(tmp_path))
    if not assigned:
        task = tasks.create_task(
            task.project_id,
            "Unassigned close target",
            validation_criteria="The developer cannot close an unassigned task.",
        )
    arguments: dict[str, Any] = {"task_id": f"#{task.seq_num}" if use_ref else task.id}
    if reason is not None:
        arguments["reason"] = reason
    with session_context_for_test(seats.former):
        result = await _task_proxy(seats).call_tool(
            "gobby-tasks",
            "close_task",
            arguments,
            session_id=seats.former,
            enforce_workflow=True,
        )
    assert result["error_code"] == "TOOL_BLOCKED"
    assert "Finish only a task this developer handed off" in result["error"]
    assert tasks.get_task(task.id).closed_at is None


@pytest.mark.parametrize("has_commit", [False, True])
async def test_reclaimed_handoff_keeps_its_close_rights(seats: _Seats, has_commit: bool) -> None:
    manager = LocalTaskManager(seats.db)
    registry = create_task_registry(manager)
    claim_task(seats.db, seats.task_id, seats.former)
    if has_commit:
        manager.link_commit(seats.task_id, COMMIT)
    _seat(seats, seats.former, "submit")
    with session_context_for_test(seats.former):
        assert (
            await registry.call(
                "escalate_task", {"task_id": seats.ref, "reason": "Handed off: reviewer LAND"}
            )
            == {}
        )
    await RuleEngine(seats.db)._process_step_after_tool(
        _after_tool(
            seats.former,
            "escalate_task",
            {"task_id": seats.ref, "reason": "Handed off: reviewer LAND"},
            {},
        ),
        seats.former,
        {},
    )
    assert _current_step(seats.db, seats.former) == "claim"
    de_escalate_event = replace(
        _after_tool(seats.former, "de_escalate_task", {"task_id": seats.ref}, {}),
        event_type=HookEventType.BEFORE_TOOL,
    )
    assert (
        RuleEngine(seats.db)._check_step_tool_enforcement(de_escalate_event, seats.former, {})
        is None
    )
    with session_context_for_test(seats.former):
        assert (
            await registry.call(
                "de_escalate_task", {"task_id": seats.ref, "reason": "Landed and released"}
            )
            == {}
        )
        claimed = await registry.call("claim_task", {"task_id": seats.ref})
    assert claimed["success"] is True
    engine = RuleEngine(seats.db)
    await engine._process_step_after_tool(
        _after_tool(seats.former, "claim_task", {"task_id": seats.ref}, claimed), seats.former, {}
    )
    assert _current_step(seats.db, seats.former) == "route_skills"
    # Keep the live instance: rebuilding defaults would hide stale completion rights.
    instances = AgentStepInstanceManager(seats.db)
    instance = instances.get_for_session(seats.former)
    assert instance is not None
    assert instance.variables["_developer_handoff_tasks"] == [seats.task_id]
    instance.variables.update(skills_routed=True, additional_skills_loaded=True)
    instances.save(instance)
    await engine._process_step_after_tool(
        _after_tool(
            seats.former,
            "get_task",
            {"task_id": seats.ref},
            {"id": seats.task_id, "ref": seats.ref},
        ),
        seats.former,
        {},
    )
    # The pre-handoff commit belongs to the earlier claim; the reclaim links its own.
    assert _current_step(seats.db, seats.former) == "implement"
    if has_commit:
        await engine._process_step_after_tool(
            _after_tool(
                seats.former,
                "link_commit",
                {"task_id": seats.ref, "commit_sha": COMMIT},
                {"task_id": seats.task_id, "commits": [COMMIT]},
            ),
            seats.former,
            {},
        )
        assert _current_step(seats.db, seats.former) == "submit"
    close_event = _after_tool(seats.former, "close_task", {"task_id": seats.ref}, {})
    close_event = replace(close_event, event_type=HookEventType.BEFORE_TOOL)
    # The hand-off entry lasts until the close, so implement admits it too.
    assert engine._check_step_tool_enforcement(close_event, seats.former, {}) is None


@pytest.mark.parametrize("close_step", ["implement", "submit"])
async def test_reclaim_before_de_escalation_keeps_the_reviewed_close(
    seats: _Seats, tmp_path: Path, close_step: str
) -> None:
    """A seat reclaiming its handed-off task still de-escalates it and reaches review (#24610)."""
    tasks = LocalTaskManager(seats.db)
    project_id = tasks.get_task(seats.task_id).project_id
    _init_git_repo(tmp_path / "repo")
    repo = tmp_path / "repo"
    LocalProjectCheckoutManager(seats.db).register(MACHINE_ID, project_id, str(repo))
    claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, "submit")
    proxy = _task_proxy(seats, full_hooks=True)
    handler = WorkflowHookHandler(
        rule_engine=RuleEngine(seats.db),
        task_manager=tasks,
        session_manager=SessionManager(seats.db),
        config=SimpleNamespace(workflow=SimpleNamespace(enabled=True, timeout=5.0)),
    )

    async def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        with session_context_for_test(seats.former):
            result = await proxy.call_tool(
                "gobby-tasks", tool, arguments, session_id=seats.former, enforce_workflow=True
            )
        completion = _after_tool(seats.former, tool, arguments, result)
        completion.metadata["_platform_session_id"] = seats.former
        completion.cwd = str(repo)
        await handler._evaluate_rules(completion)
        return cast(dict[str, Any], result)

    handoff = {"task_id": seats.ref, "reason": "Handed off: reviewer LAND at abc"}
    assert await call("escalate_task", handoff) == {}
    assert _current_step(seats.db, seats.former) == "claim"
    claimed = await call("claim_task", {"task_id": seats.ref})
    assert claimed["task_id"] == seats.task_id
    assert tasks.get_task(seats.task_id).is_escalated
    instances = AgentStepInstanceManager(seats.db)
    instance = instances.get_for_session(seats.former)
    assert instance is not None
    assert instance.variables["_developer_handoff_tasks"] == [seats.task_id]
    instance.current_step = close_step
    instance.variables.update(implementation_complete=close_step == "submit")
    instances.save(instance)

    released = await call("de_escalate_task", {"task_id": seats.ref, "reason": "Landed"})
    assert "error_code" not in released, released
    assert not tasks.get_task(seats.task_id).is_escalated
    result = await call(
        "close_task", {"task_id": seats.ref, "changes_summary": "Landed and released."}
    )
    # Gates 1-12 pass and the ordinary Gate 13 review runs; this harness has no reviewer.
    assert result["blocking_reasons"] == [
        "close_review: The task-close criteria reviewer is not configured."
    ]
    assert tasks.get_task(seats.task_id).validation_override_reason is None


@pytest.mark.parametrize("step", ["claim", "implement", "submit"])
def test_developer_cannot_de_escalate_without_its_handoff(seats: _Seats, step: str) -> None:
    claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, step)
    event = replace(
        _after_tool(seats.former, "de_escalate_task", {"task_id": seats.ref}, {}),
        event_type=HookEventType.BEFORE_TOOL,
    )
    response = RuleEngine(seats.db)._check_step_tool_enforcement(event, seats.former, {})
    assert response is not None and response.decision == "block"
    assert "Finish only a task this developer handed off" in str(response.reason)


def test_developer_cannot_forge_handoff_permission(seats: _Seats) -> None:
    _seat(seats, seats.former, "implement")
    event = replace(
        _after_tool(seats.former, "set_variable", {}, {}),
        event_type=HookEventType.BEFORE_TOOL,
    )
    event.data.update(
        tool_name="mcp__gobby__set_variable",
        tool_input={"name": "_developer_handoff_tasks", "value": [seats.task_id], "scope": "step"},
    )
    response = RuleEngine(seats.db)._check_step_tool_enforcement(event, seats.former, {})
    assert response is not None and response.decision == "block"
    assert "managed by the step workflow runtime" in str(response.reason)


@pytest.mark.parametrize("use_ref", [False, True])
def test_claim_step_refuses_escalation(seats: _Seats, use_ref: bool) -> None:
    _seat(seats, seats.former, "claim", bound=False)
    event = _before_escalation(seats, seats.ref if use_ref else seats.task_id)
    response = RuleEngine(seats.db)._check_step_tool_enforcement(event, seats.former, {})
    assert response is not None and response.decision == "block"
    assert "escalate_task' is not allowed in the 'claim' step" in str(response.reason)


async def test_claim_step_can_wait_after_handoff_close_requires_review(seats: _Seats) -> None:
    _seat(seats, seats.former, "claim", bound=False)
    instances = AgentStepInstanceManager(seats.db)
    instance = instances.get_for_session(seats.former)
    assert instance is not None
    instance.variables["_developer_handoff_tasks"] = [seats.task_id]
    instances.save(instance)
    engine = RuleEngine(seats.db)
    reviewer_run_id = "31000000-0000-4000-8000-000000000001"
    close_completion = _after_tool(
        seats.former,
        "close_task",
        {"task_id": seats.ref},
        {
            "closed": False,
            "error": "close_review_required",
            "reviewer_run_id": reviewer_run_id,
        },
    )
    assert (
        engine._check_step_tool_enforcement(
            replace(close_completion, event_type=HookEventType.BEFORE_TOOL), seats.former, {}
        )
        is None
    )
    await engine._process_step_after_tool(close_completion, seats.former, {})
    assert _current_step(seats.db, seats.former) == "claim"
    wait_event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=seats.former,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-agents",
                "tool_name": "wait_for_agent",
                "arguments": {"run_id": reviewer_run_id},
            },
        },
        metadata={},
    )
    assert engine._check_step_tool_enforcement(wait_event, seats.former, {}) is None


@pytest.mark.parametrize("active_step", ["implement", "submit"])
@pytest.mark.parametrize("reviewer_closes", [False, True])
@pytest.mark.parametrize(
    "handoff_step", ["route_skills", "load_additional_skills", "implement", "submit"]
)
@pytest.mark.parametrize("use_ref", [False, True])
async def test_handoff_close_preserves_next_task_binding(
    seats: _Seats,
    tmp_path: Path,
    active_step: str,
    reviewer_closes: bool,
    handoff_step: str,
    use_ref: bool,
) -> None:
    """Exercise real MCP gates and close storage without a live criteria reviewer."""
    tasks = LocalTaskManager(seats.db)
    project_id = tasks.get_task(seats.task_id).project_id
    # A structural parent uses the actual close checklist without spawning a reviewer.
    handed_off = tasks.create_task(project_id, "Finished parent", task_type="epic")
    child = tasks.create_task(
        project_id,
        "Finished child",
        parent_task_id=handed_off.id,
        validation_criteria="The child is closed before its parent.",
    )
    seats = replace(seats, task_id=handed_off.id, ref=f"#{handed_off.seq_num}")
    LocalProjectCheckoutManager(seats.db).register(MACHINE_ID, project_id, str(tmp_path))
    claim_task(seats.db, seats.task_id, seats.former)
    tasks.close_task(child.id, reason="completed")
    _seat(seats, seats.former, handoff_step)
    proxy = _task_proxy(seats, full_hooks=True)
    engine = RuleEngine(seats.db)
    compact_handler = WorkflowHookHandler(
        rule_engine=engine,
        task_manager=tasks,
        session_manager=SessionManager(seats.db),
        config=SimpleNamespace(workflow=SimpleNamespace(enabled=True, timeout=5.0)),
    )

    async def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        with session_context_for_test(seats.former):
            result = await proxy.call_tool(
                "gobby-tasks", tool, arguments, session_id=seats.former, enforce_workflow=True
            )
        # Codex supplies provider completion hooks; the proxy does not synthesize them.
        completion = _after_tool(seats.former, tool, arguments, result)
        completion.metadata["_platform_session_id"] = seats.former
        completion.cwd = str(tmp_path)
        await compact_handler._evaluate_rules(completion)
        return cast(dict[str, Any], result)

    task_ref = seats.ref if use_ref else seats.task_id
    assert (
        await call(
            "escalate_task", {"task_id": task_ref, "reason": "Handed off: reviewer LAND at abc"}
        )
        == {}
    )
    assert _current_step(seats.db, seats.former) == "claim"
    await compact_handler._evaluate_rules(
        HookEvent(
            event_type=HookEventType.POST_COMPACT,
            session_id="provider-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            metadata={"_platform_session_id": seats.former},
            cwd=str(tmp_path),
        )
    )
    next_task = tasks.create_task(
        project_id, "Next directed task", validation_criteria="The active binding survives."
    )
    claimed = await call("claim_task", {"task_id": next_task.id})
    assert claimed == {
        "task_id": next_task.id,
        "ref": f"#{next_task.seq_num}",
        "title": "Next directed task",
    }
    assert _current_step(seats.db, seats.former) == "route_skills"
    assert await call("get_task", {"task_id": next_task.id})
    instance_manager = AgentStepInstanceManager(seats.db)
    instance = instance_manager.get_for_session(seats.former)
    assert instance is not None
    instance.current_step = active_step
    instance.variables.update(
        skills_routed=True,
        additional_skills_loaded=True,
        implementation_complete=active_step == "submit",
    )
    instance_manager.save(instance)
    before = _instance_state(seats.db, seats.former)
    session_before = SessionVariableManager(seats.db).get_variables(seats.former)

    foreign = await call("de_escalate_task", {"task_id": next_task.id, "reason": "Not handed off"})
    assert foreign["error_code"] == "TOOL_BLOCKED"
    assert "Finish only a task this developer handed off" in foreign["error"]
    # The Orchestrator may resolve escalation before this seat finishes the handoff.
    tasks.de_escalate_task(seats.task_id, reason="Externally landed and released")
    observed = await call("get_task", {"task_id": task_ref})
    assert observed["state"]["is_closed"] is False
    de_escalate_event = replace(
        _after_tool(seats.former, "de_escalate_task", {"task_id": task_ref}, {}),
        event_type=HookEventType.BEFORE_TOOL,
    )
    assert engine._check_step_tool_enforcement(de_escalate_event, seats.former, {}) is None
    assert _instance_state(seats.db, seats.former) == before
    assert not tasks.get_task(seats.task_id).is_escalated
    if reviewer_closes:
        # Automatic review closes out of band; the developer observes its verdict.
        tasks.close_task(seats.task_id, reason="completed")
        observed = await call("get_task", {"task_id": task_ref})
        assert observed["state"]["is_closed"] is True
    else:
        result = await call(
            "close_task", {"task_id": task_ref, "changes_summary": "All children finished."}
        )
        assert result["closed"] is True
    assert tasks.get_task(seats.task_id).closed_at is not None
    step_after, state_after = _instance_state(seats.db, seats.former)
    assert step_after == before[0]
    expected_state = {**before[1], "_developer_handoff_tasks": []}
    assert state_after == expected_state
    session_after = SessionVariableManager(seats.db).get_variables(seats.former)
    for key in ("task_claimed", "assigned_task_id", "claimed_tasks"):
        assert session_after.get(key) == session_before.get(key)
    assert tasks.get_task(next_task.id).claimed_by_session_id == seats.former


def test_lost_active_claim_keeps_previous_handoffs(seats: _Seats) -> None:
    """An unrelated transfer retires the active binding, not unfinished handoffs."""
    from gobby.workflows.step_context import get_active_step_workflow_context

    claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, "implement")
    tasks = LocalTaskManager(seats.db)
    handoff = tasks.create_task(
        tasks.get_task(seats.task_id).project_id,
        "Previously handed off",
        validation_criteria="The handoff is still pending.",
    )
    instances = AgentStepInstanceManager(seats.db)
    instance = instances.get_for_session(seats.former)
    assert instance is not None
    instance.variables["_developer_handoff_tasks"] = [handoff.id]
    instances.save(instance)
    _force_transfer(seats)

    context = get_active_step_workflow_context(seats.db, seats.former, {})

    assert context is not None and context.current_step == "claim"
    _, state = _instance_state(seats.db, seats.former)
    assert state["assigned_task_id"] is None and state["task_claimed"] is False
    assert state["_developer_handoff_tasks"] == [handoff.id]


@pytest.mark.parametrize("move", [_transfer, _force_transfer, _release])
async def test_moved_claim_returns_former_owner_to_claim_and_binds_successor(
    seats: _Seats,
    move: Callable[[_Seats], None],
) -> None:
    claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, "implement")
    _seat(seats, seats.successor, "claim", bound=False)
    assert _current_step(seats.db, seats.former) == "implement"

    move(seats)

    assert _current_step(seats.db, seats.former) == "claim"
    step, state = _instance_state(seats.db, seats.former)
    assert (step, state["assigned_task_id"], state["task_claimed"]) == ("claim", None, False)

    claim_task(seats.db, seats.task_id, seats.successor)
    await RuleEngine(seats.db)._process_step_after_tool(
        _after_tool(
            seats.successor,
            "claim_task",
            {"task_id": seats.ref},
            {"success": True, "task_id": seats.task_id},
        ),
        seats.successor,
        {},
    )
    step, state = _instance_state(seats.db, seats.successor)
    assert (step, state["assigned_task_id"]) == ("route_skills", seats.task_id)
    assert _current_step(seats.db, seats.successor) == "route_skills"


def test_closed_task_returns_owner_to_claim_once_its_claim_is_pruned(seats: _Seats) -> None:
    claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, "implement")
    LocalTaskManager(seats.db).close_task(seats.task_id, reason="completed", force=True)

    variables = SessionVariableManager(seats.db).get_variables(seats.former)
    reconcile_claimed_tasks(variables, seats.former, LocalTaskManager(seats.db))
    SessionVariableManager(seats.db).merge_variables(
        seats.former, {"claimed_tasks": variables["claimed_tasks"]}
    )

    assert _current_step(seats.db, seats.former) == "claim"


@pytest.mark.parametrize("closed_bound", [False, True])
async def test_active_claim_rebinds_parked_or_closed_step_task(
    seats: _Seats, closed_bound: bool, tmp_path: Path
) -> None:
    tasks = LocalTaskManager(seats.db)
    project_id = tasks.get_task(seats.task_id).project_id
    LocalProjectCheckoutManager(seats.db).register(MACHINE_ID, project_id, str(tmp_path))
    history = tasks.create_task(
        project_id, "Historical binding", validation_criteria="The historical task is closed."
    )
    tasks.close_task(history.id, reason="completed", force=True)
    active = tasks.create_task(
        project_id, "Active claimed task", validation_criteria="The active claim can be handed off."
    )
    active_ref = f"#{active.seq_num}"
    claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, "implement")
    if closed_bound:
        tasks.close_task(seats.task_id, reason="completed", force=True)
    claim_task(seats.db, active.id, seats.former)
    tasks.link_commit(active.id, COMMIT)
    variables = SessionVariableManager(seats.db)
    variables.merge_variables(
        seats.former,
        {
            "assigned_task_id": history.id,
            "assigned_task_ref": seats.ref,
            "claimed_tasks": {seats.task_id: seats.ref, active.id: active_ref},
            "active_task_id": active.id,
        },
    )
    instances = AgentStepInstanceManager(seats.db)
    instance = instances.get_for_session(seats.former)
    assert instance is not None
    instance.variables.update(bounced=True, _developer_handoff_tasks=[history.id])
    instances.save(instance)

    # Reconcile through the real BEFORE_TOOL hook, then use an ordinary idempotent claim.
    with session_context_for_test(seats.former):
        claimed = await _task_proxy(seats, full_hooks=True).call_tool(
            "gobby-tasks",
            "claim_task",
            {"task_id": active_ref},
            session_id=seats.former,
            enforce_workflow=True,
        )
    assert "error" not in claimed
    assert claimed["task_id"] == active.id
    assert _current_step(seats.db, seats.former) == "claim"
    # Codex supplies the provider completion hook for a non-agent-run session.
    engine = RuleEngine(seats.db)
    handler = WorkflowHookHandler(
        rule_engine=engine,
        task_manager=tasks,
        session_manager=SessionManager(seats.db),
        config=SimpleNamespace(workflow=SimpleNamespace(enabled=True, timeout=5.0)),
    )

    async def complete(event: HookEvent) -> None:
        event.metadata["_platform_session_id"] = seats.former
        event.cwd = str(tmp_path)
        event.project_id = project_id
        await handler._evaluate_rules(event)

    await complete(
        _after_tool(seats.former, "claim_task", {"task_id": active_ref}, claimed),
    )
    step, state = _instance_state(seats.db, seats.former)
    assert step == "route_skills"
    assert (state["assigned_task_id"], state["assigned_task_ref"]) == (active.id, active_ref)
    assert state["bounced"] is False
    assert state["_developer_handoff_tasks"] == [history.id]
    session_state = variables.get_variables(seats.former)
    assert (session_state["assigned_task_id"], session_state["assigned_task_ref"]) == (
        active.id,
        active_ref,
    )
    if closed_bound:
        assert seats.task_id not in session_state["claimed_tasks"]
    else:
        assert session_state["claimed_tasks"][seats.task_id] == seats.ref

    instance = instances.get_for_session(seats.former)
    assert instance is not None
    instance.variables.update(skills_routed=True, additional_skills_loaded=True)
    instances.save(instance)
    await complete(
        _after_tool(
            seats.former, "get_task", {"task_id": active_ref}, {"id": active.id, "ref": active_ref}
        ),
    )
    # A commit linked before this claim does not make the candidate; this claim's link does.
    assert _current_step(seats.db, seats.former) == "implement"
    await complete(
        _after_tool(
            seats.former,
            "link_commit",
            {"task_id": active_ref, "commit_sha": COMMIT},
            {"task_id": active.id, "commits": [COMMIT]},
        ),
    )
    assert _current_step(seats.db, seats.former) == "submit"
    _, state = _instance_state(seats.db, seats.former)
    assert (state["assigned_task_id"], state["assigned_task_ref"]) == (active.id, active_ref)
    session_state = variables.get_variables(seats.former)
    assert (session_state["assigned_task_id"], session_state["assigned_task_ref"]) == (
        active.id,
        active_ref,
    )
    close_event = replace(
        _after_tool(seats.former, "close_task", {"task_id": active_ref}, {}),
        event_type=HookEventType.BEFORE_TOOL,
    )
    assert engine._check_step_tool_enforcement(close_event, seats.former, {}) is None
    with session_context_for_test(seats.former):
        result = await _task_proxy(seats, full_hooks=True).call_tool(
            "gobby-tasks",
            "escalate_task",
            {"task_id": active_ref, "reason": "Handed off: reviewer LAND at linked commit"},
            session_id=seats.former,
            enforce_workflow=True,
        )
    assert result == {}
    assert tasks.get_task(active.id).is_escalated
    await complete(
        _after_tool(
            seats.former,
            "escalate_task",
            {"task_id": active_ref, "reason": "Handed off: reviewer LAND at linked commit"},
            result,
        ),
    )
    assert _current_step(seats.db, seats.former) == "claim"
    if not closed_bound:
        assert tasks.get_task(seats.task_id).claimed_by_session_id == seats.former


@pytest.mark.parametrize("active_state", ["missing", "foreign", "closed", "same-ref"])
def test_unusable_or_equivalent_active_claim_keeps_binding(
    seats: _Seats, active_state: str
) -> None:
    tasks = LocalTaskManager(seats.db)
    claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, "implement")
    variables = SessionVariableManager(seats.db)
    if active_state == "same-ref":
        instances = AgentStepInstanceManager(seats.db)
        instance = instances.get_for_session(seats.former)
        assert instance is not None
        instance.variables["assigned_task_id"] = seats.ref
        instances.save(instance)
    else:
        other = tasks.create_task(
            tasks.get_task(seats.task_id).project_id,
            "Unusable target",
            validation_criteria="The unusable target leaves the binding intact.",
        )
        if active_state == "foreign":
            claim_task(seats.db, other.id, seats.successor)
        elif active_state == "closed":
            tasks.close_task(other.id, reason="completed", force=True)
        projection = {seats.task_id: seats.ref}
        if active_state != "missing":
            projection[other.id] = f"#{other.seq_num}"
        variables.merge_variables(
            seats.former, {"claimed_tasks": projection, "active_task_id": other.id}
        )

    assert _current_step(seats.db, seats.former) == "implement"


class _CountingDb:
    """Count every database entry point the step read can use."""

    _COUNTED = frozenset(
        {"execute", "fetchone", "fetchall", "fetchval", "transaction", "transaction_immediate"}
    )

    def __init__(self, db: HubDatabase) -> None:
        self._db = db
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._db, name)
        if name not in self._COUNTED:
            return attr

        def counted(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return attr(*args, **kwargs)

        return counted


@pytest.mark.parametrize(("step", "bound"), [("implement", True), ("claim", False)])
def test_held_or_unbound_step_adds_no_query(seats: _Seats, step: str, bound: bool) -> None:
    if bound:
        claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, step, bound=bound)
    variables = SessionVariableManager(seats.db).get_variables(seats.former)
    before = AgentStepInstanceManager(seats.db).get_for_session(seats.former)
    assert before is not None

    plain = _CountingDb(seats.db)
    get_active_step_workflow_context(cast(HubDatabase, plain), seats.former)
    gated = _CountingDb(seats.db)
    context = get_active_step_workflow_context(cast(HubDatabase, gated), seats.former, variables)

    assert context is not None
    assert (context.current_step, gated.calls) == (step, plain.calls)
    after = AgentStepInstanceManager(seats.db).get_for_session(seats.former)
    assert after is not None
    assert after.updated_at == before.updated_at


async def test_commit_linked_under_an_earlier_claim_does_not_reach_submit(seats: _Seats) -> None:
    claim_task(seats.db, seats.task_id, seats.former)
    LocalTaskManager(seats.db).link_commit(seats.task_id, COMMIT)
    _force_transfer(seats)
    _seat(seats, seats.successor, "route_skills")

    notice = await RuleEngine(seats.db)._process_step_after_tool(
        _after_tool(
            seats.successor,
            "get_task",
            {"task_id": seats.task_id},
            {"id": seats.task_id, "ref": seats.ref},
        ),
        seats.successor,
        {},
    )

    assert notice is not None
    assert notice.startswith("Step transition: route_skills -> load_additional_skills -> implement")
    step, state = _instance_state(seats.db, seats.successor)
    assert (step, state["implementation_complete"]) == ("implement", False)

    # Only this claim's own link makes the candidate.
    await RuleEngine(seats.db)._process_step_after_tool(
        _after_tool(
            seats.successor,
            "link_commit",
            {"task_id": seats.task_id, "commit_sha": COMMIT},
            {"task_id": seats.task_id, "commits": [COMMIT]},
        ),
        seats.successor,
        {},
    )
    step, state = _instance_state(seats.db, seats.successor)
    assert (step, state["implementation_complete"]) == ("submit", True)


async def test_bounce_back_to_implement_stays_in_implement(seats: _Seats) -> None:
    claim_task(seats.db, seats.task_id, seats.former)
    LocalTaskManager(seats.db).link_commit(seats.task_id, COMMIT)
    _seat(seats, seats.former, "submit")
    AgentStepInstanceManager(seats.db).merge_variables(
        seats.former, {"implementation_complete": True, "bounced": True}
    )

    notice = await RuleEngine(seats.db)._process_step_after_tool(
        HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id=seats.former,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__set_variable",
                "tool_input": {
                    "name": "bounced",
                    "value": True,
                    "session_id": seats.former,
                    "scope": "step",
                },
                "tool_output": {"success": True},
            },
            metadata={},
        ),
        seats.former,
        {},
    )

    assert notice is not None
    assert notice.startswith("Step transition: submit -> implement")
    assert _instance_state(seats.db, seats.former)[0] == "implement"


async def test_stop_gate_reports_claim_after_force_transfer(seats: _Seats) -> None:
    sync_bundled_rules(seats.db, get_bundled_rules_path())
    with seats.db.transaction() as conn:
        conn.execute("UPDATE rule_definitions SET source = 'installed', enabled = FALSE")
        conn.execute(
            "UPDATE rule_definitions SET enabled = TRUE WHERE name = %s",
            ("require-step-completion",),
        )
    handler = WorkflowHookHandler(
        rule_engine=RuleEngine(seats.db),
        session_manager=SessionManager(seats.db),
        config=SimpleNamespace(workflow=SimpleNamespace(enabled=True, timeout=5.0)),
    )
    claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, "implement")
    _force_transfer(seats)

    response = await handler._evaluate_rules(
        HookEvent(
            event_type=HookEventType.STOP,
            session_id="provider-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            metadata={"_platform_session_id": seats.former, "session_type": "terminal"},
        )
    )

    assert response.decision == "block"
    assert "Current step: claim." in str(response.reason)
