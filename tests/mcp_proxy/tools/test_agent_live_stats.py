"""MCP regressions for live agent-run activity counters."""

from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.agents import create_agents_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.database import LocalDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


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
    db: LocalDatabase,
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


@pytest.mark.asyncio
async def test_list_running_agents_includes_live_activity_counters(
    temp_db: LocalDatabase,
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
    assert result["agents"][0]["tool_calls_count"] == 22
    assert result["agents"][0]["turns_used"] == 13
