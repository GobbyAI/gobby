"""A seat holding the own-step-phase grant advances only its own step phase."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

import pytest

from gobby.mcp_proxy.tools.workflows._variables import set_variable
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import SessionContext, reset_session_context, set_session_context
from gobby.workflows.engine.idle_declaration import SET_VARIABLE_TOOL, native_variable_write_error
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests.workflows.step_instance_fixtures import make_step_instance

pytestmark = pytest.mark.unit

CALLER = "b1111111-1111-4111-8111-111111111111"
OTHER = "b2222222-2222-4222-8222-222222222222"
PROJECT = "b3333333-3333-4333-8333-333333333333"
ROLE = "fixture-phase-seat"
PHASE_CAPABILITY = "write_own_step_phase"
MARKER = "[native-variable-authority]"


@pytest.fixture
def sessions(temp_db: HubDatabase) -> Iterator[SessionManager]:
    temp_db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, CURRENT_TIMESTAMP)",
        (PROJECT, "own-step-phase"),
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


def _seat(db: HubDatabase, *, granted: bool = True) -> None:
    AgentDefinitionManager(db).create(
        ROLE,
        {
            "name": ROLE,
            "prompts": {"agent": "Advance your own step phase."},
            "workflows": {"rule_selectors": {"include": []}},
            "capabilities": [PHASE_CAPABILITY] if granted else [],
        },
        project_id=PROJECT,
        enabled=True,
    )
    SessionVariableManager(db).set_variable(CALLER, "_agent_type", ROLE)
    AgentStepInstanceManager(db).save(
        make_step_instance(
            CALLER,
            agent_name=ROLE,
            current_step="wait",
            variables={"phase": "wait", "candidate_sent": True},
        )
    )


def test_granted_seat_writes_its_own_step_phase(sessions: SessionManager) -> None:
    _seat(sessions.db)
    result = set_variable(sessions, sessions.db, "phase", "schedule", CALLER, scope="step")
    assert result["success"] is True, result
    stored = AgentStepInstanceManager(sessions.db).get_for_session(CALLER)
    assert stored is not None and stored.variables["phase"] == "schedule"


@pytest.mark.parametrize(
    "tool_input",
    [
        {"name": "phase", "value": "schedule", "scope": "step", "session_id": CALLER},
    ],
)
def test_grant_admits_own_step_phase_at_the_hook(
    sessions: SessionManager, tool_input: dict[str, object]
) -> None:
    _seat(sessions.db)
    error = native_variable_write_error(
        SET_VARIABLE_TOOL, tool_input, CALLER, {"_agent_type": ROLE}, sessions
    )
    assert error is None, error


@pytest.mark.parametrize(
    "tool_input",
    [
        {"name": "phase", "value": "schedule", "scope": "step", "session_id": OTHER},
        {"name": "phase", "value": "schedule", "scope": "step", "session_id": "not-a-session"},
        {"name": "phase", "value": "schedule", "scope": "step"},
        {"name": "phase", "value": "schedule", "session_id": CALLER},
        {"name": "phase", "value": "schedule"},
        {"name": "phase", "value": "schedule", "scope": "session", "session_id": CALLER},
        {"name": "candidate_sent", "value": False, "scope": "step", "session_id": CALLER},
        {"name": "epic_assignment", "value": {}, "scope": "step", "session_id": CALLER},
    ],
)
def test_grant_refuses_everything_beyond_own_step_phase(
    sessions: SessionManager, tool_input: dict[str, object]
) -> None:
    _seat(sessions.db)
    error = native_variable_write_error(
        SET_VARIABLE_TOOL, tool_input, CALLER, {"_agent_type": ROLE}, sessions
    )
    assert error is not None and MARKER in error


def test_seat_without_the_grant_still_refuses_its_own_step_phase(sessions: SessionManager) -> None:
    _seat(sessions.db, granted=False)
    error = native_variable_write_error(
        SET_VARIABLE_TOOL,
        {"name": "phase", "value": "schedule", "scope": "step", "session_id": CALLER},
        CALLER,
        {"_agent_type": ROLE},
        sessions,
    )
    assert error is not None and MARKER in error


@pytest.mark.parametrize(
    ("target", "scope", "name"),
    [
        (OTHER, "step", "phase"),
        (CALLER, "session", "phase"),
        (CALLER, "step", "candidate_sent"),
    ],
)
def test_mutation_boundary_refuses_foreign_session_or_wrong_scope(
    sessions: SessionManager,
    target: str,
    scope: Literal["session", "step"],
    name: str,
) -> None:
    _seat(sessions.db)
    result = set_variable(sessions, sessions.db, name, "schedule", target, scope=scope)
    assert result["success"] is False, result
    assert MARKER in result["error"]
    stored = AgentStepInstanceManager(sessions.db).get_for_session(CALLER)
    assert stored is not None and stored.variables["phase"] == "wait"
