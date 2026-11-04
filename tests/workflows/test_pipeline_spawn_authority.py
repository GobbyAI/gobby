"""A pipeline executor session spawns with its launcher's authority, never more."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager, ensure_system_session, system_session_id
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.condition_helpers_sessions import spawn_target_allowed
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

SEAT = "synthetic-pipeline-launcher"
WORKER = "synthetic-pipeline-worker"
OTHER = "synthetic-pipeline-other"


@pytest.fixture
def project_id(temp_db: HubDatabase, sample_project: dict[str, Any]) -> str:
    for name, spawnable in ((SEAT, [WORKER]), (WORKER, []), (OTHER, [])):
        AgentDefinitionManager(temp_db).create(
            name,
            {
                "name": name,
                "prompts": {"agent": "Work."},
                "workflows": {"rule_selectors": {"include": ["tag:default"]}},
                "spawnable_agents": spawnable,
            },
        )
    return str(sample_project["id"])


def _root(sessions: SessionManager, project_id: str) -> Session:
    return sessions.register(
        external_id=f"operator-{uuid4().hex}",
        machine_id=require_machine_id(),
        source="claude",
        project_id=project_id,
        agent_depth=0,
    )


def _seat(db: HubDatabase, sessions: SessionManager, project_id: str) -> Session:
    root = _root(sessions, project_id)
    child = sessions.register(
        external_id=f"seat-{uuid4().hex}",
        machine_id=require_machine_id(),
        source="claude",
        project_id=project_id,
        parent_session_id=root.id,
        agent_depth=1,
    )
    run = LocalAgentRunManager(db).create(
        parent_session_id=root.id,
        provider="claude",
        prompt="work",
        agent_name=SEAT,
        child_session_id=child.id,
    )
    SessionVariableManager(db).set_variable(child.id, "_agent_type", SEAT)
    seated = sessions.update_terminal_pickup_metadata(child.id, agent_run_id=run.id)
    assert seated is not None
    return seated


def _pipeline(
    db: HubDatabase, sessions: SessionManager, project_id: str, launcher: str | None
) -> Session:
    """The child session pipeline_executor registers for a top-level run."""
    session = sessions.register(
        external_id=f"pipeline-{uuid4().hex}",
        machine_id=require_machine_id(),
        source="pipeline",
        project_id=project_id,
        parent_session_id=launcher,
        agent_depth=0,
    )
    SessionVariableManager(db).set_variable(session.id, "_agent_type", "pipeline")
    return session


def _allowed(sessions: SessionManager, caller: Session, agent: str, project_id: str) -> bool:
    return spawn_target_allowed(
        sessions, caller.id, "spawn_agent", agent, target_project_id=project_id
    )


def test_operator_launched_pipeline_spawns_any_agent(
    temp_db: HubDatabase, session_manager: SessionManager, project_id: str
) -> None:
    launcher = _root(session_manager, project_id)
    pipeline = _pipeline(temp_db, session_manager, project_id, launcher.id)

    assert _allowed(session_manager, pipeline, OTHER, project_id) is True
    assert _allowed(session_manager, pipeline, "default", project_id) is True


def test_system_launched_pipeline_spawns_any_agent(
    temp_db: HubDatabase, session_manager: SessionManager, project_id: str
) -> None:
    ensure_system_session(temp_db)
    system = session_manager.get(system_session_id())
    assert system is not None
    pipeline = _pipeline(temp_db, session_manager, project_id, system.id)

    assert _allowed(session_manager, system, OTHER, project_id) is True
    assert _allowed(session_manager, pipeline, OTHER, project_id) is True


def test_seat_launched_pipeline_keeps_the_seats_spawn_list(
    temp_db: HubDatabase, session_manager: SessionManager, project_id: str
) -> None:
    seat = _seat(temp_db, session_manager, project_id)
    pipeline = _pipeline(temp_db, session_manager, project_id, seat.id)

    assert _allowed(session_manager, pipeline, WORKER, project_id) is True
    assert _allowed(session_manager, pipeline, OTHER, project_id) is False
    assert _allowed(session_manager, pipeline, "default", project_id) is False


def test_nested_pipeline_resolves_to_the_first_non_pipeline_ancestor(
    temp_db: HubDatabase, session_manager: SessionManager, project_id: str
) -> None:
    seat = _seat(temp_db, session_manager, project_id)
    outer = _pipeline(temp_db, session_manager, project_id, seat.id)
    inner = _pipeline(temp_db, session_manager, project_id, outer.id)
    operator_outer = _pipeline(
        temp_db, session_manager, project_id, _root(session_manager, project_id).id
    )
    operator_inner = _pipeline(temp_db, session_manager, project_id, operator_outer.id)

    assert _allowed(session_manager, inner, WORKER, project_id) is True
    assert _allowed(session_manager, inner, OTHER, project_id) is False
    assert _allowed(session_manager, operator_inner, OTHER, project_id) is True


def test_orphan_pipeline_fails_closed(
    temp_db: HubDatabase, session_manager: SessionManager, project_id: str
) -> None:
    orphan = _pipeline(temp_db, session_manager, project_id, None)

    with pytest.raises(ValueError, match="no launching session"):
        _allowed(session_manager, orphan, WORKER, project_id)


def test_pipeline_parent_cycle_fails_closed(
    temp_db: HubDatabase, session_manager: SessionManager, project_id: str
) -> None:
    first = _pipeline(temp_db, session_manager, project_id, _root(session_manager, project_id).id)
    second = _pipeline(temp_db, session_manager, project_id, first.id)
    temp_db.execute(
        "UPDATE sessions SET parent_session_id = %s WHERE id = %s", (second.id, first.id)
    )

    with pytest.raises(ValueError, match="cycle"):
        _allowed(session_manager, second, WORKER, project_id)
