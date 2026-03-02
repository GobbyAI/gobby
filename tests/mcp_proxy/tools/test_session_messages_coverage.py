"""
Comprehensive unit tests for session_messages.py MCP tools module.

Tests cover:
- Helper functions (_format_turns_for_llm)
- Message tools (get_session_messages, search_messages)
- Handoff tools (set_handoff_context, get_handoff_context)
- Session CRUD tools (get_session, list_sessions, session_stats)
- Session commits tools (get_session_commits, mark_loop_complete)
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.workflows.summary_actions import format_turns_for_llm as _format_turns_for_llm

pytestmark = pytest.mark.unit

# ============================================================================
# Custom Registry Class for Testing
# ============================================================================


class SessionMessagesTestRegistry(InternalToolRegistry):
    """Registry subclass with get_tool method for testing."""

    def get_tool(self, name: str) -> Callable[..., Any] | None:
        """Get a tool function by name (for testing)."""
        tool = self._tools.get(name)
        return tool.func if tool else None


def create_test_registry(
    message_manager: Any = None,
    session_manager: Any = None,
    inter_session_message_manager: Any = None,
) -> SessionMessagesTestRegistry:
    """Create a test-friendly registry by wrapping the real factory."""
    # Create the real registry
    real_registry = create_session_messages_registry(
        message_manager=message_manager,
        session_manager=session_manager,
        inter_session_message_manager=inter_session_message_manager,
    )

    # Create test registry with same tools
    test_registry = SessionMessagesTestRegistry(
        name=real_registry.name,
        description=real_registry.description,
    )
    test_registry._tools = real_registry._tools
    return test_registry


# ============================================================================
# Tests for set_handoff_context tool
# ============================================================================


class TestSetHandoffContext:
    """Tests for set_handoff_context tool."""

    @pytest.mark.asyncio
    async def test_agent_authored_writes_summary(self) -> None:
        """Test that content param writes to summary_markdown and sets handoff_ready."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        session_manager.resolve_session_reference.return_value = "sess-123"
        session_manager.get.return_value = mock_session

        registry = create_test_registry(session_manager=session_manager)
        set_context = registry.get_tool("set_handoff_context")

        result = await set_context(session_id="sess-123", content="## My Handoff")

        assert result["success"] is True
        assert result["mode"] == "agent_authored"
        assert result["summary_length"] == len("## My Handoff")
        session_manager.update_summary.assert_called_once_with(
            "sess-123", summary_markdown="## My Handoff"
        )
        session_manager.update_status.assert_called_once_with("sess-123", "handoff_ready")

    @pytest.mark.asyncio
    async def test_agent_authored_no_handoff_ready(self) -> None:
        """Test set_handoff_ready=False does not change status."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        session_manager.resolve_session_reference.return_value = "sess-123"
        session_manager.get.return_value = mock_session

        registry = create_test_registry(session_manager=session_manager)
        set_context = registry.get_tool("set_handoff_context")

        result = await set_context(
            session_id="sess-123", content="## Handoff", set_handoff_ready=False
        )

        assert result["success"] is True
        session_manager.update_summary.assert_called_once()
        session_manager.update_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_authored_with_to_session(self) -> None:
        """Test that content + to_session writes AND sends P2P message."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        mock_session.project_id = "proj-1"
        session_manager.resolve_session_reference.return_value = "sess-123"
        session_manager.get.return_value = mock_session

        mock_target = MagicMock()
        mock_target.id = "sess-456"
        mock_target.project_id = "proj-1"

        def get_side_effect(sid: str) -> Any:
            if sid == "sess-123":
                return mock_session
            if sid == "sess-456":
                return mock_target
            return None

        session_manager.get.side_effect = get_side_effect

        # Second resolve call for to_session
        session_manager.resolve_session_reference.side_effect = [
            "sess-123",  # for session_id
            "sess-456",  # for to_session
        ]

        ism_manager = MagicMock()
        mock_msg = MagicMock()
        mock_msg.id = "msg-1"
        ism_manager.create_message.return_value = mock_msg

        registry = create_test_registry(
            session_manager=session_manager,
            inter_session_message_manager=ism_manager,
        )
        set_context = registry.get_tool("set_handoff_context")

        result = await set_context(
            session_id="sess-123", content="## Handoff", to_session="sess-456"
        )

        assert result["success"] is True
        assert result["send_result"]["success"] is True
        assert result["send_result"]["message_id"] == "msg-1"
        ism_manager.create_message.assert_called_once_with(
            from_session="sess-123",
            to_session="sess-456",
            content="## Handoff",
            message_type="handoff",
        )

    @pytest.mark.asyncio
    async def test_session_not_found(self) -> None:
        """Test error when session not found."""
        session_manager = MagicMock()
        session_manager.resolve_session_reference.side_effect = ValueError("Not found")

        registry = create_test_registry(session_manager=session_manager)
        set_context = registry.get_tool("set_handoff_context")

        result = await set_context(session_id="nonexistent", content="## Handoff")

        assert result["success"] is False
        assert "Not found" in result["error"]


