"""Regression tests for live agent-run activity counters."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer, WaitKind
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.coordination_waits import CoordinationWaitManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


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
        machine_id="21000000-0000-4000-8000-000000000001",
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
        SET tool_call_count = %s, turn_count = %s
        WHERE id = %s
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
        SET tool_calls_count = %s, turns_used = %s
        WHERE id = %s
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
        "list_active": _only(agent_manager.list_active_global()),
        "list_by_parent": _only(agent_manager.list_by_parent(parent_id)),
    }

    for method, read_run in reads.items():
        assert read_run is not None, method
        assert read_run.tool_calls_count == 130, method
        assert read_run.turns_used == 78, method


def test_task_id_filters_apply_to_agent_run_list_queries(
    agent_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    sample_project: dict,
    temp_db: HubDatabase,
) -> None:
    """Task-scoped run lookups should only return matching task IDs."""
    parent_id = _register_session(session_manager, sample_project, "parent-filter")
    task_manager = LocalTaskManager(temp_db)
    included_task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Included",
        validation_criteria="Test task completion is observable.",
    )
    excluded_task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Excluded",
        validation_criteria="Test task completion is observable.",
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

    runs = agent_manager.list_active_global(task_ids=[included_task.id])

    assert [run.id for run in runs] == [included.id]
    assert [
        run.id for run in agent_manager.list_by_status("running", task_ids=[included_task.id])
    ] == [included.id]
    assert [
        run.id for run in agent_manager.list_by_parent(parent_id, task_ids=[included_task.id])
    ] == [included.id]
    assert agent_manager.list_active_global(task_ids=[]) == []
    assert agent_manager.list_by_status("running", task_ids=[]) == []
    assert agent_manager.list_by_parent(parent_id, task_ids=[]) == []


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


@pytest.fixture
def liveness_run(
    agent_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    temp_db: HubDatabase,
) -> AgentRun:
    parent = _register_session(session_manager, sample_project, "liveness-parent")
    child = _register_session(
        session_manager, sample_project, "liveness-child", parent_session_id=parent
    )
    run = agent_manager.create(
        parent_session_id=parent, child_session_id=child, provider="codex", prompt="liveness"
    )
    agent_manager.start(run.id)
    baseline = datetime(2026, 1, 1, tzinfo=UTC)
    temp_db.execute("UPDATE agent_runs SET started_at = %s WHERE id = %s", (baseline, run.id))
    temp_db.execute("UPDATE sessions SET last_activity = %s WHERE id = %s", (baseline, child))
    return run


@pytest.mark.parametrize("seconds,stalled", [(599.999, False), (600, True), (600.001, True)])
def test_liveness_exact_boundary_across_read_projections(
    agent_manager: LocalAgentRunManager,
    liveness_run: AgentRun,
    seconds: float,
    stalled: bool,
) -> None:
    baseline = datetime(2026, 1, 1, tzinfo=UTC)
    with patch(
        "gobby.storage.agents._liveness.utc_now", return_value=baseline + timedelta(seconds=seconds)
    ):
        reads = [
            agent_manager.get(liveness_run.id),
            *agent_manager.list_by_parent(liveness_run.parent_session_id),
            *agent_manager.list_active_global(),
            *agent_manager.list_by_status("running"),
        ]
    for run in reads:
        assert run is not None
        assert run.last_progress_at == baseline
        assert run.progress_age_seconds == seconds
        assert run.stall_suspected is stalled
        assert run.wait_kind is None
        assert run.blocked_on_parent is False
        for payload in (run.to_dict(), run.to_brief()):
            assert payload["last_progress_at"] == baseline.isoformat()
            assert payload["progress_age_seconds"] == seconds
            assert payload["stall_suspected"] is stalled
            assert payload["child_status"] == run.child_status


def test_only_durable_counter_growth_advances_progress(
    agent_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    temp_db: HubDatabase,
    liveness_run: AgentRun,
) -> None:
    assert liveness_run.child_session_id is not None
    child = liveness_run.child_session_id
    progress = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
    with patch("gobby.storage.sessions._bulk_update.utc_now", return_value=progress):
        session_manager.update_stats(child, message_count=8, turn_count=3, tool_call_count=4)
    with patch(
        "gobby.storage.sessions._bulk_update.utc_now", return_value=progress + timedelta(minutes=20)
    ):
        session_manager.update_stats(child, message_count=8, turn_count=3, tool_call_count=4)
        session_manager.update_stats(liveness_run.parent_session_id, tool_call_count=500)
        InterSessionMessageManager(temp_db).create_message(
            liveness_run.parent_session_id, child, "status?"
        )
        for _ in range(2):
            observed = agent_manager.get(liveness_run.id)
            assert observed is not None
            assert observed.last_progress_at == progress
    advanced = progress + timedelta(minutes=21)
    with patch("gobby.storage.sessions._bulk_update.utc_now", return_value=advanced):
        session_manager.update_stats(child, tool_call_count=5)
    observed = agent_manager.get(liveness_run.id)
    assert observed is not None
    assert observed.last_progress_at == advanced
    assert observed.tool_calls_count == 5


@pytest.mark.parametrize("kind", ["input", "approval", "handoff"])
def test_lifecycle_wait_prevents_stall(
    agent_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    liveness_run: AgentRun,
    kind: WaitKind,
) -> None:
    assert liveness_run.child_session_id is not None
    session_manager.update_session_status(liveness_run.child_session_id, "active")
    TurnLifecycleReducer(session_manager).enter_wait(
        liveness_run.child_session_id,
        kind=kind,
        token="prompt",
        evidence=TurnEvidence(source="test"),
    )
    with patch(
        "gobby.storage.agents._liveness.utc_now", return_value=datetime(2099, 1, 1, tzinfo=UTC)
    ):
        observed = agent_manager.get(liveness_run.id)
    assert observed is not None
    assert observed.wait_kind == kind
    assert observed.child_status == f"awaiting_{kind}"
    assert observed.stall_suspected is False
    assert observed.blocked_on_parent is False


@pytest.mark.parametrize("owner_is_parent", [True, False])
def test_coordination_wait_identifies_parent_and_expires(
    agent_manager: LocalAgentRunManager,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    temp_db: HubDatabase,
    liveness_run: AgentRun,
    owner_is_parent: bool,
) -> None:
    assert liveness_run.child_session_id is not None
    owner = (
        liveness_run.parent_session_id
        if owner_is_parent
        else _register_session(session_manager, sample_project, "other-owner")
    )
    wait = CoordinationWaitManager(temp_db).register(
        liveness_run.child_session_id, owner, coordination_key="release"
    )
    observed = agent_manager.get(liveness_run.id)
    assert observed is not None
    assert observed.wait_kind == "coordination"
    assert observed.blocked_on_parent is owner_is_parent
    assert observed.stall_suspected is False
    temp_db.execute(
        "UPDATE coordination_waits SET expires_at = clock_timestamp() - interval '1 second' WHERE id = %s",
        (wait["id"],),
    )
    expired = agent_manager.get(liveness_run.id)
    assert expired is not None
    assert expired.wait_kind is None
    assert expired.blocked_on_parent is False
    assert expired.stall_suspected is True


@pytest.mark.parametrize("awaiting_parent", [False, True])
def test_agent_completion_wait_ends_with_awaited_run(
    agent_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    liveness_run: AgentRun,
    awaiting_parent: bool,
) -> None:
    awaited = agent_manager.create(
        parent_session_id=liveness_run.parent_session_id,
        provider="codex",
        prompt="awaited",
        child_session_id=liveness_run.parent_session_id if awaiting_parent else None,
    )
    agent_manager.start(awaited.id)
    temp_db.execute(
        "INSERT INTO completion_subscribers (completion_id, session_id) VALUES (%s, %s)",
        (awaited.id, liveness_run.child_session_id),
    )
    observed = agent_manager.get(liveness_run.id)
    assert observed is not None
    assert observed.wait_kind == "agent"
    assert observed.blocked_on_parent is awaiting_parent
    assert observed.stall_suspected is False
    agent_manager.complete(awaited.id, result="done")
    observed = agent_manager.get(liveness_run.id)
    assert observed is not None
    assert observed.wait_kind is None
    assert observed.stall_suspected is True


def test_missing_child_keeps_run_start_baseline(
    agent_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    liveness_run: AgentRun,
) -> None:
    temp_db.execute(
        "UPDATE agent_runs SET child_session_id = NULL WHERE id = %s", (liveness_run.id,)
    )
    observed = agent_manager.get(liveness_run.id)
    assert observed is not None
    assert observed.child_status is None
    assert observed.wait_kind is None
    assert observed.blocked_on_parent is None
    assert observed.last_progress_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert observed.stall_suspected is True


def test_terminal_run_never_stalls_or_claims_later_child_activity(
    agent_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    liveness_run: AgentRun,
) -> None:
    agent_manager.complete(liveness_run.id, result="done")
    completed = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
    temp_db.execute(
        "UPDATE agent_runs SET completed_at = %s WHERE id = %s", (completed, liveness_run.id)
    )
    temp_db.execute(
        "UPDATE sessions SET last_activity = %s WHERE id = %s",
        (completed + timedelta(days=2), liveness_run.child_session_id),
    )
    observed = agent_manager.get(liveness_run.id)
    assert observed is not None
    assert observed.stall_suspected is False
    assert observed.last_progress_at == completed
