from datetime import UTC, datetime
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.mcp_proxy.wait_tools import MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS
from gobby.sessions.transcript_reader import TranscriptReader
from gobby.sessions.transcript_renderer import ContentBlock, RenderedMessage
from gobby.sessions.transcript_window import WindowResult
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_session_manager():
    manager = MagicMock(spec=SessionManager)
    # resolve_session_reference returns input unchanged by default
    manager.resolve_session_reference = MagicMock(side_effect=lambda ref, project_id=None: ref)
    return manager


@pytest.fixture
def mock_transcript_reader():
    reader = MagicMock(spec=TranscriptReader)
    reader.get_rendered_window = AsyncMock()
    reader.count_messages = AsyncMock()
    return reader


def _window(
    groups: list[Any],
    *,
    parsed_message_count: int | None = None,
) -> WindowResult:
    """Build a WindowResult for a head-order page of rendered groups."""
    return WindowResult(
        groups=list(groups),
        returned_count=len(groups),
        total_groups=len(groups),
        parsed_message_count=len(groups) if parsed_message_count is None else parsed_message_count,
    )


@pytest.fixture
def renderer_registry(mock_transcript_reader):
    """Registry with transcript_reader (primary renderer path)."""
    return create_session_messages_registry(transcript_reader=mock_transcript_reader)


@pytest.fixture
def full_sessions_registry(mock_session_manager):
    """Registry with session manager."""
    return create_session_messages_registry(
        session_manager=mock_session_manager,
    )


def test_create_session_messages_registry_returns_registry(renderer_registry) -> None:
    """Test that create_session_messages_registry returns an InternalToolRegistry."""
    assert isinstance(renderer_registry, InternalToolRegistry)
    assert renderer_registry.name == "gobby-sessions"


@pytest.mark.asyncio
async def test_get_session_messages_renderer_path(mock_transcript_reader, renderer_registry):
    """Test get_session_messages uses transcript_reader.get_rendered_messages when available."""
    rendered = RenderedMessage(
        id="msg-1",
        role="assistant",
        content="Hello world",
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        content_blocks=[ContentBlock(type="text", content="Hello world")],
    )
    mock_transcript_reader.get_rendered_window.return_value = _window(
        [rendered], parsed_message_count=1
    )

    result = await renderer_registry.call(
        "get_session_messages", {"session_id": "sess-123", "limit": 10, "offset": 0}
    )

    mock_transcript_reader.get_rendered_window.assert_called_with(
        session_id="sess-123", limit=10, offset=0, order="head"
    )
    assert result["success"] is True
    assert result["total_count"] == 1
    assert len(result["messages"]) == 1
    msg = result["messages"][0]
    assert msg["role"] == "assistant"
    assert msg["content_blocks"][0]["type"] == "text"
    assert msg["content_blocks"][0]["content"] == "Hello world"


@pytest.mark.asyncio
async def test_get_session_messages_renderer_keeps_content_blocks(
    mock_transcript_reader, renderer_registry
):
    """Content blocks stay complete when full_content=False."""
    long_text = "x" * 1000
    rendered = RenderedMessage(
        id="msg-1",
        role="assistant",
        content=long_text,
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        content_blocks=[ContentBlock(type="text", content=long_text)],
    )
    mock_transcript_reader.get_rendered_window.return_value = _window(
        [rendered], parsed_message_count=1
    )

    result = await renderer_registry.call(
        "get_session_messages",
        {"session_id": "sess-123", "limit": 10, "offset": 0, "full_content": False},
    )

    assert result["success"] is True
    msg = result["messages"][0]
    assert msg["content"] == long_text
    assert msg["content_blocks"][0]["content"] == long_text


@pytest.mark.asyncio
async def test_get_session_messages_renderer_full_content(
    mock_transcript_reader, renderer_registry
):
    """Test that content_blocks are NOT truncated when full_content=True."""
    long_text = "x" * 1000
    rendered = RenderedMessage(
        id="msg-1",
        role="assistant",
        content=long_text,
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        content_blocks=[ContentBlock(type="text", content=long_text)],
    )
    mock_transcript_reader.get_rendered_window.return_value = _window(
        [rendered], parsed_message_count=1
    )

    result = await renderer_registry.call(
        "get_session_messages",
        {"session_id": "sess-123", "limit": 10, "offset": 0, "full_content": True},
    )

    assert result["success"] is True
    msg = result["messages"][0]
    assert msg["content"] == long_text
    assert msg["content_blocks"][0]["content"] == long_text


def test_registry_without_managers_has_no_message_tools():
    """Test that registry with no message_manager or transcript_reader has no message tools."""
    registry = create_session_messages_registry()
    tools_list = registry.list_tools()
    tool_names = [t["name"] for t in tools_list]
    assert "get_session_messages" not in tool_names
    assert "search_session_messages" not in tool_names
    assert "search_messages" not in tool_names