# ============================================================================
# Tests for _format_turns_for_llm helper
# ============================================================================


class TestFormatTurnsForLLM:
    """Tests for _format_turns_for_llm helper function."""

    def test_empty_turns(self) -> None:
        """Test formatting empty turn list."""
        result = _format_turns_for_llm([])
        assert result == ""

    def test_simple_text_content(self) -> None:
        """Test formatting turns with simple text content."""
        turns = [
            {"message": {"role": "user", "content": "Hello"}},
            {"message": {"role": "assistant", "content": "Hi there"}},
        ]
        result = _format_turns_for_llm(turns)

        assert "[Turn 1 - user]: Hello" in result
        assert "[Turn 2 - assistant]: Hi there" in result

    def test_content_block_list(self) -> None:
        """Test formatting turns with content as list of blocks."""
        turns = [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Here is the result"},
                        {"type": "tool_use", "name": "Edit"},
                    ],
                }
            }
        ]
        result = _format_turns_for_llm(turns)

        assert "[Turn 1 - assistant]:" in result
        assert "Here is the result" in result
        assert "[Tool: Edit]" in result

    def test_content_block_with_missing_fields(self) -> None:
        """Test handling content blocks with missing fields."""
        turns = [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text"},  # Missing 'text' field
                        {"type": "tool_use"},  # Missing 'name' field
                        {"type": "unknown_type"},
                    ],
                }
            }
        ]
        result = _format_turns_for_llm(turns)

        assert "[Turn 1 - assistant]:" in result
        assert "[Tool: unknown]" in result

    def test_non_dict_content_blocks(self) -> None:
        """Test handling non-dict content blocks."""
        turns = [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        "plain string",  # Not a dict
                        123,  # Number
                        {"type": "text", "text": "actual text"},
                    ],
                }
            }
        ]
        result = _format_turns_for_llm(turns)

        assert "actual text" in result

    def test_missing_role(self) -> None:
        """Test handling turns with missing role."""
        turns = [{"message": {"content": "No role here"}}]
        result = _format_turns_for_llm(turns)

        assert "[Turn 1 - unknown]:" in result

    def test_missing_content(self) -> None:
        """Test handling turns with missing content."""
        turns = [{"message": {"role": "user"}}]
        result = _format_turns_for_llm(turns)

        assert "[Turn 1 - user]:" in result

    def test_turn_separator(self) -> None:
        """Test that turns are separated by double newlines."""
        turns = [
            {"message": {"role": "user", "content": "First"}},
            {"message": {"role": "assistant", "content": "Second"}},
        ]
        result = _format_turns_for_llm(turns)

        assert "\n\n" in result


# ============================================================================
# Tests for Message Tools
# ============================================================================


