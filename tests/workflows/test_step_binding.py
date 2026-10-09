"""A developer seat's step binding follows its live task claim (#23886)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._transitions import claim_task
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.observers import reconcile_claimed_tasks
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_context import get_active_step_workflow_context
from gobby.workflows.step_instances import AgentStepInstanceManager, build_step_instance
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

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
) -> _Seats:
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
    return _Seats(
        db=temp_db,
        former=former,
        successor=successor,
        task_id=task.id,
        ref=f"#{task.seq_num}",
    )


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


async def test_binding_a_task_with_a_linked_commit_reaches_submit(seats: _Seats) -> None:
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
    assert notice.startswith(
        "Step transition: route_skills -> load_additional_skills -> implement -> submit"
    )
    step, state = _instance_state(seats.db, seats.successor)
    assert (step, state["implementation_complete"], state["bounced"]) == ("submit", True, False)


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