# --- Session CRUD Tool Tests ---


def test_full_registry_has_session_tools(full_sessions_registry) -> None:
    """Test that full registry has all session and handoff tools."""
    expected_tools = [
        "get_session",
        "list_sessions",
        "session_stats",
        "get_handoff_context",
        "set_handoff_context",
        "wait_for_summary",
        "get_session_commits",
    ]

    tools_list = full_sessions_registry.list_tools()
    tool_names = [t["name"] for t in tools_list]

    for tool_name in expected_tools:
        assert tool_name in tool_names, f"Missing tool: {tool_name}"


def test_registry_without_session_manager_lacks_crud_tools(renderer_registry) -> None:
    """Test that registry without session_manager doesn't have CRUD tools."""
    tools_list = renderer_registry.list_tools()
    tool_names = [t["name"] for t in tools_list]

    # Should have message tools (via transcript_reader)
    assert "get_session_messages" in tool_names
    assert "search_session_messages" in tool_names
    assert "search_messages" not in tool_names

    # Should NOT have session CRUD tools
    assert "get_session" not in tool_names
    assert "list_sessions" not in tool_names


def _make_mock_session(session_id: str = "sess-123", **kwargs) -> MagicMock:
    """Helper to create a mock Session object."""
    session = MagicMock(spec=Session)
    session.id = session_id
    session.to_dict.return_value = {
        "id": session_id,
        "status": kwargs.get("status", "active"),
        "source": kwargs.get("source", "claude_code"),
        "project_id": kwargs.get("project_id", "proj-123"),
        "title": kwargs.get("title"),
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:00:00Z",
    }
    return session


@pytest.mark.asyncio
async def test_get_session(mock_session_manager, full_sessions_registry):
    """Test get_session tool execution."""
    mock_session = _make_mock_session("sess-abc")
    mock_session_manager.resolve_session_reference.return_value = "sess-abc"
    mock_session_manager.get.return_value = mock_session

    result = await full_sessions_registry.call("get_session", {"session_id": "sess-abc"})

    mock_session_manager.resolve_session_reference.assert_called_with("sess-abc", ANY)
    mock_session_manager.get.assert_called_with("sess-abc")
    assert result["found"] is True
    assert result["id"] == "sess-abc"


@pytest.mark.asyncio
async def test_get_session_hydrates_task_refs(mock_session_manager, full_sessions_registry):
    """Task refs come from authoritative task attribution for the resolved session."""
    resolved_id = "019fac20-e6fa-7001-87e1-935703260d95"
    mock_session = _make_mock_session(resolved_id)
    mock_session_manager.resolve_session_reference.return_value = resolved_id
    mock_session_manager.get.return_value = mock_session
    mock_session_manager.fetch_task_refs_by_session.return_value = {
        resolved_id: {
            "claimed": [12, 34],
            "created": [56],
            "closed": [78, 90],
        }
    }

    result = await full_sessions_registry.call("get_session", {"session_id": "#9846"})

    mock_session_manager.fetch_task_refs_by_session.assert_called_once_with([resolved_id])
    assert result["claimed_task_refs"] == [12, 34]
    assert result["created_task_refs"] == [56]
    assert result["closed_task_refs"] == [78, 90]


@pytest.mark.asyncio
async def test_get_session_not_found(mock_session_manager, full_sessions_registry):
    """Test get_session returns error when not found."""
    mock_session_manager.resolve_session_reference.side_effect = ValueError("Not found")
    mock_session_manager.get.return_value = None
    mock_session_manager.list.return_value = []

    result = await full_sessions_registry.call("get_session", {"session_id": "nonexistent"})

    assert "error" in result
    assert result["found"] is False


@pytest.mark.asyncio
async def test_get_session_prefix_match(mock_session_manager, full_sessions_registry):
    """Test get_session supports prefix matching."""
    mock_session = _make_mock_session("sess-abc123")
    # resolve_session_reference handles prefix matching and returns the full ID
    mock_session_manager.resolve_session_reference.return_value = "sess-abc123"
    mock_session_manager.get.return_value = mock_session

    result = await full_sessions_registry.call("get_session", {"session_id": "sess-abc"})

    assert result["found"] is True
    assert result["id"] == "sess-abc123"


@pytest.mark.asyncio
async def test_list_sessions(mock_session_manager, full_sessions_registry):
    """Test list_sessions tool execution."""
    mock_sessions = [
        _make_mock_session("sess-1"),
        _make_mock_session("sess-2"),
    ]
    mock_session_manager.list.return_value = mock_sessions
    mock_session_manager.count.return_value = 2

    result = await full_sessions_registry.call("list_sessions", {"limit": 10})

    mock_session_manager.list.assert_called_with(
        project_id=None, status=None, source=None, machine_id=None, limit=10
    )
    assert result["count"] == 2
    assert result["total"] == 2
    assert len(result["sessions"]) == 2