class TestGetSessionMessages:
    """Tests for get_session_messages tool."""

    @pytest.mark.asyncio
    async def test_get_messages_success(self):
        """Test successful message retrieval."""
        message_manager = AsyncMock()
        message_manager.get_messages.return_value = [
            {"id": 1, "content": "Hello", "role": "user"},
            {"id": 2, "content": "Hi", "role": "assistant"},
        ]
        message_manager.count_messages.return_value = 2

        registry = create_test_registry(message_manager=message_manager)
        get_messages = registry.get_tool("get_session_messages")

        result = await get_messages(session_id="sess-123")

        assert result["success"] is True
        assert result["total_count"] == 2
        assert result["returned_count"] == 2
        assert len(result["messages"]) == 2
        message_manager.get_messages.assert_called_once_with(
            session_id="sess-123", limit=50, offset=0
        )

    @pytest.mark.asyncio
    async def test_get_messages_truncates_content(self):
        """Test that large content is truncated when full_content=False."""
        long_content = "x" * 600  # More than 500 chars
        message_manager = AsyncMock()
        message_manager.get_messages.return_value = [
            {"id": 1, "content": long_content, "role": "user"},
        ]
        message_manager.count_messages.return_value = 1

        registry = create_test_registry(message_manager=message_manager)
        get_messages = registry.get_tool("get_session_messages")

        result = await get_messages(session_id="sess-123", full_content=False)

        assert result["success"] is True
        assert result["truncated"] is True
        # Content should be truncated to ~500 chars + "... (truncated)"
        assert len(result["messages"][0]["content"]) < 600
        assert "... (truncated)" in result["messages"][0]["content"]

    @pytest.mark.asyncio
    async def test_get_messages_full_content(self):
        """Test that content is not truncated when full_content=True."""
        long_content = "x" * 600
        message_manager = AsyncMock()
        message_manager.get_messages.return_value = [
            {"id": 1, "content": long_content, "role": "user"},
        ]
        message_manager.count_messages.return_value = 1

        registry = create_test_registry(message_manager=message_manager)
        get_messages = registry.get_tool("get_session_messages")

        result = await get_messages(session_id="sess-123", full_content=True)

        assert result["success"] is True
        assert result["truncated"] is False
        assert result["messages"][0]["content"] == long_content

    @pytest.mark.asyncio
    async def test_get_messages_truncates_tool_calls(self):
        """Test that tool call input is truncated."""
        long_input = "y" * 300
        message_manager = AsyncMock()
        message_manager.get_messages.return_value = [
            {
                "id": 1,
                "content": "test",
                "role": "assistant",
                "tool_calls": [{"name": "Edit", "input": long_input}],
            },
        ]
        message_manager.count_messages.return_value = 1

        registry = create_test_registry(message_manager=message_manager)
        get_messages = registry.get_tool("get_session_messages")

        result = await get_messages(session_id="sess-123", full_content=False)

        assert result["success"] is True
        assert "... (truncated)" in result["messages"][0]["tool_calls"][0]["input"]

    @pytest.mark.asyncio
    async def test_get_messages_truncates_tool_result(self):
        """Test that tool result content is truncated."""
        long_result = "z" * 300
        message_manager = AsyncMock()
        message_manager.get_messages.return_value = [
            {
                "id": 1,
                "content": "test",
                "role": "user",
                "tool_result": {"content": long_result},
            },
        ]
        message_manager.count_messages.return_value = 1

        registry = create_test_registry(message_manager=message_manager)
        get_messages = registry.get_tool("get_session_messages")

        result = await get_messages(session_id="sess-123", full_content=False)

        assert result["success"] is True
        assert "... (truncated)" in result["messages"][0]["tool_result"]["content"]

    @pytest.mark.asyncio
    async def test_get_messages_with_pagination(self):
        """Test message retrieval with pagination."""
        message_manager = AsyncMock()
        message_manager.get_messages.return_value = []
        message_manager.count_messages.return_value = 100

        registry = create_test_registry(message_manager=message_manager)
        get_messages = registry.get_tool("get_session_messages")

        result = await get_messages(session_id="sess-123", limit=10, offset=20)

        assert result["limit"] == 10
        assert result["offset"] == 20
        message_manager.get_messages.assert_called_once_with(
            session_id="sess-123", limit=10, offset=20
        )

    @pytest.mark.asyncio
    async def test_get_messages_error(self):
        """Test error handling in get_session_messages."""
        message_manager = AsyncMock()
        message_manager.get_messages.side_effect = Exception("Database error")

        registry = create_test_registry(message_manager=message_manager)
        get_messages = registry.get_tool("get_session_messages")

        result = await get_messages(session_id="sess-123")

        assert result["success"] is False
        assert "Database error" in result["error"]


