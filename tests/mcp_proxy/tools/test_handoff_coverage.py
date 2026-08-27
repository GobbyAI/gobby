"""Tests for sessions/_handoff.py — targeting uncovered lines."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    *,
    id: str = "sess-uuid-1",
    summary_markdown: str | None = None,
    transcript_path: str | None = None,
    title: str = "Test Session",
    status: str = "active",
    source: str = "claude",
    project_id: str = "11111111-1111-4111-8111-111111110001",
    seq_num: int | None = 1,
) -> MagicMock:
    session = MagicMock()
    session.id = id
    session.summary_markdown = summary_markdown
    session.transcript_path = transcript_path
    session.title = title
    session.status = status
    session.source = source
    session.project_id = project_id
    session.seq_num = seq_num
    return session


@pytest.fixture
def mock_session_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.db = MagicMock()
    return mgr


@pytest.fixture
def mock_inter_session_msg_manager() -> MagicMock:
    return MagicMock()


def _register_tools(
    session_manager: MagicMock | None,
    llm_service: MagicMock | None = None,
    transcript_processor: MagicMock | None = None,
    inter_session_message_manager: MagicMock | None = None,
) -> InternalToolRegistry:
    """Register handoff tools and return the registry."""
    from gobby.mcp_proxy.tools.sessions._handoff import register_handoff_tools

    registry = InternalToolRegistry(
        name="test-handoff",
        description="Test handoff tools",
    )
    register_handoff_tools(
        registry,
        session_manager,
        llm_service_resolver=lambda: llm_service,
        transcript_processor=transcript_processor,
        inter_session_message_manager=inter_session_message_manager,
    )
    return registry


# ---------------------------------------------------------------------------
# set_handoff_context tests
# ---------------------------------------------------------------------------


class TestSetHandoffContext:
    """Tests for set_handoff_context tool."""

    @pytest.mark.asyncio
    async def test_session_manager_none(self) -> None:
        """When session_manager is None, returns error."""
        from gobby.mcp_proxy.tools.sessions._handoff import register_handoff_tools

        registry = InternalToolRegistry(name="test", description="test")
        register_handoff_tools(registry, session_manager=None)

        with session_context_for_test("s1"):
            result = await registry.call("set_handoff_context", {})
        assert result["success"] is False
        assert "not available" in result["error"]


# ---------------------------------------------------------------------------
# get_handoff_context tests
# ---------------------------------------------------------------------------


class TestGetHandoffContextProjectScope:
    """Project scoping tests for get_handoff_context."""

    @patch(
        "gobby.mcp_proxy.tools.sessions._handoff.get_project_context",
        return_value={"id": "11111111-1111-4111-8111-111111110001"},
    )
    def test_fallback_uses_caller_project_context(
        self, _mock_project_context: MagicMock, mock_session_manager: MagicMock
    ) -> None:
        parent = _make_session(
            id="parent-session",
            summary_markdown="# Same project\nReady",
            status="handoff_ready",
            project_id="11111111-1111-4111-8111-111111110001",
        )
        mock_session_manager.find_parent.return_value = parent
        registry = _register_tools(mock_session_manager)

        result = registry.get_tool("get_handoff_context")(project_id="spoofed-project")

        assert result["success"] is True
        assert result["session_id"] == "parent-session"
        mock_session_manager.find_parent.assert_called_once()
        assert (
            mock_session_manager.find_parent.call_args.kwargs["project_id"]
            == "11111111-1111-4111-8111-111111110001"
        )
        mock_session_manager.list.assert_not_called()

    @patch("gobby.mcp_proxy.tools.sessions._handoff.get_project_context", return_value=None)
    def test_fallback_without_project_context_fails_closed(
        self, _mock_project_context: MagicMock, mock_session_manager: MagicMock
    ) -> None:
        registry = _register_tools(mock_session_manager)

        result = registry.get_tool("get_handoff_context")()

        assert result["success"] is False
        assert result["found"] is False
        mock_session_manager.find_parent.assert_not_called()
        mock_session_manager.list.assert_not_called()

    @patch(
        "gobby.mcp_proxy.tools.sessions._handoff.get_project_context",
        return_value={"id": "11111111-1111-4111-8111-111111110001"},
    )
    def test_explicit_cross_project_session_fails_closed(
        self, _mock_project_context: MagicMock, mock_session_manager: MagicMock
    ) -> None:
        parent = _make_session(
            id="parent-session",
            summary_markdown="# Other project\nWrong",
            status="handoff_ready",
            project_id="other-project",
        )
        mock_session_manager.resolve_session_reference.return_value = "parent-session"
        mock_session_manager.get.return_value = parent
        registry = _register_tools(mock_session_manager)

        result = registry.get_tool("get_handoff_context")(session_id="parent-session")

        assert result["success"] is False
        assert result["found"] is False
        assert "context" not in result

    @patch(
        "gobby.mcp_proxy.tools.sessions._handoff.get_project_context",
        return_value={"id": "11111111-1111-4111-8111-111111110001"},
    )
    def test_link_child_session_rejects_cross_project_child(
        self, _mock_project_context: MagicMock, mock_session_manager: MagicMock
    ) -> None:
        parent = _make_session(
            id="parent-session",
            summary_markdown="# Parent\nReady",
            status="handoff_ready",
            project_id="11111111-1111-4111-8111-111111110001",
        )
        child = _make_session(id="child-session", project_id="11111111-1111-4111-8111-111111110002")
        mock_session_manager.resolve_session_reference.side_effect = [
            "parent-session",
            "child-session",
        ]
        mock_session_manager.get.side_effect = [parent, child]
        registry = _register_tools(mock_session_manager)

        result = registry.get_tool("get_handoff_context")(
            session_id="parent-session",
            link_child_session_id="child-session",
        )

        assert result["success"] is False
        assert "different project" in result["error"]
        mock_session_manager.update_parent_session_id.assert_not_called()


# ---------------------------------------------------------------------------
# get_handoff_context self-read after in-place compaction (#21090)
# ---------------------------------------------------------------------------


class TestGetHandoffContextSelfReadAfterCompaction:
    """A session reading its own row keeps the pre-compaction summary."""

    @patch(
        "gobby.mcp_proxy.tools.sessions._handoff.get_project_context",
        return_value={"id": "11111111-1111-4111-8111-111111110001"},
    )
    def test_stale_self_read_serves_summary_plus_digest_tail(
        self, _mock_project_context: MagicMock, mock_session_manager: MagicMock
    ) -> None:
        own = _make_session(
            id="sess-uuid-1",
            summary_markdown="## Current State\n\n#20728 is in progress in a backend-developer run.",
            status="active",
        )
        own.digest_markdown = (
            "<!-- gobby:digest-turn:1 -->\n### Turn 1\nKicked off #21085.\n\n"
            "<!-- gobby:digest-turn:2 -->\n### Turn 2\nClosed #21085 and spawned the #20728 agent."
        )
        own.summary_digest_turn_count = 1
        own.last_turn_markdown = (
            "The user asked the agent to continue work after a compaction interruption."
        )
        mock_session_manager.resolve_session_reference.return_value = "sess-uuid-1"
        mock_session_manager.get.return_value = own
        registry = _register_tools(mock_session_manager)

        tool = registry.get_tool("get_handoff_context")
        assert tool is not None
        with session_context_for_test("sess-uuid-1"):
            result = tool(session_id="#1")

        assert result["success"] is True
        assert result["stale"] is True
        assert result["context_type"] == "summary_with_digest_tail"
        assert result["context"].startswith("## Current State")
        assert "Closed #21085 and spawned the #20728 agent." in result["context"]
        assert "continue work after a compaction interruption" not in result["context"]