@pytest.mark.asyncio
async def test_list_sessions_with_filters(mock_session_manager, full_sessions_registry):
    """Test list_sessions with status and source filters."""
    mock_session_manager.list.return_value = []
    mock_session_manager.count.return_value = 0

    result = await full_sessions_registry.call(
        "list_sessions",
        {"status": "active", "source": "claude_code", "project_id": "proj-123"},
    )

    mock_session_manager.list.assert_called_with(
        project_id="proj-123",
        status="active",
        source="claude_code",
        machine_id=None,
        limit=20,
    )
    assert result["filters"]["status"] == "active"
    assert result["filters"]["source"] == "claude_code"


@pytest.mark.asyncio
async def test_session_stats(mock_session_manager, full_sessions_registry):
    """Test session_stats tool execution."""
    mock_session_manager.count.return_value = 10
    mock_session_manager.count_by_status.return_value = {
        "active": 3,
        "expired": 7,
    }

    result = await full_sessions_registry.call("session_stats", {})

    assert result["total"] == 10
    assert result["by_status"]["active"] == 3
    assert result["by_status"]["expired"] == 7
    mock_session_manager.count_by_status.assert_called_once_with(project_id=None)


@pytest.mark.asyncio
async def test_session_stats_scopes_project_aggregates(
    mock_session_manager, full_sessions_registry
):
    """Test session_stats forwards project filters to every aggregate."""

    def count(project_id=None, status=None, source=None):
        if source == "claude":
            return 2
        if source == "qwen":
            return 1
        if source:
            return 0
        return 4

    mock_session_manager.count.side_effect = count
    mock_session_manager.count_by_status.return_value = {
        "active": 3,
        "paused": 1,
    }

    result = await full_sessions_registry.call("session_stats", {"project_id": "proj-123"})

    assert result["total"] == 4
    assert result["by_status"] == {"active": 3, "paused": 1}
    assert result["by_source"] == {"claude": 2, "qwen": 1}
    mock_session_manager.count_by_status.assert_called_once_with(project_id="proj-123")
    assert mock_session_manager.count.call_args_list[0].kwargs == {"project_id": "proj-123"}
    for call in mock_session_manager.count.call_args_list[1:]:
        assert call.kwargs["project_id"] == "proj-123"


@pytest.mark.asyncio
async def test_session_stats_counts_droid_source(mock_session_manager, full_sessions_registry):
    """Test session_stats includes droid in by_source."""

    def count(project_id=None, status=None, source=None):
        if source == "droid":
            return 4
        if source:
            return 0
        return 4

    mock_session_manager.count.side_effect = count
    mock_session_manager.count_by_status.return_value = {"active": 4}

    result = await full_sessions_registry.call("session_stats", {})

    assert result["total"] == 4
    assert result["by_source"] == {"droid": 4}


# --- Handoff Tool Tests ---


@pytest.mark.asyncio
async def test_get_handoff_context_by_session_id(mock_session_manager, full_sessions_registry):
    """Test get_handoff_context tool returns summary_markdown preferentially."""
    mock_session = _make_mock_session("sess-abc")
    mock_session.summary_markdown = "## Summary\n\nTest handoff content"
    mock_session.title = "Test Session"
    mock_session.status = "handoff_ready"
    mock_session_manager.resolve_session_reference.return_value = "sess-abc"
    mock_session_manager.get.return_value = mock_session

    result = await full_sessions_registry.call("get_handoff_context", {"session_id": "sess-abc"})

    mock_session_manager.resolve_session_reference.assert_called_with("sess-abc", ANY)
    mock_session_manager.get.assert_called_with("sess-abc")
    assert result["session_id"] == "sess-abc"
    assert result["has_context"] is True
    assert "Test handoff content" in result["context"]
    assert result["context_type"] == "summary_markdown"
    assert result.get("stale") is False


@pytest.mark.asyncio
async def test_get_handoff_context_preserves_oversized_precompact_summary(
    mock_session_manager: MagicMock,
    full_sessions_registry: InternalToolRegistry,
) -> None:
    summary = "# Latest pre-compact summary\n\n" + "\n".join(["complete fact"] * 3_000)
    mock_session = _make_mock_session("sess-oversized")
    mock_session.summary_markdown = summary
    mock_session.summary_digest_turn_count = 1
    mock_session.digest_markdown = "### Turn 1\nComplete compact-triggering turn"
    mock_session.last_turn_markdown = "older observer-only fallback"
    mock_session.status = "handoff_ready"
    mock_session_manager.resolve_session_reference.return_value = "sess-oversized"
    mock_session_manager.get.return_value = mock_session

    result = await full_sessions_registry.call(
        "get_handoff_context",
        {"session_id": "sess-oversized"},
    )

    assert result["context"] == summary
    assert result["context_type"] == "summary_markdown"
    assert result.get("stale") is False