class TestSearchMessages:
    """Tests for search_messages tool."""

    @pytest.mark.asyncio
    async def test_search_messages_success(self):
        """Test successful message search."""
        message_manager = AsyncMock()
        message_manager.search_messages.return_value = [
            {"id": 1, "content": "Found match", "role": "user"},
        ]

        registry = create_test_registry(message_manager=message_manager)
        search = registry.get_tool("search_messages")

        result = await search(query="match")

        assert result["success"] is True
        assert result["count"] == 1
        assert len(result["results"]) == 1
        message_manager.search_messages.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_messages_with_session_filter(self):
        """Test search with session ID filter."""
        message_manager = AsyncMock()
        message_manager.search_messages.return_value = []

        registry = create_test_registry(message_manager=message_manager)
        search = registry.get_tool("search_messages")

        await search(query="test", session_id="sess-123")

        message_manager.search_messages.assert_called_once_with(
            query_text="test", session_id="sess-123", limit=20
        )

    @pytest.mark.asyncio
    async def test_search_messages_truncates_content(self):
        """Test that search results are truncated."""
        long_content = "x" * 600
        message_manager = AsyncMock()
        message_manager.search_messages.return_value = [
            {"id": 1, "content": long_content, "role": "user"},
        ]

        registry = create_test_registry(message_manager=message_manager)
        search = registry.get_tool("search_messages")

        result = await search(query="test", full_content=False)

        assert result["truncated"] is True
        assert "... (truncated)" in result["results"][0]["content"]

    @pytest.mark.asyncio
    async def test_search_messages_error(self):
        """Test error handling in search_messages."""
        message_manager = AsyncMock()
        message_manager.search_messages.side_effect = Exception("Search failed")

        registry = create_test_registry(message_manager=message_manager)
        search = registry.get_tool("search_messages")

        result = await search(query="test")

        assert result["success"] is False
        assert "Search failed" in result["error"]


# ============================================================================
# Tests for Handoff Tools
# ============================================================================


