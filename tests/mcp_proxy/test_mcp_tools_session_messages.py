from datetime import UTC, datetime
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.sessions.transcript_reader import TranscriptReader
from gobby.sessions.transcript_renderer import ContentBlock, RenderedMessage
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager

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
    reader.get_rendered_messages = AsyncMock()
    reader.count_messages = AsyncMock()
    return reader


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
    mock_transcript_reader.get_rendered_messages.return_value = [rendered]
    mock_transcript_reader.count_messages.return_value = 1

    result = await renderer_registry.call(
        "get_session_messages", {"session_id": "sess-123", "limit": 10, "offset": 0}
    )

    mock_transcript_reader.get_rendered_messages.assert_called_with(
        session_id="sess-123", limit=10, offset=0
    )
    mock_transcript_reader.count_messages.assert_called_with("sess-123")
    assert result["success"] is True
    assert result["total_count"] == 1
    assert len(result["messages"]) == 1
    msg = result["messages"][0]
    assert msg["role"] == "assistant"
    assert msg["content_blocks"][0]["type"] == "text"
    assert msg["content_blocks"][0]["content"] == "Hello world"


@pytest.mark.asyncio
async def test_get_session_messages_renderer_truncates_content_blocks(
    mock_transcript_reader, renderer_registry
):
    """Test that content_blocks text is truncated when full_content=False."""
    long_text = "x" * 1000
    rendered = RenderedMessage(
        id="msg-1",
        role="assistant",
        content=long_text,
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        content_blocks=[ContentBlock(type="text", content=long_text)],
    )
    mock_transcript_reader.get_rendered_messages.return_value = [rendered]
    mock_transcript_reader.count_messages.return_value = 1

    result = await renderer_registry.call(
        "get_session_messages",
        {"session_id": "sess-123", "limit": 10, "offset": 0, "full_content": False},
    )

    assert result["success"] is True
    msg = result["messages"][0]
    # Top-level content truncated at 500
    assert msg["content"].endswith("... (truncated)")
    assert len(msg["content"]) < 1000
    # content_blocks text truncated at 500
    block_content = msg["content_blocks"][0]["content"]
    assert block_content.endswith("... (truncated)")
    assert len(block_content) < 1000


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
    mock_transcript_reader.get_rendered_messages.return_value = [rendered]
    mock_transcript_reader.count_messages.return_value = 1

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
        project_id=None, status=None, source=None, limit=10
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
        project_id="proj-123", status="active", source="claude_code", limit=20
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
    mock_session = _make_mock_session("sess-abc")
    mock_session.summary_markdown = None
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
    mock_session_manager.list.return_value = [mock_session]

    result = await full_sessions_registry.call("get_handoff_context", {})

    mock_session_manager.list.assert_called_with(status="handoff_ready", limit=1)
    assert result["found"] is True
    assert result["session_id"] == "sess-recent"


@pytest.mark.asyncio
async def test_get_handoff_context_links_child(mock_session_manager, full_sessions_registry):
    """Test get_handoff_context can link a child session to the parent."""
    mock_session = _make_mock_session("sess-parent", status="handoff_ready")
    mock_session.summary_markdown = "## Context"
    mock_session.title = "Parent"
    mock_session.status = "handoff_ready"
    mock_session_manager.get.return_value = mock_session

    result = await full_sessions_registry.call(
        "get_handoff_context",
        {"session_id": "sess-parent", "link_child_session_id": "sess-child"},
    )

    mock_session_manager.update_parent_session_id.assert_called_with("sess-child", "sess-parent")
    assert result["linked_child"] == "sess-child"


@pytest.mark.asyncio
async def test_get_handoff_context_no_session_found(mock_session_manager, full_sessions_registry):
    """Test get_handoff_context when no handoff_ready session exists."""
    mock_session_manager.get.return_value = None
    mock_session_manager.list.return_value = []

    result = await full_sessions_registry.call("get_handoff_context", {"session_id": "nonexistent"})

    assert result["success"] is False


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