@pytest.mark.asyncio
async def test_get_handoff_context_same_session_returns_latest_precompact_summary(
    mock_session_manager: MagicMock,
    full_sessions_registry: InternalToolRegistry,
) -> None:
    summary = "latest exact pre-compact revision"
    mock_session = _make_mock_session("sess-self")
    mock_session.summary_markdown = summary
    mock_session.summary_digest_turn_count = 1
    mock_session.digest_markdown = "### Turn 1\nLatest exact turn"
    mock_session.last_turn_markdown = "older observer-only fallback"
    mock_session.status = "active"
    mock_session_manager.resolve_session_reference.return_value = "sess-self"
    mock_session_manager.get.return_value = mock_session

    with session_context_for_test("sess-self"):
        result = await full_sessions_registry.call(
            "get_handoff_context",
            {"session_id": "sess-self"},
        )

    assert result["context"] == summary
    assert result.get("stale") is False


@pytest.mark.asyncio
async def test_get_handoff_context_rejects_stale_last_turn_fallback(
    mock_session_manager: MagicMock,
    full_sessions_registry: InternalToolRegistry,
) -> None:
    summary = "complete latest summary revision"
    mock_session = _make_mock_session("sess-fresh")
    mock_session.summary_markdown = summary
    mock_session.summary_digest_turn_count = 2
    mock_session.digest_markdown = "### Turn 1\nOne\n\n### Turn 2\nTwo"
    mock_session.last_turn_markdown = "older observer-only fallback"
    mock_session.status = "handoff_ready"
    mock_session_manager.resolve_session_reference.return_value = "sess-fresh"
    mock_session_manager.get.return_value = mock_session

    result = await full_sessions_registry.call(
        "get_handoff_context",
        {"session_id": "sess-fresh"},
    )

    assert result["context"] == summary
    assert "older observer-only fallback" not in result["context"]
    assert result.get("stale") is False


async def test_get_handoff_context_appends_digest_tail_when_summary_lags_digest(
    mock_session_manager, full_sessions_registry
):
    mock_session = _make_mock_session("sess-abc")
    mock_session.summary_markdown = "## Current State\nRound 13 repairs"
    mock_session.digest_markdown = "### Turn 1\nRound 13\n\n### Turn 2\nRound 14 wait"
    mock_session.summary_digest_turn_count = 1
    mock_session.last_turn_markdown = "Round 14 is still active and the child summary is stale."
    mock_session.last_assistant_content = None
    mock_session.title = "Round Fourteen Review Monitoring"
    mock_session.status = "handoff_ready"
    mock_session_manager.resolve_session_reference.return_value = "sess-abc"
    mock_session_manager.get.return_value = mock_session

    result = await full_sessions_registry.call("get_handoff_context", {"session_id": "sess-abc"})

    assert result["success"] is True
    assert result["stale"] is True
    assert result["context_type"] == "summary_with_digest_tail"
    assert result["context"].startswith("## Current State\nRound 13 repairs")
    assert "### Turn 2\nRound 14 wait" in result["context"]
    assert "Round 14 is still active" not in result["context"]


@pytest.mark.asyncio
async def test_get_handoff_context_expired_session_id_fails_closed(
    mock_session_manager, full_sessions_registry
):
    """Explicit session lookup must not serve stale non-handoff summaries."""
    mock_session = _make_mock_session("sess-expired", status="expired")
    mock_session.summary_markdown = "## Stale Context"
    mock_session.status = "expired"
    mock_session_manager.resolve_session_reference.return_value = "sess-expired"
    mock_session_manager.get.return_value = mock_session

    result = await full_sessions_registry.call(
        "get_handoff_context", {"session_id": "sess-expired"}
    )

    assert result["success"] is False
    assert result["found"] is False
    mock_session_manager.find_parent.assert_not_called()
    mock_session_manager.list.assert_not_called()


