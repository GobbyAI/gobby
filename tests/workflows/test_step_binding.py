"""A developer seat's step binding follows its live task claim (#23886)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._transitions import claim_task
from gobby.utils.project_context import reset_project_context, set_project_context
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


def _claim_routes_to_skills(body: AgentDefinitionBody, *names: str) -> None:
    """Add one transition: a successful claim_task binds the task and reaches route_skills."""
    bind = [
        {
            "server": "gobby-tasks",
            "tool": "claim_task",
            "action": "set_variable",
            "variable": "assigned_task_id",
            "value": "(tool_output.get('result') or tool_output).get('task_id')",
        },
        {
            "server": "gobby-tasks",
            "tool": "claim_task",
            "action": "set_variable",
            "variable": "assigned_task_ref",
            "value": "(tool_output.get('result') or tool_output).get('ref')",
        },
    ]
    for name in names:
        step = _step(body, name)
        step.on_mcp_success.extend(bind)
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
