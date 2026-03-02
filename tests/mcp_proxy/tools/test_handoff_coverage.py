"""Tests for sessions/_handoff.py — targeting uncovered lines."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    *,
    id: str = "sess-uuid-1",
    summary_markdown: str | None = None,
    compact_markdown: str | None = None,
    jsonl_path: str | None = None,
    title: str = "Test Session",
    status: str = "active",
    source: str = "claude",
    project_id: str = "proj-1",
    seq_num: int | None = 1,
) -> MagicMock:
    session = MagicMock()
    session.id = id
    session.summary_markdown = summary_markdown
    session.compact_markdown = compact_markdown
    session.jsonl_path = jsonl_path
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
        session_manager,  # type: ignore[arg-type]
        llm_service=llm_service,
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
        register_handoff_tools(registry, session_manager=None)  # type: ignore[arg-type]

        result = await registry.call("set_handoff_context", {"session_id": "s1"})
        assert result["success"] is False
        assert "not available" in result["error"]


# ---------------------------------------------------------------------------
# get_handoff_context tests
# ---------------------------------------------------------------------------
