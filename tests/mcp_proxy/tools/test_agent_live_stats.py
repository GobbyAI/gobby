"""MCP regressions for live agent-run activity counters."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.events import CompletionEventRegistry
from gobby.mcp_proxy.tools import agent_live_activity
from gobby.mcp_proxy.tools.agents import create_agents_registry
from gobby.sessions.transcript_reader import TranscriptReader
from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "tool_name",
    [
        "get_agent_result",
        "get_running_agent",
        "get_agent_live_output",
        "wait_for_agent",
        "list_running_agents",
    ],
)
@pytest.mark.parametrize("state", ["stalled", "input", "missing"])
async def test_liveness_payloads_share_durable_state_despite_transcript_overlays(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tool_name: str,
    state: str,
) -> None:
    parent = _register_session(session_manager, sample_project, "projection-parent")
    child = _register_session(
        session_manager, sample_project, "projection-child", parent_session_id=parent
    )
    storage = LocalAgentRunManager(temp_db)
    run = storage.create(
        parent_session_id=parent, child_session_id=child, provider="codex", prompt="liveness"
    )
    storage.start(run.id)
    if state == "input":
        session_manager.update_session_status(child, "active")
        TurnLifecycleReducer(session_manager).enter_wait(
            child, kind="input", token="question", evidence=TurnEvidence(source="test")
        )
    elif state == "missing":
        temp_db.execute("UPDATE agent_runs SET child_session_id = NULL WHERE id = %s", (run.id,))
    baseline = datetime(2026, 1, 1, tzinfo=UTC)
    temp_db.execute("UPDATE agent_runs SET started_at = %s WHERE id = %s", (baseline, run.id))
    temp_db.execute("UPDATE sessions SET last_activity = %s WHERE id = %s", (baseline, child))
    runner = MagicMock()
    runner.run_storage = storage
    runner.get_run.side_effect = storage.get
    reader = _FakeTranscriptReader({"message_count": 100, "turn_count": 50, "tool_call_count": 80})
    registry = create_agents_registry(
        runner,
        session_manager=session_manager,
        db=temp_db,
        completion_registry=CompletionEventRegistry(),
        transcript_reader=cast(TranscriptReader, reader),
    )
    tool = registry._tools[tool_name].func
    with (
        session_context_for_test(parent),
        patch(
            "gobby.storage.agents._liveness.utc_now", return_value=baseline + timedelta(seconds=600)
        ),
    ):
        result = (
            await tool(parent_session_id=parent)
            if tool_name == "list_running_agents"
            else await tool(run.id)
        )
    assert result["success"] is True
    payload = (
        result["agents"][0]
        if tool_name == "list_running_agents"
        else result["agent"]
        if tool_name == "get_running_agent"
        else result
    )
    assert payload["last_progress_at"] == baseline.isoformat()
    assert payload["progress_age_seconds"] == 600
    assert payload["stall_suspected"] is (state != "input")
    assert payload["wait_kind"] == ("input" if state == "input" else None)
    assert payload["blocked_on_parent"] is (None if state == "missing" else False)
    if state == "missing":
        assert payload["child_status"] is None
    elif state == "input":
        assert payload["child_status"] == "awaiting_input"
    else:
        assert isinstance(payload["child_status"], str)
    persisted = session_manager.get(child)
    assert persisted is not None
    assert persisted.last_activity == baseline


@pytest.mark.asyncio
async def test_get_agent_result_result_at_is_older_than_live_progress(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    parent = _register_session(session_manager, sample_project, "result-at-parent")
    child = _register_session(
        session_manager, sample_project, "result-at-child", parent_session_id=parent
    )
    storage = LocalAgentRunManager(temp_db)
    run = storage.create(
        parent_session_id=parent, child_session_id=child, provider="codex", prompt="recency"
    )
    storage.start(run.id)
    result_at = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    live_progress = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    temp_db.execute(
        "UPDATE agent_runs SET result = %s, updated_at = %s, started_at = %s WHERE id = %s",
        ("mid-run parent note", result_at, result_at, run.id),
    )
    temp_db.execute("UPDATE sessions SET last_activity = %s WHERE id = %s", (live_progress, child))
    runner = MagicMock()
    runner.run_storage = storage
    runner.get_run.side_effect = storage.get
    registry = create_agents_registry(
        runner,
        session_manager=session_manager,
        db=temp_db,
        completion_registry=CompletionEventRegistry(),
    )
    with session_context_for_test(parent):
        payload = await registry._tools["get_agent_result"].func(run.id)

    assert payload["success"] is True
    assert payload["result"] == "mid-run parent note"
    assert payload["result_at"] == result_at.isoformat()
    assert payload["last_progress_at"] == live_progress.isoformat()
    assert payload["result_at"] != payload["last_progress_at"]


class _FakeTranscriptReader:
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts
        self.session_ids: list[str] = []

    async def get_activity_counts(self, session_id: str) -> dict[str, int]:
        self.session_ids.append(session_id)
        return self.counts


class _ExplodingTranscriptReader:
    async def get_activity_counts(self, _session_id: str) -> dict[str, int]:
        raise RuntimeError("transcript index unavailable")


@pytest.mark.asyncio
async def test_overlay_runs_live_activity_preserves_replacement_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(child_session_id="child-1")
    replacement = SimpleNamespace(child_session_id="child-1", live_message_count=2)
    transcript_reader = object()
    overlay = AsyncMock(return_value=replacement)
    monkeypatch.setattr(agent_live_activity, "overlay_live_activity", overlay)

    results = await agent_live_activity.overlay_runs_live_activity([run], transcript_reader)

    assert results == [replacement]
    overlay.assert_awaited_once_with(run, transcript_reader)


@pytest.mark.asyncio
async def test_overlay_runs_live_activity_preserves_run_on_unexpected_overlay_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = SimpleNamespace(
        child_session_id="child-1",
        parent_session_id=None,
        tool_calls_count=0,
        turns_used=0,
    )

    with caplog.at_level("WARNING", logger="gobby.mcp_proxy.tools.agent_live_activity"):
        results = await agent_live_activity.overlay_runs_live_activity(
            [run],
            _ExplodingTranscriptReader(),
        )

    assert results == [run]
    assert run.tool_calls_count == 0
    assert run.turns_used == 0
    assert "Unexpected live transcript activity overlay failure" in caplog.text


def _register_session(
    session_manager: SessionManager,
    sample_project: dict,
    external_id: str,
    *,
    parent_session_id: str | None = None,
) -> str:
    session = session_manager.register(
        external_id=external_id,
        # sample_project pins the local identity to its isolated machine (#21453).
        machine_id=require_machine_id(),
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
async def test_wait_for_agent_subscription_overlays_transcript_activity(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
) -> None:
    """wait_for_agent subscription payload includes live transcript activity."""
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
    registry = create_agents_registry(
        runner,
        session_manager=session_manager,
        db=temp_db,
        completion_registry=CompletionEventRegistry(),
        transcript_reader=transcript_reader,
    )
    wait_for_agent = registry._tools["wait_for_agent"].func

    with session_context_for_test(parent_id):
        result = await wait_for_agent(run.id)

    assert result["success"] is True
    assert result["completed"] is False
    assert result["notification_registered"] is True
    assert result["tool_calls_count"] == 17
    assert result["turns_used"] == 5
    assert transcript_reader.session_ids == [child_id]


@pytest.mark.asyncio
async def test_wait_for_agent_terminal_error_overlays_transcript_activity(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
) -> None:
    """wait_for_agent terminal payload preserves transcript activity."""
    parent_id = _register_session(session_manager, sample_project, "mcp-parent-terminal")
    child_id = _register_session(
        session_manager,
        sample_project,
        "mcp-child-terminal",
        parent_session_id=parent_id,
    )
    run_storage = LocalAgentRunManager(temp_db)
    run = run_storage.create(
        parent_session_id=parent_id,
        child_session_id=child_id,
        provider="claude",
        prompt="terminal with live counters",
        agent_name="planner",
    )
    run_storage.start(run.id)
    run_storage.fail(run.id, "idle after max reprompt attempts")

    runner = MagicMock()
    runner.run_storage = run_storage
    runner.get_run.side_effect = lambda run_id: run_storage.get(run_id)
    transcript_reader = _FakeTranscriptReader(
        {"message_count": 41, "turn_count": 5, "tool_call_count": 17}
    )
    registry = create_agents_registry(runner, transcript_reader=transcript_reader)
    wait_for_agent = registry._tools["wait_for_agent"].func

    result = await wait_for_agent(run.id)

    assert result["success"] is True
    assert result["completed"] is True
    assert result["notification_registered"] is False
    assert result["status"] == "error"
    assert result["tool_calls_count"] == 17
    assert result["turns_used"] == 5
    assert transcript_reader.session_ids == [child_id]


@pytest.mark.asyncio
async def test_get_agent_result_overlays_transcript_activity_when_persisted_counts_lag(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
) -> None:
    """Completed result payloads use transcript counts when persisted stats stayed at zero."""
    parent_id = _register_session(session_manager, sample_project, "mcp-parent-result")
    child_id = _register_session(
        session_manager,
        sample_project,
        "mcp-child-result",
        parent_session_id=parent_id,
    )
    run_storage = LocalAgentRunManager(temp_db)
    run = run_storage.create(
        parent_session_id=parent_id,
        child_session_id=child_id,
        provider="claude",
        prompt="completed result with lagging counters",
        agent_name="qa-reviewer",
    )
    run_storage.start(run.id)
    run_storage.complete(run.id, "approved")

    runner = MagicMock()
    runner.run_storage = run_storage
    runner.get_run.side_effect = lambda run_id: run_storage.get(run_id)
    transcript_reader = _FakeTranscriptReader(
        {"message_count": 58, "turn_count": 6, "tool_call_count": 24}
    )
    registry = create_agents_registry(runner, transcript_reader=transcript_reader)
    get_agent_result = registry._tools["get_agent_result"].func

    result = await get_agent_result(run.id)

    assert result["success"] is True
    assert result["status"] == "success"
    assert result["tool_calls_count"] == 24
    assert result["turns_used"] == 6
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