class TestGetHandoffContext:
    """Tests for get_handoff_context tool."""

    def test_get_by_session_id_returns_summary(self) -> None:
        """Test retrieval by session_id returns summary_markdown preferentially."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        mock_session.summary_markdown = "## Summary"
        mock_session.compact_markdown = "## Compact"
        mock_session.title = "Test Session"
        mock_session.status = "handoff_ready"
        session_manager.resolve_session_reference.return_value = "sess-123"
        session_manager.get.return_value = mock_session

        registry = create_test_registry(session_manager=session_manager)
        get_context = registry.get_tool("get_handoff_context")

        result = get_context(session_id="sess-123")

        assert result["success"] is True
        assert result["found"] is True
        assert result["session_id"] == "sess-123"
        assert result["has_context"] is True
        assert result["context"] == "## Summary"
        assert result["context_type"] == "summary_markdown"

    def test_get_falls_back_to_compact(self) -> None:
        """Test that compact_markdown is used when summary_markdown is None."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        mock_session.summary_markdown = None
        mock_session.compact_markdown = "## Compact"
        mock_session.title = "Test"
        mock_session.status = "handoff_ready"
        session_manager.resolve_session_reference.return_value = "sess-123"
        session_manager.get.return_value = mock_session

        registry = create_test_registry(session_manager=session_manager)
        get_context = registry.get_tool("get_handoff_context")

        result = get_context(session_id="sess-123")

        assert result["context"] == "## Compact"
        assert result["context_type"] == "compact_markdown"

    def test_get_handoff_context_session_not_found(self) -> None:
        """Test error when session not found by ID."""
        session_manager = MagicMock()
        session_manager.resolve_session_reference.side_effect = ValueError("Not found")

        registry = create_test_registry(session_manager=session_manager)
        get_context = registry.get_tool("get_handoff_context")

        result = get_context(session_id="nonexistent")

        assert result["success"] is False
        assert "Not found" in result["error"]

    def test_get_ambiguous_prefix(self) -> None:
        """Test error on ambiguous session ID prefix."""
        session_manager = MagicMock()
        session_manager.resolve_session_reference.side_effect = ValueError(
            "Ambiguous session reference 'sess-123': matches sess-123-a, sess-123-b"
        )

        registry = create_test_registry(session_manager=session_manager)
        get_context = registry.get_tool("get_handoff_context")

        result = get_context(session_id="sess-123")

        assert "error" in result
        assert "Ambiguous" in result["error"]

    @patch("gobby.utils.machine_id.get_machine_id")
    def test_get_by_project_id(self, mock_get_machine_id) -> None:
        """Test finding session by project_id."""
        mock_get_machine_id.return_value = "machine-123"

        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "sess-456"
        mock_session.summary_markdown = "## Context"
        mock_session.compact_markdown = None
        mock_session.title = "Test"
        mock_session.status = "handoff_ready"
        session_manager.find_parent.return_value = mock_session

        registry = create_test_registry(session_manager=session_manager)
        get_context = registry.get_tool("get_handoff_context")

        result = get_context(project_id="project-123")

        assert result["found"] is True
        session_manager.find_parent.assert_called_once_with(
            machine_id="machine-123",
            project_id="project-123",
            source=None,
            status="handoff_ready",
        )

    def test_get_most_recent_handoff(self) -> None:
        """Test finding most recent handoff_ready session when no params given."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "sess-latest"
        mock_session.summary_markdown = "## Summary"
        mock_session.compact_markdown = None
        mock_session.title = "Latest"
        mock_session.status = "handoff_ready"
        session_manager.list.return_value = [mock_session]

        registry = create_test_registry(session_manager=session_manager)
        get_context = registry.get_tool("get_handoff_context")

        result = get_context()

        assert result["found"] is True
        assert result["context_type"] == "summary_markdown"

    def test_get_no_session_found(self) -> None:
        """Test when no handoff_ready session found."""
        session_manager = MagicMock()
        session_manager.list.return_value = []

        registry = create_test_registry(session_manager=session_manager)
        get_context = registry.get_tool("get_handoff_context")

        result = get_context()

        assert result["found"] is False
        assert "No handoff-ready session found" in result["message"]

    def test_get_no_context_on_session(self) -> None:
        """Test when session exists but has no context."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        mock_session.summary_markdown = None
        mock_session.compact_markdown = None
        session_manager.resolve_session_reference.return_value = "sess-123"
        session_manager.get.return_value = mock_session

        registry = create_test_registry(session_manager=session_manager)
        get_context = registry.get_tool("get_handoff_context")

        result = get_context(session_id="sess-123")

        assert result["found"] is True
        assert result["has_context"] is False

    def test_get_with_link_child(self) -> None:
        """Test linking a child session to parent."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "sess-parent"
        mock_session.summary_markdown = "## Context"
        mock_session.compact_markdown = None
        mock_session.title = "Parent"
        mock_session.status = "handoff_ready"
        session_manager.get.return_value = mock_session
        session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref

        registry = create_test_registry(session_manager=session_manager)
        get_context = registry.get_tool("get_handoff_context")

        result = get_context(session_id="sess-parent", link_child_session_id="sess-child")

        assert result["found"] is True
        assert result["linked_child"] == "sess-child"
        session_manager.update_parent_session_id.assert_called_once_with(
            "sess-child", "sess-parent"
        )


# ============================================================================
# Tests for Session CRUD Tools
# ============================================================================


class TestGetSession:
    """Tests for get_session tool."""

    def test_get_session_success(self) -> None:
        """Test successful session retrieval."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.to_dict.return_value = {
            "id": "sess-123",
            "title": "Test Session",
            "status": "active",
        }
        session_manager.resolve_session_reference.return_value = "sess-123"
        session_manager.get.return_value = mock_session

        registry = create_test_registry(session_manager=session_manager)
        get_session = registry.get_tool("get_session")

        result = get_session(session_id="sess-123")

        assert result["found"] is True
        assert result["id"] == "sess-123"
        assert result["title"] == "Test Session"

    def test_get_session_by_prefix(self) -> None:
        """Test session retrieval by prefix via resolve_session_reference."""
        session_manager = MagicMock()

        mock_session = MagicMock()
        mock_session.id = "sess-123-full"
        mock_session.to_dict.return_value = {"id": "sess-123-full"}

        # resolve_session_reference handles prefix matching internally
        session_manager.resolve_session_reference.return_value = "sess-123-full"
        session_manager.get.return_value = mock_session

        registry = create_test_registry(session_manager=session_manager)
        get_session = registry.get_tool("get_session")

        result = get_session(session_id="sess-123")

        assert result["found"] is True
        assert result["id"] == "sess-123-full"

    def test_get_session_ambiguous_prefix(self) -> None:
        """Test session retrieval with ambiguous prefix raises ValueError."""
        session_manager = MagicMock()
        # resolve_session_reference raises ValueError for ambiguous prefix
        session_manager.resolve_session_reference.side_effect = ValueError(
            "Ambiguous session reference 'sess-abc' matches 3 sessions"
        )
        session_manager.get.return_value = None

        registry = create_test_registry(session_manager=session_manager)
        get_session = registry.get_tool("get_session")

        result = get_session(session_id="sess-abc")

        # When resolve raises ValueError, code catches it and session is None
        assert result["found"] is False
        assert "not found" in result["error"]

    def test_get_session_not_found(self) -> None:
        """Test when session not found."""
        session_manager = MagicMock()
        session_manager.resolve_session_reference.side_effect = ValueError("Not found")
        session_manager.get.return_value = None

        registry = create_test_registry(session_manager=session_manager)
        get_session = registry.get_tool("get_session")

        result = get_session(session_id="nonexistent")

        assert result["found"] is False
        assert "not found" in result["error"]