@pytest.mark.asyncio
async def test_get_handoff_context_bound_successor_reads_expired_predecessor(
    mock_session_manager: MagicMock, full_sessions_registry: InternalToolRegistry
) -> None:
    """A bound successor may read its direct predecessor after expiry."""
    predecessor = _make_mock_session("sess-expired", status="expired", project_id="proj-123")
    predecessor.summary_markdown = "## Clear Handoff\nContinue from the staged handoff."
    predecessor.status = "expired"
    predecessor.project_id = "proj-123"
    successor = _make_mock_session("sess-successor", project_id="proj-123")
    successor.parent_session_id = "sess-expired"
    successor.project_id = "proj-123"
    mock_session_manager.resolve_session_reference.return_value = "sess-expired"

    def _get(session_id: str) -> MagicMock | None:
        if session_id == "sess-expired":
            return predecessor
        if session_id == "sess-successor":
            return successor
        return None

    mock_session_manager.get.side_effect = _get

    with (
        session_context_for_test("sess-successor"),
        patch(
            "gobby.mcp_proxy.tools.sessions._handoff.get_project_context",
            return_value={"id": "proj-123"},
        ),
    ):
        result = await full_sessions_registry.call(
            "get_handoff_context", {"session_id": "sess-expired"}
        )

    assert result["success"] is True
    assert result["found"] is True
    assert result["context"] == "## Clear Handoff\nContinue from the staged handoff."
    assert result["session_id"] == "sess-expired"


@pytest.mark.asyncio
async def test_get_handoff_context_no_summary_returns_no_context(
    mock_session_manager, full_sessions_registry
):
    """Test get_handoff_context returns has_context=False when summary_markdown is None.

    compact_markdown fallback was removed in migration 163.
    """
    mock_session = _make_mock_session("sess-abc")
    mock_session.summary_markdown = None
    mock_session.title = "Test"
    mock_session.status = "handoff_ready"
    mock_session_manager.resolve_session_reference.return_value = "sess-abc"
    mock_session_manager.get.return_value = mock_session

    result = await full_sessions_registry.call("get_handoff_context", {"session_id": "sess-abc"})

    assert result["has_context"] is False


@pytest.mark.asyncio
async def test_get_handoff_context_not_found(mock_session_manager, full_sessions_registry):
    """Test get_handoff_context when session not found."""
    mock_session_manager.resolve_session_reference.side_effect = ValueError("Not found")
    mock_session_manager.get.return_value = None

    result = await full_sessions_registry.call("get_handoff_context", {"session_id": "nonexistent"})

    assert "error" in result
    assert result["success"] is False


@pytest.mark.asyncio
async def test_get_handoff_context_no_context(mock_session_manager, full_sessions_registry):
    """Test get_handoff_context when session has no handoff context."""
    mock_session = _make_mock_session("sess-abc", status="handoff_ready")
    mock_session.summary_markdown = None
    mock_session.status = "handoff_ready"
    mock_session_manager.resolve_session_reference.return_value = "sess-abc"
    mock_session_manager.get.return_value = mock_session

    result = await full_sessions_registry.call("get_handoff_context", {"session_id": "sess-abc"})

    assert result["has_context"] is False
    assert "no handoff context" in result["message"]


@pytest.mark.asyncio
async def test_get_handoff_context_most_recent(mock_session_manager, full_sessions_registry):
    """Test get_handoff_context finds most recent handoff_ready session."""
    mock_session = _make_mock_session("sess-recent", status="handoff_ready")
    mock_session.summary_markdown = "## Recent Context"
    mock_session.title = "Recent Session"
    mock_session.status = "handoff_ready"
    mock_session_manager.find_parent.return_value = mock_session

    with patch(
        "gobby.utils.machine_id.get_machine_id",
        return_value="21000000-0000-4000-8000-000000000001",
    ):
        result = await full_sessions_registry.call(
            "get_handoff_context", {"project_id": "proj-123"}
        )

    mock_session_manager.find_parent.assert_called_once()
    mock_session_manager.list.assert_not_called()
    assert result["found"] is True
    assert result["session_id"] == "sess-recent"


@pytest.mark.asyncio
async def test_get_handoff_context_links_child(mock_session_manager, full_sessions_registry):
    """Test get_handoff_context can link a child session to the parent."""
    mock_session = _make_mock_session("sess-parent", status="handoff_ready")
    mock_session.summary_markdown = "## Context"
    mock_session.title = "Parent"
    mock_session.status = "handoff_ready"
    mock_session.project_id = "proj-123"
    child_session = _make_mock_session("sess-child", project_id="proj-123")
    child_session.project_id = "proj-123"
    mock_session_manager.get.side_effect = [mock_session, child_session]

    with patch(
        "gobby.mcp_proxy.tools.sessions._handoff.get_project_context",
        return_value=None,
    ):
        result = await full_sessions_registry.call(
            "get_handoff_context",
            {"session_id": "sess-parent", "link_child_session_id": "sess-child"},
        )

    mock_session_manager.update_parent_session_id.assert_called_with("sess-child", "sess-parent")
    assert result["linked_child"] == "sess-child"


@pytest.mark.asyncio
async def test_get_handoff_context_no_session_found(mock_session_manager, full_sessions_registry):
    """Test get_handoff_context when no handoff_ready session exists."""
    mock_session_manager.resolve_session_reference.return_value = "nonexistent"
    mock_session_manager.get.return_value = None

    result = await full_sessions_registry.call("get_handoff_context", {"session_id": "nonexistent"})

    assert result["success"] is False
    mock_session_manager.find_parent.assert_not_called()
    mock_session_manager.list.assert_not_called()


