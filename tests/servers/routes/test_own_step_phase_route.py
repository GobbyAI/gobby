"""A live seat advances its own step phase over the variable route with its run identity."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import AGENT_RUN_ID_HEADER
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests.servers.conftest import create_http_server
from tests.workflows.step_instance_fixtures import make_step_instance

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("authenticated_http_requests")]

_PROJECT = "c3333333-3333-4333-8333-333333333333"
_PARENT = "c4444444-4444-4444-8444-444444444444"
_OWN = "c1111111-1111-4111-8111-111111111111"
_OTHER = "c2222222-2222-4222-8222-222222222222"
_ROLE = "fixture-phase-seat"
_CAPABILITY = "write_own_step_phase"
_PHASES = ["schedule", "wait", "integration", "repair", "integration-leaf", "completion"]


def _seed(db: HubDatabase) -> str:
    """Insert a granted seat with a live run; return the run id."""
    db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, CURRENT_TIMESTAMP)",
        (_PROJECT, "own-step-phase-route"),
    )
    for session_id in (_PARENT, _OWN, _OTHER):
        db.execute(
            "INSERT INTO sessions "
            "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (session_id, session_id, "21000000-0000-4000-8000-000000000001", "codex", _PROJECT),
        )
    AgentDefinitionManager(db).create(
        _ROLE,
        {
            "name": _ROLE,
            "prompts": {"agent": "Advance your own step phase."},
            "workflows": {"rule_selectors": {"include": []}},
            "capabilities": [_CAPABILITY],
        },
        project_id=_PROJECT,
        enabled=True,
    )
    SessionVariableManager(db).set_variable(_OWN, "_agent_type", _ROLE)
    SessionVariableManager(db).set_variable(_OTHER, "_agent_type", _ROLE)
    for session_id in (_OWN, _OTHER):
        AgentStepInstanceManager(db).save(
            make_step_instance(
                session_id,
                agent_name=_ROLE,
                current_step="wait",
                variables={"phase": "wait"},
            )
        )
    run = LocalAgentRunManager(db).create(
        _PARENT, "codex", "Coordinate the assigned leaf.", agent_name=_ROLE, child_session_id=_OWN
    )
    return str(run.id)


def _client(db: HubDatabase) -> TestClient:
    server = create_http_server(
        config=DaemonConfig(),
        database=db,
        session_manager=SessionManager(db),
    )
    return TestClient(server.app)


@pytest.mark.parametrize("phase", _PHASES)
def test_live_seat_advances_own_phase_over_documented_route(
    temp_db: HubDatabase, phase: str
) -> None:
    run_id = _seed(temp_db)
    response = _client(temp_db).post(
        f"/api/sessions/{_OWN}/variables/set",
        headers={
            "X-Gobby-Session-Id": _OWN,
            "X-Gobby-Project-Id": _PROJECT,
            AGENT_RUN_ID_HEADER: run_id,
        },
        json={"name": "phase", "value": phase, "scope": "step"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True, body
    stored = AgentStepInstanceManager(temp_db).get_for_session(_OWN)
    assert stored is not None and stored.variables["phase"] == phase


def test_live_seat_without_its_run_identity_is_refused_at_the_route(temp_db: HubDatabase) -> None:
    _seed(temp_db)
    response = _client(temp_db).post(
        f"/api/sessions/{_OWN}/variables/set",
        headers={"X-Gobby-Session-Id": _OWN, "X-Gobby-Project-Id": _PROJECT},
        json={"name": "phase", "value": "completion", "scope": "step"},
    )
    assert response.status_code == 403, response.text
    stored = AgentStepInstanceManager(temp_db).get_for_session(_OWN)
    assert stored is not None and stored.variables["phase"] == "wait"


def test_live_seat_cannot_advance_another_seats_phase_over_the_route(temp_db: HubDatabase) -> None:
    run_id = _seed(temp_db)
    response = _client(temp_db).post(
        f"/api/sessions/{_OTHER}/variables/set",
        headers={
            "X-Gobby-Session-Id": _OWN,
            "X-Gobby-Project-Id": _PROJECT,
            AGENT_RUN_ID_HEADER: run_id,
        },
        json={"name": "phase", "value": "completion", "scope": "step"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is False, body
    assert "[native-variable-authority]" in body["error"]
    stored = AgentStepInstanceManager(temp_db).get_for_session(_OTHER)
    assert stored is not None and stored.variables["phase"] == "wait"
