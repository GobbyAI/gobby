"""MCP regressions for live agent-run activity counters."""

from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.agents import create_agents_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


class _FakeTranscriptReader:
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts
        self.session_ids: list[str] = []

    async def get_activity_counts(self, session_id: str) -> dict[str, int]:
        self.session_ids.append(session_id)
        return self.counts


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
        SET tool_call_count = %s, turn_count = %s
        WHERE id = %s
        """,
        (tool_calls_count, turns_used, session_id),
    )


@pytest.mark.asyncio
async def test_list_running_agents_includes_live_activity_counters(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
) -> None:
    """list_running_agents returns counters from AgentRun.to_brief()."""
    parent_id = _register_session(session_manager, sample_project, "mcp-parent-live")
    child_id = _register_session(
        session_manager,
        sample_project,
        "mcp-child-live",
        parent_session_id=parent_id,
    )
    run_storage = LocalAgentRunManager(temp_db)
    run = run_storage.create(
        parent_session_id=parent_id,
        child_session_id=child_id,
        provider="claude",
        prompt="surface live counters",
        agent_name="merge-worker",
    )
    run_storage.start(run.id)
    _set_session_stats(temp_db, child_id, tool_calls_count=22, turns_used=13)

    runner = MagicMock()
    runner.run_storage = run_storage
    registry = create_agents_registry(runner)
    list_running = registry._tools["list_running_agents"].func

    result = await list_running(parent_session_id=parent_id)

    assert result["success"] is True
    assert result["count"] == 1
    assert result["agents"][0]["run_id"] == run.id
    assert result["agents"][0]["agent_name"] == "merge-worker"
    assert result["agents"][0]["tool_calls_count"] == 22
    assert result["agents"][0]["turns_used"] == 13


@pytest.mark.asyncio
async def test_list_running_agents_overlays_transcript_activity_when_session_stats_lag(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
) -> None:
    """Active agent lists use transcript counts when aggregate session stats lag."""
    parent_id = _register_session(session_manager, sample_project, "mcp-parent-transcript")
    child_id = _register_session(
        session_manager,
        sample_project,
        "mcp-child-transcript",
        parent_session_id=parent_id,
    )
    run_storage = LocalAgentRunManager(temp_db)
    run = run_storage.create(
        parent_session_id=parent_id,
        child_session_id=child_id,
        provider="claude",
        prompt="surface transcript counters",
        agent_name="planner",
    )
    run_storage.start(run.id)

    runner = MagicMock()
    runner.run_storage = run_storage
    transcript_reader = _FakeTranscriptReader(
        {"message_count": 78, "turn_count": 6, "tool_call_count": 19}
    )
    registry = create_agents_registry(runner, transcript_reader=transcript_reader)
    list_running = registry._tools["list_running_agents"].func

    result = await list_running(parent_session_id=parent_id)

    assert result["success"] is True
    assert result["agents"][0]["tool_calls_count"] == 19
    assert result["agents"][0]["turns_used"] == 6
    assert transcript_reader.session_ids == [child_id]


@pytest.mark.asyncio
async def test_wait_for_agent_timeout_overlays_transcript_activity(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
) -> None:
    """wait_for_agent timeout payload includes live transcript activity."""
    parent_id = _register_session(session_manager, sample_project, "mcp-parent-wait")
    child_id = _register_session(
        session_manager,
        sample_project,
        "mcp-child-wait",
        parent_session_id=parent_id,
    )
    run_storage = LocalAgentRunManager(temp_db)
    run = run_storage.create(
        parent_session_id=parent_id,
        child_session_id=child_id,
        provider="claude",
        prompt="wait with live counters",
        agent_name="planner",
    )
    run_storage.start(run.id)

    runner = MagicMock()
    runner.run_storage = run_storage
    runner.get_run.side_effect = lambda run_id: run_storage.get(run_id)
    transcript_reader = _FakeTranscriptReader(
        {"message_count": 41, "turn_count": 5, "tool_call_count": 17}
    )
    registry = create_agents_registry(runner, transcript_reader=transcript_reader)
    wait_for_agent = registry._tools["wait_for_agent"].func

    result = await wait_for_agent(run.id, timeout_seconds=0, poll_interval_seconds=0.1)

    assert result["success"] is True
    assert result["completed"] is False
    assert result["tool_calls_count"] == 17
    assert result["turns_used"] == 5
    assert transcript_reader.session_ids == [child_id]


@pytest.mark.asyncio
async def test_list_running_agents_default_scope_sees_non_child_runs(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
) -> None:
    """Default MCP listing is build-wide, not limited to the caller session."""
    parent_id = _register_session(session_manager, sample_project, "mcp-build-parent")
    child_id = _register_session(
        session_manager,
        sample_project,
        "mcp-build-child",
        parent_session_id=parent_id,
    )
    caller_id = _register_session(session_manager, sample_project, "mcp-monitor-caller")
    run_storage = LocalAgentRunManager(temp_db)
    run = run_storage.create(
        parent_session_id=parent_id,
        child_session_id=child_id,
        provider="claude",
        prompt="visible outside caller lineage",
        agent_name="backend-developer",
    )
    run_storage.start(run.id)

    runner = MagicMock()
    runner.run_storage = run_storage
    registry = create_agents_registry(runner)
    list_running = registry._tools["list_running_agents"].func

    with session_context_for_test(caller_id):
        result = await list_running()

    assert result["success"] is True
    assert result["scope"] == "all"
    assert result["count"] == 1
    assert result["agents"][0]["run_id"] == run.id


@pytest.mark.asyncio
async def test_list_running_agents_running_status_matches_cli_query(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
) -> None:
    """MCP status='running' returns the same run IDs as CLI --status running."""
    parent_id = _register_session(session_manager, sample_project, "mcp-cli-parent")
    caller_id = _register_session(session_manager, sample_project, "mcp-cli-caller")
    run_storage = LocalAgentRunManager(temp_db)
    pending = run_storage.create(
        parent_session_id=parent_id,
        provider="claude",
        prompt="pending run",
    )
    running = run_storage.create(
        parent_session_id=parent_id,
        provider="claude",
        prompt="running run",
    )
    run_storage.start(running.id)

    runner = MagicMock()
    runner.run_storage = run_storage
    registry = create_agents_registry(runner)
    list_running = registry._tools["list_running_agents"].func

    with session_context_for_test(caller_id):
        result = await list_running(status="running")

    cli_run_ids = [run.id for run in run_storage.list_running()]
    mcp_run_ids = [agent["run_id"] for agent in result["agents"]]

    assert pending.id not in mcp_run_ids
    assert mcp_run_ids == cli_run_ids == [running.id]
