"""Regression tests for live agent-run activity counters."""

import pytest

from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


@pytest.fixture
def agent_manager(temp_db: HubDatabase) -> LocalAgentRunManager:
    """Create an agent run manager with temp database."""
    return LocalAgentRunManager(temp_db)


def _register_session(
    session_manager: SessionManager,
    sample_project: dict,
    external_id: str,
    *,
    parent_session_id: str | None = None,
) -> str:
    session = session_manager.register(
        external_id=external_id,
        machine_id="machine-1",
        source="claude",
        project_id=sample_project["id"],
        parent_session_id=parent_session_id,
    )
    return session.id


def _set_session_stats(
    db: HubDatabase,
    session_id: str,
    *,
    tool_calls_count: int,
    turns_used: int,
) -> None:
    db.execute(
        """
        UPDATE sessions
        SET tool_call_count = ?, turn_count = ?
        WHERE id = ?
        """,
        (tool_calls_count, turns_used, session_id),
    )


def _set_persisted_run_stats(
    db: HubDatabase,
    run_id: str,
    *,
    tool_calls_count: int,
    turns_used: int,
) -> None:
    db.execute(
        """
        UPDATE agent_runs
        SET tool_calls_count = ?, turns_used = ?
        WHERE id = ?
        """,
        (tool_calls_count, turns_used, run_id),
    )


def _only(runs: list[AgentRun]) -> AgentRun:
    assert len(runs) == 1
    return runs[0]


def test_active_read_methods_use_child_session_stats(
    agent_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    sample_project: dict,
    temp_db: HubDatabase,
) -> None:
    """Active run reads return live child-session counters over persisted zeros."""
    parent_id = _register_session(session_manager, sample_project, "parent-live")
    child_id = _register_session(
        session_manager,
        sample_project,
        "child-live",
        parent_session_id=parent_id,
    )
    run = agent_manager.create(
        parent_session_id=parent_id,
        child_session_id=child_id,
        provider="claude",
        prompt="track live stats",
    )
    agent_manager.start(run.id)
    _set_session_stats(temp_db, child_id, tool_calls_count=130, turns_used=78)

    reads = {
        "get": agent_manager.get(run.id),
        "list_by_session": _only(agent_manager.list_by_session(parent_id)),
        "list_by_status": _only(agent_manager.list_by_status("running")),
        "list_running": _only(agent_manager.list_running()),
        "list_active": _only(agent_manager.list_active()),
        "list_by_parent": _only(agent_manager.list_by_parent(parent_id)),
    }

    for method, read_run in reads.items():
        assert read_run is not None, method
        assert read_run.tool_calls_count == 130, method
        assert read_run.turns_used == 78, method


def test_list_active_filters_by_task_ids_in_sql(
    agent_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    sample_project: dict,
    temp_db: HubDatabase,
) -> None:
    """Task-scoped active-run lookup should only return matching task IDs."""
    parent_id = _register_session(session_manager, sample_project, "parent-filter")
    task_manager = LocalTaskManager(temp_db)
    included_task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Included",
    )
    excluded_task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Excluded",
    )
    included = agent_manager.create(
        parent_session_id=parent_id,
        provider="claude",
        prompt="included",
        task_id=included_task.id,
    )
    excluded = agent_manager.create(
        parent_session_id=parent_id,
        provider="claude",
        prompt="excluded",
        task_id=excluded_task.id,
    )
    agent_manager.start(included.id)
    agent_manager.start(excluded.id)

    runs = agent_manager.list_active(task_ids=[included_task.id])

    assert [run.id for run in runs] == [included.id]
    assert agent_manager.list_active(task_ids=[]) == []


def test_active_run_without_child_session_uses_parent_session_stats(
    agent_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    sample_project: dict,
    temp_db: HubDatabase,
) -> None:
    """Active runs fall back to parent-session counters when no child row exists."""
    parent_id = _register_session(session_manager, sample_project, "parent-fallback")
    run = agent_manager.create(
        parent_session_id=parent_id,
        provider="claude",
        prompt="no child session yet",
    )
    agent_manager.start(run.id)
    _set_persisted_run_stats(temp_db, run.id, tool_calls_count=1, turns_used=1)
    _set_session_stats(temp_db, parent_id, tool_calls_count=9, turns_used=4)

    retrieved = agent_manager.get(run.id)

    assert retrieved is not None
    assert retrieved.tool_calls_count == 9
    assert retrieved.turns_used == 4


def test_terminal_run_without_child_session_keeps_persisted_stats(
    agent_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    sample_project: dict,
    temp_db: HubDatabase,
) -> None:
    """Terminal history remains based on persisted agent_runs counters."""
    parent_id = _register_session(session_manager, sample_project, "parent-terminal")
    run = agent_manager.create(
        parent_session_id=parent_id,
        provider="claude",
        prompt="terminal history",
    )
    agent_manager.start(run.id)
    agent_manager.complete(run.id, result="done", tool_calls_count=11, turns_used=6)
    _set_session_stats(temp_db, parent_id, tool_calls_count=99, turns_used=88)

    retrieved = agent_manager.get(run.id)
    listed = _only(agent_manager.list_by_status("success"))

    assert retrieved is not None
    assert retrieved.tool_calls_count == 11
    assert retrieved.turns_used == 6
    assert listed.tool_calls_count == 11
    assert listed.turns_used == 6


def test_to_brief_includes_activity_counters(
    agent_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    sample_project: dict,
    temp_db: HubDatabase,
) -> None:
    """Brief agent-run payloads include the derived activity counters."""
    parent_id = _register_session(session_manager, sample_project, "parent-brief")
    child_id = _register_session(
        session_manager,
        sample_project,
        "child-brief",
        parent_session_id=parent_id,
    )
    run = agent_manager.create(
        parent_session_id=parent_id,
        child_session_id=child_id,
        provider="claude",
        prompt="brief stats",
    )
    agent_manager.start(run.id)
    _set_session_stats(temp_db, child_id, tool_calls_count=7, turns_used=3)

    brief = agent_manager.get(run.id).to_brief()

    assert brief["tool_calls_count"] == 7
    assert brief["turns_used"] == 3


def test_to_brief_includes_agent_identity(
    agent_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    sample_project: dict,
) -> None:
    """Brief agent-run payloads expose enough identity for orchestration filters."""
    parent_id = _register_session(session_manager, sample_project, "parent-identity")
    child_id = _register_session(
        session_manager,
        sample_project,
        "child-identity",
        parent_session_id=parent_id,
    )
    run = agent_manager.create(
        parent_session_id=parent_id,
        child_session_id=child_id,
        provider="claude",
        prompt="identity",
        workflow_name="merge-orchestrator",
        agent_name="merge-worker",
        model="sonnet",
    )

    brief = agent_manager.get(run.id).to_brief()

    assert brief["agent_name"] == "merge-worker"
    assert brief["workflow_name"] == "merge-orchestrator"
    assert brief["model"] == "sonnet"