@pytest.mark.asyncio
async def test_wait_for_summary_returns_ready_context(mock_session_manager, full_sessions_registry):
    """wait_for_summary returns context once summary_markdown is available."""
    empty_session = _make_mock_session("sess-wait")
    empty_session.summary_markdown = ""
    ready_session = _make_mock_session("sess-wait")
    ready_session.summary_markdown = "## Summary\n\nReady now"
    mock_session_manager.resolve_session_reference.return_value = "sess-wait"
    mock_session_manager.get.side_effect = [empty_session, ready_session]

    result = await full_sessions_registry.call(
        "wait_for_summary",
        {
            "session_id": "sess-wait",
            "timeout_seconds": 0.05,
            "poll_interval_seconds": 0.001,
        },
    )

    mock_session_manager.resolve_session_reference.assert_called_with("sess-wait", ANY)
    assert mock_session_manager.get.call_count == 2
    assert result == {
        "success": True,
        "completed": True,
        "session_id": "sess-wait",
        "has_context": True,
        "context": "## Summary\n\nReady now",
        "context_type": "summary_markdown",
    }


async def test_wait_for_summary_appends_digest_tail_when_summary_lags_digest(
    mock_session_manager, full_sessions_registry
):
    stale_session = _make_mock_session("sess-wait")
    stale_session.summary_markdown = "## Current State\nRound 13 repairs"
    stale_session.digest_markdown = "### Turn 1\nRound 13\n\n### Turn 2\nRound 14 wait"
    stale_session.summary_digest_turn_count = 1
    stale_session.last_turn_markdown = "Round 14 is still active and the child summary is stale."
    stale_session.last_assistant_content = None
    mock_session_manager.resolve_session_reference.return_value = "sess-wait"
    mock_session_manager.get.return_value = stale_session

    result = await full_sessions_registry.call(
        "wait_for_summary",
        {
            "session_id": "sess-wait",
            "timeout_seconds": 0.05,
            "poll_interval_seconds": 0.001,
        },
    )

    assert result == {
        "success": True,
        "completed": True,
        "session_id": "sess-wait",
        "has_context": True,
        "context": (
            "## Current State\nRound 13 repairs\n\n"
            "## Digest turns since this summary\n\n"
            "### Turn 2\nRound 14 wait"
        ),
        "context_type": "summary_with_digest_tail",
        "stale": True,
    }


@pytest.mark.asyncio
async def test_wait_for_summary_times_out(mock_session_manager, full_sessions_registry):
    """wait_for_summary returns completed=false when summary stays empty."""
    session = _make_mock_session("sess-empty")
    session.summary_markdown = ""
    mock_session_manager.resolve_session_reference.return_value = "sess-empty"
    mock_session_manager.get.return_value = session

    result = await full_sessions_registry.call(
        "wait_for_summary",
        {
            "session_id": "sess-empty",
            "timeout_seconds": 0,
            "poll_interval_seconds": 0.001,
        },
    )

    assert result == {
        "success": True,
        "completed": False,
        "session_id": "sess-empty",
        "timeout_seconds": 0.0,
    }


@pytest.mark.asyncio
async def test_wait_for_summary_clamps_timeout_to_wrapper_limit(
    mock_session_manager: MagicMock,
    full_sessions_registry: InternalToolRegistry,
) -> None:
    session = _make_mock_session("sess-empty")
    session.summary_markdown = ""
    mock_session_manager.resolve_session_reference.return_value = "sess-empty"
    mock_session_manager.get.return_value = session
    loop = MagicMock()
    loop.time.side_effect = [0.0, MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS]

    with patch(
        "gobby.mcp_proxy.tools.sessions._handoff.asyncio.get_running_loop",
        return_value=loop,
    ):
        result = await full_sessions_registry.call(
            "wait_for_summary",
            {
                "session_id": "sess-empty",
                "timeout_seconds": MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS + 1,
            },
        )

    assert result["timeout_seconds"] == MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_wait_for_summary_missing_session(mock_session_manager, full_sessions_registry):
    """wait_for_summary reports unresolved sessions as missing."""
    mock_session_manager.resolve_session_reference.side_effect = ValueError("Not found")

    result = await full_sessions_registry.call(
        "wait_for_summary",
        {"session_id": "missing", "timeout_seconds": 0},
    )

    assert result["success"] is False
    assert result["completed"] is False
    assert result["found"] is False
    assert result["session_id"] == "missing"
    assert result["error"] == "Not found"