class TestListSessions:
    """Tests for list_sessions tool."""

    def test_list_sessions_basic(self) -> None:
        """Test basic session listing."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.to_dict.return_value = {"id": "sess-1", "status": "active"}
        session_manager.list.return_value = [mock_session]
        session_manager.count.return_value = 1

        registry = create_test_registry(session_manager=session_manager)
        list_sessions = registry.get_tool("list_sessions")

        result = list_sessions()

        assert result["count"] == 1
        assert result["total"] == 1
        assert len(result["sessions"]) == 1

    def test_list_sessions_with_filters(self) -> None:
        """Test session listing with filters."""
        session_manager = MagicMock()
        session_manager.list.return_value = []
        session_manager.count.return_value = 0

        registry = create_test_registry(session_manager=session_manager)
        list_sessions = registry.get_tool("list_sessions")

        result = list_sessions(project_id="proj-1", status="active", source="claude_code", limit=10)

        assert result["filters"]["project_id"] == "proj-1"
        assert result["filters"]["status"] == "active"
        assert result["filters"]["source"] == "claude_code"
        assert result["limit"] == 10

        session_manager.list.assert_called_once_with(
            project_id="proj-1", status="active", source="claude_code", limit=10
        )

    def test_list_sessions_misuse_warning(self) -> None:
        """Test that list_sessions warns about misuse pattern (status='active', limit=1)."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.to_dict.return_value = {"id": "sess-1", "status": "active"}
        session_manager.list.return_value = [mock_session]
        session_manager.count.return_value = 1

        registry = create_test_registry(session_manager=session_manager)
        list_sessions = registry.get_tool("list_sessions")

        # This is the misuse pattern - trying to find "my session" by listing active sessions
        result = list_sessions(status="active", limit=1)

        # Should return a warning about this pattern
        assert "warning" in result
        assert "get_current_session" in result["warning"]
        assert "hint" in result
        # Should still return the sessions
        assert result["count"] == 1
        assert len(result["sessions"]) == 1


class TestSessionStats:
    """Tests for session_stats tool."""

    def test_session_stats_basic(self) -> None:
        """Test basic session statistics."""
        session_manager = MagicMock()
        # Count is called: 1x total + 6x sources (claude, gemini, codex, cursor, windsurf, copilot)
        session_manager.count.side_effect = [
            100,  # Total
            50,  # claude
            30,  # gemini
            0,  # codex (will be excluded)
            0,  # cursor (will be excluded)
            0,  # windsurf (will be excluded)
            0,  # copilot (will be excluded)
        ]
        session_manager.count_by_status.return_value = {
            "active": 10,
            "paused": 20,
            "expired": 70,
        }

        registry = create_test_registry(session_manager=session_manager)
        stats = registry.get_tool("session_stats")

        result = stats()

        assert result["total"] == 100
        assert result["by_status"]["active"] == 10
        assert result["by_source"]["claude"] == 50
        assert result["by_source"]["gemini"] == 30
        assert "codex" not in result["by_source"]  # Zero count excluded


# ============================================================================
# Tests for Session Commits Tool
# ============================================================================


