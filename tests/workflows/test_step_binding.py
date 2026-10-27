"""A developer seat's step binding follows its live task claim (#23886)."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

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
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.definitions import WorkflowStep, WorkflowTransition
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.step_tool_input import inner_tool_input
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.observers import reconcile_claimed_tasks
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_context import get_active_step_workflow_context
from gobby.workflows.step_instances import AgentStepInstanceManager, build_step_instance
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.conftest import _init_git_repo
from tests.fixtures.agent_definitions import synthetic_developer_body

pytestmark = pytest.mark.unit

MACHINE_ID = "21000000-0000-4000-8000-000000000001"
COMMIT = "fed94b9482"


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


def _persist(
    seats: _Seats,
    session_id: str,
    body: AgentDefinitionBody,
    step: str,
    *,
    bound: bool = True,
) -> None:
    """Persist ``body`` at ``step`` for a session, bound to the task when ``bound``."""
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


def _seat(seats: _Seats, session_id: str, step: str, *, bound: bool = True) -> None:
    """Persist the synthetic developer seat at ``step``, bound to the task when ``bound``."""
    _persist(seats, session_id, synthetic_developer_body(), step, bound=bound)


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


def _force_transfer(seats: _Seats) -> None:
    claim_task(seats.db, seats.task_id, seats.successor, force=True)


def _transfer(seats: _Seats) -> None:
    claim_task(seats.db, seats.task_id, seats.successor, expected_owner=seats.former)


def _release(seats: _Seats) -> None:
    LocalTaskManager(seats.db).release_task_claim(seats.task_id)


def _before_escalation(seats: _Seats, task_ref: str | None) -> HookEvent:
    event = _after_tool(
        seats.former, "escalate_task", {"task_id": task_ref, "reason": "Handed off"}, {}
    )
    return replace(event, event_type=HookEventType.BEFORE_TOOL)


def _step(body: AgentDefinitionBody, name: str) -> WorkflowStep:
    assert body.step_workflow is not None
    return next(step for step in body.step_workflow.steps if step.name == name)


_CLAIM_RESETS = (
    "candidate_sent",
    "bounced",
    "implementation_complete",
    "skills_routed",
    "additional_skills_loaded",
    "handed_off",
)


def _set(tool: str, variable: str, value: Any, when: str | None = None) -> dict[str, Any]:
    """One gobby-tasks set_variable success handler, optionally gated by ``when``."""
    entry: dict[str, Any] = {
        "server": "gobby-tasks",
        "tool": tool,
        "action": "set_variable",
        "variable": variable,
        "value": value,
    }
    if when is not None:
        entry["when"] = when
    return entry


def _claim_bind_entries() -> list[dict[str, Any]]:
    """A successful claim_task binds the task and resets its attempt flags, as the bundled claim."""
    bind = [
        _set(
            "claim_task",
            "assigned_task_id",
            "(tool_output.get('result') or tool_output).get('task_id')",
        ),
        _set(
            "claim_task",
            "assigned_task_ref",
            "(tool_output.get('result') or tool_output).get('ref')",
        ),
    ]
    return [*bind, *(_set("claim_task", variable, False) for variable in _CLAIM_RESETS)]


def _claim_routes_to_skills(body: AgentDefinitionBody, *names: str) -> None:
    """Add one transition: a successful claim_task binds the task and reaches route_skills."""
    for name in names:
        step = _step(body, name)
        step.on_mcp_success.extend(_claim_bind_entries())
        step.transitions.append(
            WorkflowTransition(to="route_skills", when="vars.get('assigned_task_id')")
        )


async def test_inline_escalate_handler_moves_submit_to_claim(seats: _Seats) -> None:
    """An inline escalate_task success handler moves submit back to claim.

    The handler is supplied by the test, so this proves the transition only; the claim
    release itself is performed by the real escalate_task tool and is not exercised here.
    """
    claim_task(seats.db, seats.task_id, seats.former)
    body = synthetic_developer_body()
    submit = _step(body, "submit")
    submit.on_mcp_success.append(
        {
            "server": "gobby-tasks",
            "tool": "escalate_task",
            "action": "set_variable",
            "variable": "handed_off",
            "value": True,
        }
    )
    submit.transitions.append(WorkflowTransition(to="claim", when="vars.get('handed_off')"))
    _persist(seats, seats.former, body, "submit")

    notice = await RuleEngine(seats.db)._process_step_after_tool(
        _after_tool(seats.former, "escalate_task", {"task_id": seats.ref}, {}),
        seats.former,
        {},
    )

    assert notice is not None and notice.startswith("Step transition: submit -> claim")
    assert _current_step(seats.db, seats.former) == "claim"


@pytest.mark.parametrize("argument_key", ["arguments", "args"])
@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("assigned", [False, True])
def test_submit_condition_uses_inner_task_despite_outer_shadow(
    seats: _Seats, argument_key: str, encoded: bool, assigned: bool
) -> None:
    """A step gate reads the inner task argument, not the outer proxy key that shadows it."""
    claim_task(seats.db, seats.task_id, seats.former)
    body = synthetic_developer_body()
    _step(body, "submit").on_mcp_before.append(
        {
            "server": "gobby-tasks",
            "tool": "escalate_task",
            "action": "block",
            "when": (
                "tool_input.get('task_id') not in "
                "(vars.get('assigned_task_id'), vars.get('assigned_task_ref'))"
            ),
            "reason": "Only the assigned task may be escalated.",
        }
    )
    _persist(seats, seats.former, body, "submit")
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
        assert "Only the assigned task" in str(response.reason)
    assert _current_step(seats.db, seats.former) == "submit"


@pytest.mark.parametrize("use_ref", [False, True])
@pytest.mark.parametrize("reviewed", [False, True])
async def test_inline_close_handler_moves_implement_to_claim(
    seats: _Seats, use_ref: bool, reviewed: bool
) -> None:
    """An inline close_task success, or its reviewed get_task observation, moves implement to claim.

    Both handlers accept the assigned task by ref or by UUID, so use_ref exercises each form.
    The binding reset is not covered: the synthetic graph carries no handler for it.
    """
    claim_task(seats.db, seats.task_id, seats.former)
    body = synthetic_developer_body()
    implement = _step(body, "implement")
    assigned = (
        "tool_input.get('task_id') in (vars.get('assigned_task_id'), vars.get('assigned_task_ref'))"
    )
    implement.on_mcp_success.append(
        {
            "server": "gobby-tasks",
            "tool": "close_task",
            "action": "set_variable",
            "variable": "closed",
            "value": "(tool_output.get('result') or tool_output).get('closed') is True",
            "when": assigned,
        }
    )
    implement.on_mcp_success.append(
        {
            "server": "gobby-tasks",
            "tool": "get_task",
            "action": "set_variable",
            "variable": "closed",
            "value": (
                "((tool_output.get('result') or tool_output).get('state') or {})"
                ".get('is_closed') is True"
            ),
            "when": assigned,
        }
    )
    implement.transitions.append(WorkflowTransition(to="claim", when="vars.get('closed')"))
    _persist(seats, seats.former, body, "implement")
    task_ref = seats.ref if use_ref else seats.task_id
    engine = RuleEngine(seats.db)
    if reviewed:
        await engine._process_step_after_tool(
            _after_tool(
                seats.former,
                "close_task",
                {"task_id": task_ref, "reason": "moot"},
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
            {"task_id": task_ref, "reason": "moot"},
            {"closed": True},
        )

    notice = await engine._process_step_after_tool(event, seats.former, {})

    assert notice is not None and notice.startswith("Step transition: implement -> claim")
    assert _current_step(seats.db, seats.former) == "claim"


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


def test_lost_active_claim_keeps_previous_handoffs(seats: _Seats) -> None:
    """An unrelated transfer retires the active binding, not unfinished handoffs."""
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
    successor_body = synthetic_developer_body()
    _claim_routes_to_skills(successor_body, "claim")
    _persist(seats, seats.successor, successor_body, "claim", bound=False)
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
            {"success": True, "task_id": seats.task_id, "ref": seats.ref},
        ),
        seats.successor,
        {},
    )
    step, state = _instance_state(seats.db, seats.successor)
    assert (step, state["assigned_task_id"]) == ("route_skills", seats.task_id)


def test_closed_task_returns_owner_to_claim_once_its_claim_is_pruned(
    seats: _Seats, tmp_path: Path
) -> None:
    tasks = LocalTaskManager(seats.db)
    project_id = tasks.get_task(seats.task_id).project_id
    LocalProjectCheckoutManager(seats.db).register(MACHINE_ID, project_id, str(tmp_path))
    claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, "implement")
    tasks.close_task(seats.task_id, reason="done", force=True)

    variables = SessionVariableManager(seats.db).get_variables(seats.former)
    reconcile_claimed_tasks(variables, seats.former, LocalTaskManager(seats.db))
    SessionVariableManager(seats.db).merge_variables(
        seats.former, {"claimed_tasks": variables["claimed_tasks"]}
    )

    assert _current_step(seats.db, seats.former) == "claim"


@pytest.mark.parametrize("starting_step", ["claim", "implement"])
async def test_developer_reclaim_rebinds_assigned_task(seats: _Seats, starting_step: str) -> None:
    tasks = LocalTaskManager(seats.db)
    project_id = tasks.get_task(seats.task_id).project_id
    other = tasks.create_task(
        project_id, "Next claim", validation_criteria="Claim follows selection."
    )
    claim_task(seats.db, seats.task_id, seats.former)
    body = synthetic_developer_body()
    _claim_routes_to_skills(body, starting_step)
    _persist(seats, seats.former, body, starting_step)

    await RuleEngine(seats.db)._process_step_after_tool(
        _after_tool(
            seats.former,
            "claim_task",
            {"task_id": f"#{other.seq_num}"},
            {"success": True, "task_id": other.id, "ref": f"#{other.seq_num}"},
        ),
        seats.former,
        {},
    )

    step, state = _instance_state(seats.db, seats.former)
    assert step == "route_skills"
    assert (state["assigned_task_id"], state["assigned_task_ref"]) == (
        other.id,
        f"#{other.seq_num}",
    )


async def test_inline_link_commit_handler_moves_implement_to_submit(seats: _Seats) -> None:
    """An inline link_commit success handler moves implement to submit.

    The handler is supplied by the test, so this proves dispatch and the transition only,
    not the wiring of the real link_commit handler.
    """
    body = synthetic_developer_body()
    implement = _step(body, "implement")
    implement.on_mcp_success.append(
        {
            "server": "gobby-tasks",
            "tool": "link_commit",
            "action": "set_variable",
            "variable": "implementation_complete",
            "value": True,
        }
    )
    implement.transitions.append(
        WorkflowTransition(to="submit", when="vars.get('implementation_complete')")
    )
    _persist(seats, seats.former, body, "implement")

    notice = await RuleEngine(seats.db)._process_step_after_tool(
        _after_tool(
            seats.former,
            "link_commit",
            {"task_id": seats.task_id, "commit_sha": COMMIT},
            {"task_id": seats.task_id, "commits": [COMMIT]},
        ),
        seats.former,
        {},
    )

    assert notice is not None and notice.startswith("Step transition: implement -> submit")
    step, _ = _instance_state(seats.db, seats.former)
    assert step == "submit"


_ASSIGNED_ONLY = (
    "tool_input.get('task_id') not in (vars.get('assigned_task_id'), vars.get('assigned_task_ref'))"
)
_HANDED_OFF_ONLY = "tool_input.get('task_id') not in vars.get('_developer_handoff_tasks', [])"


def _block_when(
    body: AgentDefinitionBody, step_name: str, tool: str, when: str, reason: str
) -> None:
    """Add one on_mcp_before gate to ``step_name``: block ``tool`` when ``when`` holds."""
    _step(body, step_name).on_mcp_before.append(
        {"server": "gobby-tasks", "tool": tool, "action": "block", "when": when, "reason": reason}
    )


def _block_response(seats: _Seats, event: HookEvent) -> Any:
    return RuleEngine(seats.db)._check_step_tool_enforcement(event, seats.former, {})


_OWN_TASK = (
    "tool_input.get('task_id') in (vars.get('assigned_task_id'), vars.get('assigned_task_ref'))"
)
_NO_WORK = "tool_input.get('reason') in ['moot', 'superseded', 'wont_do']"
_CLOSE_ADMITTED = f"{_HANDED_OFF_ONLY} and not ({_OWN_TASK} and not vars.get('candidate_sent'))"
_HANDOFF_STEPS = ("route_skills", "load_additional_skills", "implement", "submit")
_HANDOFF_RESETS = (
    "implementation_complete",
    "candidate_sent",
    "bounced",
    "skills_routed",
    "additional_skills_loaded",
)
_FORGET_TASK = (
    "[task_id for task_id in vars.get('_developer_handoff_tasks', [])"
    " if task_id != tool_input.get('task_id')]"
)
_OBSERVED_CLOSED = (
    "(tool_output.get('result') or tool_output).get('closed_at') is not None"
    " or ((tool_output.get('result') or tool_output).get('state') or {}).get('is_closed') is True"
)
_CLOSED = (
    "tool_output.get('closed') is True or (tool_output.get('result') or {}).get('closed') is True"
)


def _handoff_body() -> AgentDefinitionBody:
    """The synthetic developer with its handoff, close and skill-routing handlers written inline.

    Only the contract the proxy tests exercise. A directed handoff of the assigned task is
    recorded in _developer_handoff_tasks and releases the binding back to claim.
    de_escalate_task and close_task are admitted for a listed task, or for the assigned task
    before a candidate was sent, and a no-work close is refused once one was sent. A
    get_task routes skills to implement, and a link_commit under the claim completes it.
    """
    body = synthetic_developer_body()
    _claim_routes_to_skills(body, "claim")
    for name in _HANDOFF_STEPS:
        step = _step(body, name)
        step.on_mcp_success.extend(_claim_bind_entries())
        _block_when(body, name, "escalate_task", _ASSIGNED_ONLY, "Hand off only the assigned task.")
        _block_when(
            body,
            name,
            "de_escalate_task",
            _HANDED_OFF_ONLY,
            "Finish only a task this developer handed off.",
        )
        _block_when(
            body,
            name,
            "close_task",
            _CLOSE_ADMITTED,
            "Finish only a task this developer handed off.",
        )
        _block_when(
            body,
            name,
            "close_task",
            f"{_NO_WORK} and vars.get('candidate_sent')",
            "A candidate was already sent for this task, so it cannot take the no-work shortcut.",
        )
        step.on_mcp_success.extend(
            [
                _set(
                    "escalate_task",
                    "_developer_handoff_tasks",
                    "vars.get('_developer_handoff_tasks', []) + [vars.get('assigned_task_id')]",
                ),
                _set("escalate_task", "handed_off", True),
                _set("escalate_task", "task_claimed", False),
                _set("escalate_task", "assigned_task_id", None),
                _set("escalate_task", "assigned_task_ref", None),
                *(_set("escalate_task", variable, False) for variable in _HANDOFF_RESETS),
                _set("get_task", "_developer_handoff_tasks", _FORGET_TASK, _OBSERVED_CLOSED),
                _set("close_task", "_developer_handoff_tasks", _FORGET_TASK, _CLOSED),
            ]
        )
        step.transitions.append(WorkflowTransition(to="claim", when="vars.get('handed_off')"))
    route = _step(body, "route_skills")
    route.on_mcp_success.extend(
        [
            _set("get_task", "skills_routed", True, _OWN_TASK),
            _set("get_task", "additional_skills_loaded", True, _OWN_TASK),
        ]
    )
    route.transitions.append(
        WorkflowTransition(to="load_additional_skills", when="vars.get('skills_routed')")
    )
    _step(body, "load_additional_skills").transitions.append(
        WorkflowTransition(to="implement", when="vars.get('additional_skills_loaded')")
    )
    implement = _step(body, "implement")
    implement.on_mcp_success.append(_set("link_commit", "implementation_complete", True, _OWN_TASK))
    implement.transitions.append(
        WorkflowTransition(to="submit", when="vars.get('implementation_complete')")
    )
    return body


def _task_proxy(seats: _Seats, *, full_hooks: bool = False) -> ToolProxyService:
    """Use real proxy dispatch with step enforcement as the workflow adapter."""
    engine = RuleEngine(seats.db, task_manager=LocalTaskManager(seats.db))
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


@pytest.mark.parametrize("task_ref", ["#999999", "foreign-task", None])
def test_submit_refuses_handoff_of_an_unassigned_task(seats: _Seats, task_ref: str | None) -> None:
    claim_task(seats.db, seats.task_id, seats.former)
    body = synthetic_developer_body()
    _block_when(body, "submit", "escalate_task", _ASSIGNED_ONLY, "Only the assigned task.")
    _persist(seats, seats.former, body, "submit")

    response = _block_response(seats, _before_escalation(seats, task_ref))

    assert response is not None and response.decision == "block"
    assert "Only the assigned task" in str(response.reason)
    assert _current_step(seats.db, seats.former) == "submit"


@pytest.mark.parametrize("assigned", [False, True])
def test_implement_close_blocks_unsubmitted_work_and_other_tasks(
    seats: _Seats, assigned: bool
) -> None:
    tasks = LocalTaskManager(seats.db)
    claim_task(seats.db, seats.task_id, seats.former)
    body = synthetic_developer_body()
    _block_when(body, "implement", "close_task", _HANDED_OFF_ONLY, "Finish only a handed-off task.")
    _persist(seats, seats.former, body, "implement")
    if assigned:
        target = seats.task_id
    else:
        target = tasks.create_task(
            tasks.get_task(seats.task_id).project_id,
            "Unassigned close target",
            validation_criteria="The developer cannot close an unassigned task.",
        ).id
    event = replace(
        _after_tool(seats.former, "close_task", {"task_id": target}, {}),
        event_type=HookEventType.BEFORE_TOOL,
    )

    response = _block_response(seats, event)

    assert response is not None and response.decision == "block"
    assert "Finish only a handed-off task" in str(response.reason)
    assert tasks.get_task(target).closed_at is None


@pytest.mark.parametrize("step", ["claim", "implement", "submit"])
def test_inline_block_gate_refuses_de_escalate_without_handoff(seats: _Seats, step: str) -> None:
    """An inline on_mcp_before block gate refuses de_escalate_task of an unhanded task.

    The gate is added by the test, so this proves its condition, not the bundled seat's gate.
    """
    claim_task(seats.db, seats.task_id, seats.former)
    body = synthetic_developer_body()
    _block_when(body, step, "de_escalate_task", _HANDED_OFF_ONLY, "Finish only a handed-off task.")
    _persist(seats, seats.former, body, step)
    event = replace(
        _after_tool(seats.former, "de_escalate_task", {"task_id": seats.ref}, {}),
        event_type=HookEventType.BEFORE_TOOL,
    )

    response = _block_response(seats, event)

    assert response is not None and response.decision == "block"
    assert "Finish only a handed-off task" in str(response.reason)


def test_claim_step_refuses_escalation(seats: _Seats) -> None:
    body = synthetic_developer_body()
    _step(body, "claim").allowed_mcp_tools = ["gobby-tasks:claim_task"]
    _persist(seats, seats.former, body, "claim", bound=False)

    response = _block_response(seats, _before_escalation(seats, seats.ref))

    assert response is not None and response.decision == "block"
    assert "MCP tool 'gobby-tasks:escalate_task' is not allowed in the 'claim' step" in str(
        response.reason
    )


@pytest.mark.parametrize("foreign", [False, True])
def test_submit_handoff_gate_resolves_ref_in_explicit_project(seats: _Seats, foreign: bool) -> None:
    """The gate resolves the ref in the explicit project, not a same-numbered task elsewhere."""
    tasks = LocalTaskManager(seats.db)
    own_task = tasks.get_task(seats.task_id)
    other_project = LocalProjectManager(seats.db).create("shadow-project")
    other_task = tasks.create_task(
        other_project.id,
        "Same number in another project",
        validation_criteria="The developer cannot hand this task off.",
    )
    assert other_task.seq_num == own_task.seq_num
    claim_task(seats.db, seats.task_id, seats.former)
    body = synthetic_developer_body()
    _block_when(body, "submit", "escalate_task", _ASSIGNED_ONLY, "Only the assigned task.")
    _persist(seats, seats.former, body, "submit")
    event = _before_escalation(seats, seats.ref)
    event.data["tool_input"]["project_id"] = other_project.id if foreign else own_task.project_id

    response = _block_response(seats, event)

    if foreign:
        assert response is not None and response.decision == "block"
    else:
        assert response is None


async def test_implement_close_preview_does_not_advance_the_step(seats: _Seats) -> None:
    """A close preview reports a result, not a completion, so implement does not advance.

    The same inline handler advances on a real close, which proves the handler is wired and
    that the preview is what holds the step back.
    """
    claim_task(seats.db, seats.task_id, seats.former)
    body = synthetic_developer_body()
    implement = _step(body, "implement")
    implement.on_mcp_success.append(
        {
            "server": "gobby-tasks",
            "tool": "close_task",
            "action": "set_variable",
            "variable": "closed",
            "value": "(tool_output.get('result') or tool_output).get('closed') is True",
        }
    )
    implement.transitions.append(WorkflowTransition(to="claim", when="vars.get('closed')"))
    _persist(seats, seats.former, body, "implement")

    notice = await RuleEngine(seats.db)._process_step_after_tool(
        _after_tool(
            seats.former,
            "close_task",
            {"task_id": seats.ref, "reason": "moot", "preview": True},
            {"preview": True, "can_close": True, "closed": False},
        ),
        seats.former,
        {},
    )

    assert notice is None
    assert _current_step(seats.db, seats.former) == "implement"

    closed = await RuleEngine(seats.db)._process_step_after_tool(
        _after_tool(
            seats.former,
            "close_task",
            {"task_id": seats.ref, "reason": "moot"},
            {"closed": True},
        ),
        seats.former,
        {},
    )

    assert closed is not None and closed.startswith("Step transition: implement -> claim")
    assert _current_step(seats.db, seats.former) == "claim"


def test_inline_close_gate_admits_task_listed_as_handed_off(seats: _Seats) -> None:
    """An inline close gate admits a task listed in _developer_handoff_tasks from implement.

    No claim, release or reclaim runs: the handoff list is written directly, so this proves
    the gate's condition only.
    """
    body = synthetic_developer_body()
    _block_when(body, "implement", "close_task", _HANDED_OFF_ONLY, "Finish only a handed-off task.")
    assert body.step_workflow is not None
    body.step_workflow.variables["_developer_handoff_tasks"] = [seats.task_id]
    _persist(seats, seats.former, body, "implement")
    event = replace(
        _after_tool(seats.former, "close_task", {"task_id": seats.ref}, {}),
        event_type=HookEventType.BEFORE_TOOL,
    )

    assert _block_response(seats, event) is None


async def test_reclaimed_escalation_returns_route_skills_to_claim(seats: _Seats) -> None:
    """An escalation from route_skills, after a reclaim, returns the seat to claim."""
    claim_task(seats.db, seats.task_id, seats.former)
    body = synthetic_developer_body()
    route = _step(body, "route_skills")
    route.on_mcp_success.append(
        {
            "server": "gobby-tasks",
            "tool": "escalate_task",
            "action": "set_variable",
            "variable": "handed_off",
            "value": True,
        }
    )
    route.transitions.append(WorkflowTransition(to="claim", when="vars.get('handed_off')"))
    _persist(seats, seats.former, body, "route_skills")

    notice = await RuleEngine(seats.db)._process_step_after_tool(
        _after_tool(seats.former, "escalate_task", {"task_id": seats.ref}, {}),
        seats.former,
        {},
    )

    assert notice is not None and notice.startswith("Step transition: route_skills -> claim")
    assert _current_step(seats.db, seats.former) == "claim"


async def test_release_then_reclaim_requires_fresh_link_commit(seats: _Seats) -> None:
    """A reclaim after release resets implementation_complete, so a fresh link is needed."""
    claim_task(seats.db, seats.task_id, seats.former)
    body = synthetic_developer_body()
    _claim_routes_to_skills(body, "claim", "submit")
    _persist(seats, seats.former, body, "submit")
    instances = AgentStepInstanceManager(seats.db)
    instance = instances.get_for_session(seats.former)
    assert instance is not None
    instance.variables.update(candidate_sent=True, implementation_complete=True)
    instances.save(instance)
    _release(seats)
    claim_task(seats.db, seats.task_id, seats.former)

    await RuleEngine(seats.db)._process_step_after_tool(
        _after_tool(
            seats.former,
            "claim_task",
            {"task_id": seats.ref},
            {"success": True, "task_id": seats.task_id, "ref": seats.ref},
        ),
        seats.former,
        SessionVariableManager(seats.db).get_variables(seats.former),
    )

    step, state = _instance_state(seats.db, seats.former)
    assert step == "route_skills"
    assert state["implementation_complete"] is False
    assert state["candidate_sent"] is False


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


@pytest.mark.parametrize("assigned", [False, True])
async def test_handler_fires_only_for_the_resolved_assigned_task(
    seats: _Seats, assigned: bool
) -> None:
    body = synthetic_developer_body()
    assert body.step_workflow is not None
    implement = next(step for step in body.step_workflow.steps if step.name == "implement")
    implement.on_mcp_success.append(
        {
            "server": "gobby-tasks",
            "tool": "close_task",
            "action": "set_variable",
            "variable": "implementation_complete",
            "value": True,
            "when": (
                "tool_input.get('task_id') in "
                "(vars.get('assigned_task_id'), vars.get('assigned_task_uuid'))"
            ),
        }
    )
    implement.transitions.append(
        WorkflowTransition(to="submit", when="vars.get('implementation_complete')")
    )
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
        current_step="implement",
    )
    AgentStepInstanceManager(seats.db).save(instance)
    foreign = LocalTaskManager(seats.db).create_task(
        LocalTaskManager(seats.db).get_task(seats.task_id).project_id,
        "Unassigned handler target",
        validation_criteria="It cannot advance the dispatched seat.",
    )
    task_ref = seats.ref if assigned else f"#{foreign.seq_num}"
    await RuleEngine(seats.db)._process_step_after_tool(
        _after_tool(seats.former, "close_task", {"task_id": task_ref}, {"closed": True}),
        seats.former,
        {},
    )
    current_step, state = _instance_state(seats.db, seats.former)
    assert state["implementation_complete"] is assigned
    assert current_step == ("submit" if assigned else "implement")


@pytest.mark.parametrize("failed", [False, True])
async def test_unselected_or_failed_claim_completion_does_not_rebind(
    seats: _Seats, failed: bool
) -> None:
    tasks = LocalTaskManager(seats.db)
    project_id = tasks.get_task(seats.task_id).project_id
    selected = tasks.create_task(
        project_id, "Selected", validation_criteria="Selection stays current."
    )
    claim_task(seats.db, seats.task_id, seats.former)
    _seat(seats, seats.former, "implement")
    claim_task(seats.db, selected.id, seats.former)
    output = (
        {"success": False, "error": "Claim failed", "task_id": selected.id}
        if failed
        else {"success": True, "task_id": seats.task_id, "ref": seats.ref}
    )
    event = _after_tool(seats.former, "claim_task", {"task_id": output["task_id"]}, output)
    if failed:
        # The proxy delivers internal tool failures directly, without a success wrapper.
        event.data["tool_output"] = output
    await RuleEngine(seats.db)._process_step_after_tool(
        event,
        seats.former,
        SessionVariableManager(seats.db).get_variables(seats.former),
    )
    step, state = _instance_state(seats.db, seats.former)
    assert step == "implement"
    assert state["assigned_task_id"] == seats.task_id
    assert (
        SessionVariableManager(seats.db).get_variables(seats.former)["active_task_id"]
        == selected.id
    )


@pytest.mark.parametrize("active_state", ["missing", "foreign", "closed", "same-ref"])
def test_unusable_or_equivalent_active_claim_keeps_binding(
    seats: _Seats, active_state: str, tmp_path: Path
) -> None:
    tasks = LocalTaskManager(seats.db)
    project_id = tasks.get_task(seats.task_id).project_id
    LocalProjectCheckoutManager(seats.db).register(MACHINE_ID, project_id, str(tmp_path))
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
            project_id,
            "Unusable target",
            validation_criteria="The unusable target leaves the binding intact.",
        )
        if active_state == "foreign":
            claim_task(seats.db, other.id, seats.successor)
        elif active_state == "closed":
            tasks.close_task(other.id, reason="done", force=True)
        projection = {seats.task_id: seats.ref}
        if active_state != "missing":
            projection[other.id] = f"#{other.seq_num}"
        variables.merge_variables(
            seats.former, {"claimed_tasks": projection, "active_task_id": other.id}
        )

    assert _current_step(seats.db, seats.former) == "implement"


@pytest.mark.parametrize("close_step", ["implement", "submit"])
async def test_reclaim_before_de_escalation_keeps_close_invariants(
    seats: _Seats, tmp_path: Path, close_step: str
) -> None:
    """Reclaiming a handed-off task preserves close authority, not an evidence bypass."""
    tasks = LocalTaskManager(seats.db)
    project_id = tasks.get_task(seats.task_id).project_id
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    LocalProjectCheckoutManager(seats.db).register(MACHINE_ID, project_id, str(repo))
    claim_task(seats.db, seats.task_id, seats.former)
    _persist(seats, seats.former, _handoff_body(), "submit")
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
    assert [gate["name"] for gate in result["blocking_reasons"]] == ["linked_commits"]
    assert result["closed"] is False
    assert tasks.get_task(seats.task_id).validation_override_reason is None


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
    """Closing a no-work parent through real MCP gates preserves the next task binding."""
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
    tasks.close_task(child.id, reason="done")
    _persist(seats, seats.former, _handoff_body(), handoff_step)
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
        # Another authorized session closes out of band; the developer observes it.
        tasks.close_task(seats.task_id, reason="moot")
        observed = await call("get_task", {"task_id": task_ref})
        assert observed["state"]["is_closed"] is True
    else:
        # The structural parent has no commit of its own; its children carried the work,
        # so the canonical no-work disposition satisfies linked_commits (#23341).
        result = await call(
            "close_task",
            {"task_id": task_ref, "reason": "moot", "changes_summary": "All children finished."},
        )
        assert result["closed"] is True, result
    assert tasks.get_task(seats.task_id).closed_at is not None
    step_after, state_after = _instance_state(seats.db, seats.former)
    assert step_after == before[0]
    expected_state = {**before[1], "_developer_handoff_tasks": []}
    assert state_after == expected_state
    session_after = SessionVariableManager(seats.db).get_variables(seats.former)
    for key in ("task_claimed", "assigned_task_id", "claimed_tasks"):
        assert session_after.get(key) == session_before.get(key)
    assert tasks.get_task(next_task.id).claimed_by_session_id == seats.former


@pytest.mark.parametrize("use_ref", [False, True])
async def test_implement_no_work_close_preview_reaches_checklist(
    seats: _Seats, tmp_path: Path, use_ref: bool
) -> None:
    """No candidate is needed to request a no-work close of the assigned claim."""
    task = LocalTaskManager(seats.db).get_task(seats.task_id)
    LocalProjectCheckoutManager(seats.db).register(MACHINE_ID, task.project_id, str(tmp_path))
    claim_task(seats.db, seats.task_id, seats.former)
    _persist(seats, seats.former, _handoff_body(), "implement")
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
                "reason": "moot",
                "project_path": str(tmp_path),
                "preview": True,
                "response_detail": "diagnostic",
            },
            session_id=seats.former,
            enforce_workflow=True,
        )

    # No-work preview reaches the mechanical checklist without launching a reviewer.
    assert result["preview"] is True
    assert result["can_close"] is True
    assert result["closed"] is False
    assert all(gate["passed"] for gate in result["gates"])
    assert _instance_state(seats.db, seats.former) == before
    assert LocalTaskManager(seats.db).get_task(seats.task_id).closed_at is None


async def test_relaunched_developer_recovers_handoff_close_preview(
    seats: _Seats, tmp_path: Path
) -> None:
    tasks = LocalTaskManager(seats.db)
    project_id = tasks.get_task(seats.task_id).project_id
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    LocalProjectCheckoutManager(seats.db).register(MACHINE_ID, project_id, str(repo))
    claim_task(seats.db, seats.task_id, seats.former)
    _persist(seats, seats.former, _handoff_body(), "submit")
    proxy = _task_proxy(seats)
    engine = RuleEngine(seats.db)

    async def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        with session_context_for_test(seats.former):
            result = await proxy.call_tool(
                "gobby-tasks", tool, arguments, session_id=seats.former, enforce_workflow=True
            )
        await engine._process_step_after_tool(
            _after_tool(seats.former, tool, arguments, result),
            seats.former,
            SessionVariableManager(seats.db).get_variables(seats.former),
        )
        return cast(dict[str, Any], result)

    handoff = {"task_id": seats.ref, "reason": "Handed off: reviewer LAND at linked commit"}
    assert await call("escalate_task", handoff) == {}
    assert _instance_state(seats.db, seats.former)[1]["_developer_handoff_tasks"] == [seats.task_id]
    # A fresh relaunch has lost the old instance's handoff permission.
    _persist(seats, seats.former, _handoff_body(), "claim", bound=False)
    assert _instance_state(seats.db, seats.former)[1]["_developer_handoff_tasks"] == []
    claimed = await call("claim_task", {"task_id": seats.ref})
    assert claimed["task_id"] == seats.task_id
    assert _current_step(seats.db, seats.former) == "route_skills"
    # Repeating the authorized handoff releases the reclaimed escalated task
    # and rebuilds close rights through ordinary tool effects, without variable writes.
    assert await call("escalate_task", handoff) == {}
    assert _current_step(seats.db, seats.former) == "claim"
    assert _instance_state(seats.db, seats.former)[1]["_developer_handoff_tasks"] == [seats.task_id]
    assert "error" not in await call(
        "de_escalate_task", {"task_id": seats.ref, "reason": "Landed and released"}
    )
    # The reviewer LANDed the candidate at the repo's HEAD, the linked work the reason names.
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    tasks.link_commit(seats.task_id, head)
    preview = await call(
        "close_task",
        {"task_id": seats.ref, "preview": True, "changes_summary": "Landed and released."},
    )
    assert preview["preview"] is True
    assert preview["can_close"] is True
    assert preview["commit_shas"] == [head]
    assert "blocking_reasons" not in preview
    assert tasks.get_task(seats.task_id).closed_at is None


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
    tasks.close_task(history.id, reason="done", force=True)
    active = tasks.create_task(
        project_id, "Active claimed task", validation_criteria="The active claim can be handed off."
    )
    active_ref = f"#{active.seq_num}"
    claim_task(seats.db, seats.task_id, seats.former)
    _persist(seats, seats.former, _handoff_body(), "implement")
    if closed_bound:
        tasks.close_task(seats.task_id, reason="done", force=True)
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


async def test_commit_linked_under_an_earlier_claim_does_not_reach_submit(seats: _Seats) -> None:
    """A commit linked under an earlier claim does not complete the successor's candidate.

    Only the successor's own link_commit sets implementation_complete, so the earlier link
    neither reaches submit nor counts after the force transfer.
    """
    claim_task(seats.db, seats.task_id, seats.former)
    LocalTaskManager(seats.db).link_commit(seats.task_id, COMMIT)
    _force_transfer(seats)
    _persist(seats, seats.successor, _handoff_body(), "route_skills")

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
    """A bounced flag set on submit returns the seat to implement, with no reviewer envelope."""
    claim_task(seats.db, seats.task_id, seats.former)
    body = synthetic_developer_body()
    _step(body, "submit").transitions.append(
        WorkflowTransition(to="implement", when="vars.get('bounced')")
    )
    _persist(seats, seats.former, body, "submit")
    instances = AgentStepInstanceManager(seats.db)
    instance = instances.get_for_session(seats.former)
    assert instance is not None
    instance.variables.update(bounced=True)
    instances.save(instance)

    notice = await RuleEngine(seats.db)._process_step_after_tool(
        _after_tool(seats.former, "get_task", {"task_id": seats.ref}, {"id": seats.task_id}),
        seats.former,
        {},
    )

    assert notice is not None and notice.startswith("Step transition: submit -> implement")
    assert _current_step(seats.db, seats.former) == "implement"


@pytest.mark.parametrize("step", ["implement", "submit"])
async def test_bounced_candidate_cannot_close_as_no_work(
    seats: _Seats, tmp_path: Path, step: str
) -> None:
    """A bounced candidate's no-work close preview is refused through the real proxy."""
    tasks = LocalTaskManager(seats.db)
    LocalProjectCheckoutManager(seats.db).register(
        MACHINE_ID, tasks.get_task(seats.task_id).project_id, str(tmp_path)
    )
    claim_task(seats.db, seats.task_id, seats.former)
    _persist(seats, seats.former, _handoff_body(), step)
    instances = AgentStepInstanceManager(seats.db)
    instance = instances.get_for_session(seats.former)
    assert instance is not None
    instance.variables.update(candidate_sent=True, bounced=True)
    instances.save(instance)

    with session_context_for_test(seats.former):
        result = await _task_proxy(seats).call_tool(
            "gobby-tasks",
            "close_task",
            {
                "task_id": seats.ref,
                "reason": "moot",
                "preview": True,
                "project_path": str(tmp_path),
            },
            session_id=seats.former,
            enforce_workflow=True,
        )

    assert result["error_code"] == "TOOL_BLOCKED"
    assert tasks.get_task(seats.task_id).closed_at is None
    assert _current_step(seats.db, seats.former) == step


@pytest.mark.parametrize("assigned", [True, False])
@pytest.mark.parametrize("tool", ["claim_task", "get_task", "close_task"])
async def test_dispatched_seat_handlers_match_resolved_assigned_task(
    seats: _Seats, tool: str, assigned: bool
) -> None:
    """Each seat handler fires only for the task the seat resolved as its assignment."""
    tasks = LocalTaskManager(seats.db)
    other = tasks.create_task(
        tasks.get_task(seats.task_id).project_id,
        "Unassigned handler target",
        validation_criteria="The handler does not fire for an unassigned task.",
    )
    claim_task(seats.db, seats.task_id, seats.former)
    body = synthetic_developer_body()
    _step(body, "implement").on_mcp_success.append(_set(tool, "handled", True, _OWN_TASK))
    _persist(seats, seats.former, body, "implement")
    target = seats.task_id if assigned else other.id

    await RuleEngine(seats.db)._process_step_after_tool(
        _after_tool(seats.former, tool, {"task_id": target}, {"task_id": target}),
        seats.former,
        {},
    )

    _, state = _instance_state(seats.db, seats.former)
    assert state.get("handled", False) is assigned


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