@pytest.mark.asyncio
async def test_wait_for_summary_missing_resolved_session(
    mock_session_manager, full_sessions_registry
):
    """wait_for_summary reports a resolved ref whose session row is absent."""
    mock_session_manager.resolve_session_reference.side_effect = None
    mock_session_manager.resolve_session_reference.return_value = "missing-uuid"
    mock_session_manager.get.return_value = None

    result = await full_sessions_registry.call(
        "wait_for_summary",
        {"session_id": "#77", "timeout_seconds": 0},
    )

    assert result == {
        "success": False,
        "completed": False,
        "found": False,
        "session_id": "missing-uuid",
        "error": "Session #77 not found",
    }


@pytest.mark.asyncio
async def test_wait_for_summary_resolves_seq_ref(mock_session_manager, full_sessions_registry):
    """wait_for_summary resolves #N refs before polling."""
    session = _make_mock_session("uuid-42")
    session.summary_markdown = "Resolved context"
    mock_session_manager.resolve_session_reference.side_effect = None
    mock_session_manager.resolve_session_reference.return_value = "uuid-42"
    mock_session_manager.get.return_value = session

    result = await full_sessions_registry.call(
        "wait_for_summary",
        {"session_id": "#42", "timeout_seconds": 0},
    )

    mock_session_manager.resolve_session_reference.assert_called_with("#42", ANY)
    mock_session_manager.get.assert_called_with("uuid-42")
    assert result["success"] is True
    assert result["completed"] is True
    assert result["session_id"] == "uuid-42"
    assert result["context"] == "Resolved context"


@pytest.mark.asyncio
async def test_set_handoff_context_no_session(mock_session_manager, full_sessions_registry):
    """Test set_handoff_context when no session is found."""
    mock_session_manager.get.return_value = None
    mock_session_manager.list.return_value = []

    result = await full_sessions_registry.call("set_handoff_context", {"content": "## Handoff"})

    assert "error" in result
    assert "No session context available" in result["error"]


@pytest.mark.asyncio
async def test_set_handoff_context_agent_authored(mock_session_manager, full_sessions_registry):
    """Test set_handoff_context with agent-authored content."""
    mock_session = _make_mock_session("sess-abc")
    mock_session_manager.resolve_session_reference.return_value = "sess-abc"
    mock_session_manager.get.return_value = mock_session

    from gobby.utils.session_context import session_context_for_test

    with session_context_for_test("sess-abc"):
        result = await full_sessions_registry.call(
            "set_handoff_context", {"content": "## My Summary"}
        )

    assert result["success"] is True
    assert result["mode"] == "agent_authored"
    mock_session_manager.update_summary.assert_called_once_with(
        "sess-abc", summary_markdown="## My Summary"
    )
    mock_session_manager.update_last_turn_markdown.assert_called_once_with(
        "sess-abc", "## My Summary"
    )
    mock_session_manager.update_status.assert_called_once_with("sess-abc", "handoff_ready")


@pytest.mark.asyncio
async def test_set_handoff_context_accepts_explicit_session_id(
    mock_session_manager, full_sessions_registry
):
    mock_session = _make_mock_session("resolved-session")
    mock_session_manager.resolve_session_reference.return_value = "resolved-session"
    mock_session_manager.get.return_value = mock_session

    result = await full_sessions_registry.call(
        "set_handoff_context",
        {"session_id": "#42", "content": "## Explicit Summary"},
    )

    assert result["success"] is True
    assert result["session_id"] == "resolved-session"
    mock_session_manager.resolve_session_reference.assert_called_once_with("#42", ANY)
    mock_session_manager.update_summary.assert_called_once_with(
        "resolved-session", summary_markdown="## Explicit Summary"
    )


def test_set_handoff_context_schema_marks_session_id_optional(full_sessions_registry) -> None:
    schema = full_sessions_registry.get_schema("set_handoff_context")

    assert schema is not None
    assert "session_id" in schema["inputSchema"]["properties"]
    assert "full" not in schema["inputSchema"]["properties"]
    assert "session_id" not in schema["inputSchema"].get("required", [])
    assert "defaults to the current session" in schema["description"]


# --- Get Session Commits Tool Tests ---


@pytest.mark.asyncio
async def test_get_session_commits(mock_session_manager, full_sessions_registry):
    """Test get_session_commits tool execution."""
    from datetime import datetime
    from unittest.mock import patch

    mock_session = _make_mock_session("sess-abc")
    mock_session.created_at = datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC)
    mock_session.updated_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    mock_session.transcript_path = "/tmp/test/transcript.jsonl"
    mock_session_manager.get.return_value = mock_session

    # Mock subprocess.run to return git log output
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = (
        "abc123|Fix bug|2025-01-01T11:00:00+00:00\ndef456|Add feature|2025-01-01T11:30:00+00:00"
    )

    with patch("subprocess.run", return_value=mock_result):
        result = await full_sessions_registry.call(
            "get_session_commits", {"session_id": "sess-abc"}
        )

    mock_session_manager.get.assert_called_with("sess-abc")
    assert result["session_id"] == "sess-abc"
    assert result["count"] == 2
    assert len(result["commits"]) == 2
    assert result["commits"][0]["hash"] == "abc123"
    assert result["commits"][0]["message"] == "Fix bug"
    assert result["commits"][1]["hash"] == "def456"
    assert "timeframe" in result