class TestGetSessionCommits:
    """Tests for get_session_commits tool."""

    def test_get_session_commits_success(self) -> None:
        """Test successful commit retrieval."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        mock_session.jsonl_path = "/tmp/test/transcript.jsonl"
        mock_session.created_at = "2024-01-01T10:00:00+00:00"
        mock_session.updated_at = "2024-01-01T12:00:00+00:00"
        session_manager.get.return_value = mock_session

        registry = create_test_registry(session_manager=session_manager)
        get_commits = registry.get_tool("get_session_commits")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="abc1234|First commit|2024-01-01T11:00:00\ndef5678|Second commit|2024-01-01T11:30:00",
            )

            result = get_commits(session_id="sess-123")

        assert result["session_id"] == "sess-123"
        assert result["count"] == 2
        assert result["commits"][0]["hash"] == "abc1234"
        assert result["commits"][0]["message"] == "First commit"

    def test_get_session_commits_not_found(self) -> None:
        """Test when session not found."""
        session_manager = MagicMock()
        session_manager.get.return_value = None
        session_manager.list.return_value = []

        registry = create_test_registry(session_manager=session_manager)
        get_commits = registry.get_tool("get_session_commits")

        result = get_commits(session_id="nonexistent")

        assert "error" in result
        assert "not found" in result["error"]

    def test_get_session_commits_git_error(self) -> None:
        """Test handling git command error."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        mock_session.jsonl_path = "/tmp/test/transcript.jsonl"
        mock_session.created_at = "2024-01-01T10:00:00+00:00"
        mock_session.updated_at = "2024-01-01T12:00:00+00:00"
        session_manager.get.return_value = mock_session

        registry = create_test_registry(session_manager=session_manager)
        get_commits = registry.get_tool("get_session_commits")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="fatal: not a git repository")

            result = get_commits(session_id="sess-123")

        assert "error" in result
        assert "Git command failed" in result["error"]

    def test_get_session_commits_timeout(self) -> None:
        """Test handling git command timeout."""
        import subprocess

        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        mock_session.jsonl_path = "/tmp/test/transcript.jsonl"
        mock_session.created_at = "2024-01-01T10:00:00+00:00"
        mock_session.updated_at = "2024-01-01T12:00:00+00:00"
        session_manager.get.return_value = mock_session

        registry = create_test_registry(session_manager=session_manager)
        get_commits = registry.get_tool("get_session_commits")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("git", 10)

            result = get_commits(session_id="sess-123")

        assert "error" in result
        assert "timed out" in result["error"]

    def test_get_session_commits_git_not_found(self) -> None:
        """Test handling git not found."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        mock_session.jsonl_path = "/tmp/test/transcript.jsonl"
        mock_session.created_at = "2024-01-01T10:00:00+00:00"
        mock_session.updated_at = "2024-01-01T12:00:00+00:00"
        session_manager.get.return_value = mock_session

        registry = create_test_registry(session_manager=session_manager)
        get_commits = registry.get_tool("get_session_commits")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")

            result = get_commits(session_id="sess-123")

        assert "error" in result
        assert "Git executable not found" in result["error"]

    def test_get_session_commits_by_prefix(self) -> None:
        """Test commit retrieval by session ID prefix."""
        session_manager = MagicMock()

        mock_session = MagicMock()
        mock_session.id = "sess-123-full"
        mock_session.jsonl_path = "/tmp/test/transcript.jsonl"
        mock_session.created_at = "2024-01-01T10:00:00+00:00"
        mock_session.updated_at = "2024-01-01T12:00:00+00:00"

        # resolve_session_reference resolves prefix to full ID
        session_manager.resolve_session_reference.return_value = "sess-123-full"
        session_manager.get.return_value = mock_session

        registry = create_test_registry(session_manager=session_manager)
        get_commits = registry.get_tool("get_session_commits")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")

            result = get_commits(session_id="sess-123")

        assert result["session_id"] == "sess-123-full"

    def test_get_session_commits_datetime_objects(self) -> None:
        """Test handling datetime objects instead of strings."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        mock_session.jsonl_path = "/tmp/test/transcript.jsonl"
        mock_session.created_at = datetime(2024, 1, 1, 10, 0, 0, tzinfo=UTC)
        mock_session.updated_at = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        session_manager.get.return_value = mock_session

        registry = create_test_registry(session_manager=session_manager)
        get_commits = registry.get_tool("get_session_commits")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")

            result = get_commits(session_id="sess-123")

        assert result["session_id"] == "sess-123"
        assert "timeframe" in result


class TestMarkLoopComplete:
    """Tests for mark_loop_complete tool."""

    def test_mark_loop_complete_success(self) -> None:
        """Test successful loop completion marking."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        session_manager.get.return_value = mock_session

        mock_svm = MagicMock()

        registry = create_test_registry(session_manager=session_manager)
        mark_complete = registry.get_tool("mark_loop_complete")

        with (
            patch("gobby.storage.database.LocalDatabase"),
            patch("gobby.workflows.state_manager.SessionVariableManager") as mock_svm_class,
        ):
            mock_svm_class.return_value = mock_svm

            result = mark_complete(session_id="sess-123")

        assert result["success"] is True
        assert result["session_id"] == "sess-123"
        assert result["stop_reason"] == "completed"
        mock_svm.set_variable.assert_called_once_with("sess-123", "stop_reason", "completed")

    def test_mark_loop_complete_no_session(self) -> None:
        """Test when session not found."""
        session_manager = MagicMock()
        session_manager.get.return_value = None

        registry = create_test_registry(session_manager=session_manager)
        mark_complete = registry.get_tool("mark_loop_complete")

        result = mark_complete(session_id="nonexistent")

        assert "error" in result
        assert "not found" in result["error"]

    def test_mark_loop_complete_sets_variable_directly(self) -> None:
        """Test that set_variable is called directly (no WorkflowState needed)."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        session_manager.get.return_value = mock_session

        mock_svm = MagicMock()

        registry = create_test_registry(session_manager=session_manager)
        mark_complete = registry.get_tool("mark_loop_complete")

        with (
            patch("gobby.storage.database.LocalDatabase"),
            patch("gobby.workflows.state_manager.SessionVariableManager") as mock_svm_class,
        ):
            mock_svm_class.return_value = mock_svm

            result = mark_complete(session_id="sess-123")

        assert result["success"] is True
        mock_svm.set_variable.assert_called_once_with("sess-123", "stop_reason", "completed")


