"""Native variable authority at both hook and physical mutation boundaries."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.workflows._variables import set_variable
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import SessionContext, reset_session_context, set_session_context
from gobby.workflows.agent_models import AgentStepWorkflowBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.idle_declaration import SET_VARIABLE_TOOL, native_variable_write_error
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstance, AgentStepInstanceManager

pytestmark = pytest.mark.unit
CALLER = "a1111111-1111-4111-8111-111111111111"
OTHER = "a2222222-2222-4222-8222-222222222222"
PROJECT = "a3333333-3333-4333-8333-333333333333"


@pytest.fixture
def sessions(temp_db: HubDatabase) -> Iterator[SessionManager]:
    temp_db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, CURRENT_TIMESTAMP)",
        (PROJECT, "native-authority"),
    )
    for session_id in (CALLER, OTHER):
        temp_db.execute(
            "INSERT INTO sessions "
            "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (session_id, session_id, "21000000-0000-4000-8000-000000000001", "codex", PROJECT),
        )
    token = set_session_context(SessionContext(session_id=CALLER))
    try:
        yield SessionManager(temp_db)
    finally:
        reset_session_context(token)


def _instance(db: HubDatabase, role: str, step: str) -> None:
    workflow = AgentStepWorkflowBody.model_validate({"steps": [{"name": step}]})
    AgentStepInstanceManager(db).save(
        AgentStepInstance(
            id=str(uuid4()),
            session_id=CALLER,
            agent_name=role,
            snapshot=workflow,
            current_step=step,
            enabled=True,
            step_entered_at=datetime.now(UTC),
            variables={"candidate_sent": True},
        )
    )


def _blocked_seat_steps() -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for path in sorted(get_bundled_agents_path().glob("*.yaml")):
        agent = yaml.safe_load(path.read_text())
        role = agent["name"]
        if role in {"assistant", "orchestrator", "default"}:
            continue
        workflow = agent.get("step_workflow") or {}
        cases.extend((role, step["name"]) for step in workflow.get("steps", []))
        cases.append((role, "no-workflow"))
    return cases


@pytest.mark.parametrize(("role", "step"), _blocked_seat_steps())
@pytest.mark.parametrize("scope", ["session", "step"])
def test_every_seat_step_refuses_native_mutation(
    sessions: SessionManager, role: str, step: str, scope: Literal["session", "step"]
) -> None:
    manager = SessionVariableManager(sessions.db)
    manager.set_variable(CALLER, "_agent_type", role)
    manager.set_variable(CALLER, "candidate_sent", True)
    if step != "no-workflow":
        _instance(sessions.db, role, step)
    result = set_variable(sessions, sessions.db, "candidate_sent", False, CALLER, scope=scope)
    assert result["success"] is False
    assert "[native-variable-authority]" in result["error"]
    assert manager.get_variables(CALLER)["candidate_sent"] is True
    instance = AgentStepInstanceManager(sessions.db).get_for_session(CALLER)
    if instance is not None:
        assert instance.variables["candidate_sent"] is True


@pytest.mark.parametrize("role", ["assistant", "orchestrator", "default"])
@pytest.mark.parametrize("scope", ["session", "step"])
def test_allowed_roles_keep_own_session_writes(
    sessions: SessionManager, role: str, scope: Literal["session", "step"]
) -> None:
    manager = SessionVariableManager(sessions.db)
    manager.set_variable(CALLER, "_agent_type", role)
    _instance(sessions.db, role, "serve")
    result = set_variable(sessions, sessions.db, "sample", "value", CALLER, scope=scope)
    assert result["success"] is True
    if scope == "session":
        assert manager.get_variables(CALLER)["sample"] == "value"
    else:
        instance = AgentStepInstanceManager(sessions.db).get_for_session(CALLER)
        assert instance is not None and instance.variables["sample"] == "value"


@pytest.mark.parametrize("role", ["assistant", "orchestrator", "default", "developer"])
def test_no_role_can_write_foreign_session(sessions: SessionManager, role: str) -> None:
    manager = SessionVariableManager(sessions.db)
    manager.set_variable(CALLER, "_agent_type", role)
    result = set_variable(sessions, sessions.db, "sample", True, OTHER)
    assert result["success"] is False
    assert "[native-variable-authority]" in result["error"]
    assert "sample" not in manager.get_variables(OTHER)


def test_absent_role_is_default_only_without_a_bound_seat(sessions: SessionManager) -> None:
    result = set_variable(sessions, sessions.db, "sample", 1, CALLER)
    assert result["success"] is True
    _instance(sessions.db, "developer", "submit")
    result = set_variable(sessions, sessions.db, "sample", 2, CALLER)
    assert result["success"] is False
    assert SessionVariableManager(sessions.db).get_variables(CALLER)["sample"] == 1


@pytest.mark.parametrize("role", [None, "", "unknown", ["default"]])
def test_malformed_present_role_never_becomes_default(
    sessions: SessionManager, role: object
) -> None:
    error = native_variable_write_error(
        SET_VARIABLE_TOOL, {"session_id": CALLER}, CALLER, {"_agent_type": role}, sessions
    )
    assert error is not None and "[native-variable-authority]" in error


@pytest.mark.parametrize("tool_input", [None, [], {}, {"session_id": OTHER}])
def test_missing_or_foreign_target_is_refused(sessions: SessionManager, tool_input: object) -> None:
    error = native_variable_write_error(
        SET_VARIABLE_TOOL, tool_input, CALLER, {"_agent_type": "default"}, sessions
    )
    assert error is not None and "[native-variable-authority]" in error


def test_missing_caller_does_not_borrow_target_identity(sessions: SessionManager) -> None:
    error = native_variable_write_error(
        SET_VARIABLE_TOOL, {"session_id": CALLER}, None, {}, sessions
    )
    assert error is not None and "resolved calling session" in error


def test_allowed_role_still_cannot_write_reserved_authority(sessions: SessionManager) -> None:
    manager = SessionVariableManager(sessions.db)
    manager.set_variable(CALLER, "_agent_type", "default")
    result = set_variable(sessions, sessions.db, "_agent_type", "developer", CALLER)
    assert result["success"] is False
    assert manager.get_variables(CALLER)["_agent_type"] == "default"


@pytest.mark.parametrize("role", ["developer", "code-reviewer", "assistant", "default"])
async def test_hook_checks_native_authority_before_infrastructure_exemption(
    sessions: SessionManager, role: str
) -> None:
    engine = RuleEngine(sessions.db, session_manager=sessions)
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=CALLER,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        metadata={},
        data={
            "tool_name": SET_VARIABLE_TOOL,
            "tool_input": {
                "name": "candidate_sent",
                "value": False,
                "session_id": CALLER,
            },
        },
    )
    variables = {"_agent_type": role, "candidate_sent": True}
    response = await engine.evaluate(event, session_id=CALLER, variables=variables)
    assert (response.decision == "block") == (role in {"developer", "code-reviewer"})
    assert variables["candidate_sent"] is True
    if response.decision == "block":
        assert "[native-variable-authority]" in (response.reason or "")