@pytest.mark.asyncio
async def test_get_session_commits_uses_session_project_repo_path(mock_session_manager):
    """Session commit lookup should run git in the session project's repository."""
    from datetime import datetime
    from unittest.mock import patch

    # projects.id is a native uuid column; LocalProjectManager.get() returns
    # None for non-uuid ids, which would silently fall back to the transcript
    # directory instead of the project repo.
    project_id = "7f3e2a10-9b8c-4d5e-a6f7-0123456789ab"
    mock_session = _make_mock_session("sess-abc")
    mock_session.created_at = datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC)
    mock_session.updated_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    mock_session.project_id = project_id
    mock_session.transcript_path = "/tmp/nonrepo/transcript.jsonl"
    mock_session_manager.get.return_value = mock_session

    mock_db = MagicMock()
    mock_db.fetchone.return_value = {
        "id": project_id,
        "name": "gobby",
        "repo_path": "/repo/gobby",
        "github_url": None,
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:00:00Z",
    }
    registry = create_session_messages_registry(session_manager=mock_session_manager, db=mock_db)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "abc123|Fix bug|2025-01-01T11:00:00+00:00"

    with patch("subprocess.run", return_value=mock_result) as run:
        result = await registry.call("get_session_commits", {"session_id": "sess-abc"})

    assert result["count"] == 1
    assert run.call_args.kwargs["cwd"] == "/repo/gobby"


@pytest.mark.asyncio
async def test_get_session_commits_not_found(mock_session_manager, full_sessions_registry):
    """Test get_session_commits returns error when session not found."""
    mock_session_manager.get.return_value = None
    mock_session_manager.list.return_value = []

    result = await full_sessions_registry.call("get_session_commits", {"session_id": "nonexistent"})

    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_get_session_commits_prefix_match(mock_session_manager, full_sessions_registry):
    """Test get_session_commits supports prefix matching via resolve_session_reference."""
    from datetime import datetime
    from unittest.mock import patch

    mock_session = _make_mock_session("sess-abc123")
    mock_session.created_at = datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC)
    mock_session.updated_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    mock_session.transcript_path = "/tmp/test/transcript.jsonl"

    # resolve_session_reference resolves prefix to full ID
    mock_session_manager.resolve_session_reference.return_value = "sess-abc123"
    mock_session_manager.get.return_value = mock_session

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""

    with patch("subprocess.run", return_value=mock_result):
        result = await full_sessions_registry.call(
            "get_session_commits", {"session_id": "sess-abc"}
        )

    assert result["session_id"] == "sess-abc123"
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_get_session_commits_no_commits(mock_session_manager, full_sessions_registry):
    """Test get_session_commits with no commits in timeframe."""
    from datetime import datetime
    from unittest.mock import patch

    mock_session = _make_mock_session("sess-abc")
    mock_session.created_at = datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC)
    mock_session.updated_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    mock_session.transcript_path = None  # No transcript path
    mock_session_manager.get.return_value = mock_session

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""

    with patch("subprocess.run", return_value=mock_result):
        result = await full_sessions_registry.call(
            "get_session_commits", {"session_id": "sess-abc"}
        )

    assert result["session_id"] == "sess-abc"
    assert result["count"] == 0
    assert result["commits"] == []


@pytest.mark.asyncio
async def test_get_session_commits_git_error(mock_session_manager, full_sessions_registry):
    """Test get_session_commits handles git errors."""
    from datetime import datetime
    from unittest.mock import patch

    mock_session = _make_mock_session("sess-abc")
    mock_session.created_at = datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC)
    mock_session.updated_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    mock_session.transcript_path = "/tmp/test/transcript.jsonl"
    mock_session_manager.get.return_value = mock_session

    mock_result = MagicMock()
    mock_result.returncode = 128
    mock_result.stderr = "fatal: not a git repository"

    with patch("subprocess.run", return_value=mock_result):
        result = await full_sessions_registry.call(
            "get_session_commits", {"session_id": "sess-abc"}
        )

    assert result["session_id"] == "sess-abc"
    assert "error" in result
    assert "Git command failed" in result["error"]


@pytest.fixture(autouse=True)
def _local_session_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.sessions._commits.require_local_session_ownership",
        lambda _session: "local-machine",
    )