# ============================================================================
# Tests for Registry Creation
# ============================================================================


class TestRegistryCreation:
    """Tests for create_session_messages_registry function."""

    def test_create_registry_with_no_managers(self) -> None:
        """Test creating registry with no managers."""
        registry = create_session_messages_registry()

        assert registry.name == "gobby-sessions"
        # With no managers, only the registry shell is created
        assert len(registry) == 0

    def test_create_registry_with_message_manager(self) -> None:
        """Test creating registry with message manager only."""
        message_manager = MagicMock()
        registry = create_session_messages_registry(message_manager=message_manager)

        # Should have message tools
        tools = registry.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "get_session_messages" in tool_names
        assert "search_messages" in tool_names

    def test_create_registry_with_session_manager(self) -> None:
        """Test creating registry with session manager only."""
        session_manager = MagicMock()
        registry = create_session_messages_registry(session_manager=session_manager)

        # Should have session CRUD and handoff tools
        tools = registry.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "get_session" in tool_names
        assert "list_sessions" in tool_names
        assert "get_handoff_context" in tool_names
        assert "set_handoff_context" in tool_names

    def test_create_registry_with_both_managers(self) -> None:
        """Test creating registry with both managers."""
        message_manager = MagicMock()
        session_manager = MagicMock()
        registry = create_session_messages_registry(
            message_manager=message_manager,
            session_manager=session_manager,
        )

        tools = registry.list_tools()
        tool_names = [t["name"] for t in tools]

        # Should have all tools
        assert "get_session_messages" in tool_names
        assert "search_messages" in tool_names
        assert "get_session" in tool_names
        assert "list_sessions" in tool_names
        assert "get_handoff_context" in tool_names
        assert "set_handoff_context" in tool_names
        assert "mark_loop_complete" in tool_names


# ============================================================================
# Edge Case Tests
# ============================================================================


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_get_messages_empty_content(self):
        """Test handling messages with empty content."""
        message_manager = AsyncMock()
        message_manager.get_messages.return_value = [
            {"id": 1, "content": "", "role": "user"},
            {"id": 2, "content": None, "role": "assistant"},
        ]
        message_manager.count_messages.return_value = 2

        registry = create_test_registry(message_manager=message_manager)
        get_messages = registry.get_tool("get_session_messages")

        result = await get_messages(session_id="sess-123")

        assert result["success"] is True
        assert result["returned_count"] == 2

    @pytest.mark.asyncio
    async def test_get_messages_non_string_content(self):
        """Test handling messages with non-string content."""
        message_manager = AsyncMock()
        message_manager.get_messages.return_value = [
            {"id": 1, "content": ["block1", "block2"], "role": "assistant"},
        ]
        message_manager.count_messages.return_value = 1

        registry = create_test_registry(message_manager=message_manager)
        get_messages = registry.get_tool("get_session_messages")

        result = await get_messages(session_id="sess-123", full_content=False)

        assert result["success"] is True
        # Non-string content should not be truncated
        assert result["messages"][0]["content"] == ["block1", "block2"]

    def test_format_turns_empty_message(self) -> None:
        """Test formatting turns with empty message dict."""
        turns = [{"message": {}}]
        result = _format_turns_for_llm(turns)

        assert "[Turn 1 - unknown]:" in result

    @pytest.mark.asyncio
    async def test_set_handoff_context_session_manager_none(self) -> None:
        """Test set_handoff_context returns error when no session_manager."""
        registry = create_test_registry()
        set_context = registry.get_tool("set_handoff_context")

        # Tool won't be registered if session_manager is None
        assert set_context is None

    def test_get_handoff_context_session_manager_none(self) -> None:
        """Test get_handoff_context returns error when no session_manager."""
        registry = create_test_registry()
        get_context = registry.get_tool("get_handoff_context")

        # Tool won't be registered if session_manager is None
        assert get_context is None
