"""Tests for cli/sessions.py -- targeting uncovered lines.

Covers: get_session_manager, get_message_manager, show_session (json/details),
        list_sessions (cost/title truncation), messages (json/tool/truncation),
        search (json/empty), delete failure, stats with project.
Lines targeted: 19-26, 102-158, 177-295, 309-310
"""

from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.sessions import sessions
from gobby.storage.session_models import Session

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_session_manager() -> Generator[MagicMock]:
    with patch("gobby.cli.sessions.get_session_manager") as mock:
        yield mock.return_value


@pytest.fixture
def mock_message_manager() -> Generator[MagicMock]:
    with patch("gobby.cli.sessions.get_message_manager") as mock:
        yield mock.return_value


@pytest.fixture
def mock_resolve_session() -> Generator[MagicMock]:
    with patch("gobby.cli.sessions.resolve_session_id") as mock:
        mock.side_effect = lambda x, **kw: x if x else "current-session"
        yield mock


def _make_session(**overrides: Any) -> Session:
    defaults = {
        "id": "sess-abc123",
        "project_id": "proj-1",
        "status": "active",
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
        "source": "claude",
        "title": "Test Session",
        "seq_num": 1,
        "external_id": None,
        "machine_id": None,
        "jsonl_path": None,
        "summary_path": None,
        "summary_markdown": None,
        "compact_markdown": None,
        "git_branch": None,
        "parent_session_id": None,
    }
    defaults.update(overrides)
    return Session(**defaults)


# =============================================================================
# get_session_manager / get_message_manager
# =============================================================================


class TestManagerCreation:
    @patch("gobby.cli.sessions.LocalDatabase")
    @patch("gobby.cli.sessions.LocalSessionManager")
    def test_get_session_manager(self, mock_mgr_cls: MagicMock, mock_db: MagicMock) -> None:
        from gobby.cli.sessions import get_session_manager

        result = get_session_manager()
        mock_mgr_cls.assert_called_once()
        assert result == mock_mgr_cls.return_value

    @patch("gobby.cli.sessions.LocalDatabase")
    @patch("gobby.cli.sessions.LocalSessionMessageManager")
    def test_get_message_manager(self, mock_mgr_cls: MagicMock, mock_db: MagicMock) -> None:
        from gobby.cli.sessions import get_message_manager

        result = get_message_manager()
        mock_mgr_cls.assert_called_once()
        assert result == mock_mgr_cls.return_value


# =============================================================================
# list_sessions - edge cases
# =============================================================================


class TestListSessionsEdgeCases:
    def test_list_with_cost(self, runner: CliRunner, mock_session_manager: MagicMock) -> None:
        session = _make_session(
            usage_total_cost_usd=1.23,
            usage_input_tokens=1000,
            usage_output_tokens=500,
            usage_cache_creation_tokens=0,
            usage_cache_read_tokens=0,
        )
        mock_session_manager.list.return_value = [session]
        result = runner.invoke(sessions, ["list"])
        assert result.exit_code == 0
        assert "$1.23" in result.output

    def test_list_long_title_truncated(
        self, runner: CliRunner, mock_session_manager: MagicMock
    ) -> None:
        long_title = "A" * 60
        session = _make_session(title=long_title)
        mock_session_manager.list.return_value = [session]
        result = runner.invoke(sessions, ["list"])
        assert result.exit_code == 0
        assert "..." in result.output

    def test_list_no_title(self, runner: CliRunner, mock_session_manager: MagicMock) -> None:
        session = _make_session(title=None, seq_num=None)
        mock_session_manager.list.return_value = [session]
        result = runner.invoke(sessions, ["list"])
        assert result.exit_code == 0
        assert "(no title)" in result.output

    def test_list_handoff_ready_icon(
        self, runner: CliRunner, mock_session_manager: MagicMock
    ) -> None:
        session = _make_session(status="handoff_ready")
        mock_session_manager.list.return_value = [session]
        result = runner.invoke(sessions, ["list"])
        assert result.exit_code == 0
        assert "→" in result.output


# =============================================================================
# show_session - details
# =============================================================================


class TestShowSessionDetails:
    def test_show_json(
        self, runner: CliRunner, mock_session_manager: MagicMock, mock_resolve_session: MagicMock
    ) -> None:
        session = _make_session()
        mock_session_manager.get.return_value = session
        result = runner.invoke(sessions, ["show", "sess-abc123", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == "sess-abc123"

    def test_show_with_branch_and_parent(
        self, runner: CliRunner, mock_session_manager: MagicMock, mock_resolve_session: MagicMock
    ) -> None:
        session = _make_session(
            git_branch="feature/x",
            parent_session_id="sess-parent",
        )
        mock_session_manager.get.return_value = session
        result = runner.invoke(sessions, ["show", "sess-abc123"])
        assert result.exit_code == 0
        assert "Branch: feature/x" in result.output
        assert "Parent: sess-parent" in result.output

    def test_show_with_usage(
        self, runner: CliRunner, mock_session_manager: MagicMock, mock_resolve_session: MagicMock
    ) -> None:
        session = _make_session(
            usage_input_tokens=1000,
            usage_output_tokens=500,
            usage_cache_creation_tokens=200,
            usage_cache_read_tokens=100,
            usage_total_cost_usd=0.0123,
        )
        mock_session_manager.get.return_value = session
        result = runner.invoke(sessions, ["show", "sess-abc123"])
        assert result.exit_code == 0
        assert "Usage Stats:" in result.output
        assert "Input Tokens: 1000" in result.output
        assert "Cache Write: 200" in result.output
        assert "$0.0123" in result.output

    def test_show_long_summary_truncated(
        self, runner: CliRunner, mock_session_manager: MagicMock, mock_resolve_session: MagicMock
    ) -> None:
        session = _make_session(summary_markdown="X" * 600)
        mock_session_manager.get.return_value = session
        result = runner.invoke(sessions, ["show", "sess-abc123"])
        assert result.exit_code == 0
        assert "Summary:" in result.output
        assert "..." in result.output


# =============================================================================
# messages - edge cases
# =============================================================================


class TestMessagesEdgeCases:
    def test_messages_json(
        self,
        runner: CliRunner,
        mock_session_manager: MagicMock,
        mock_message_manager: MagicMock,
        mock_resolve_session: MagicMock,
    ) -> None:
        session = _make_session()
        mock_session_manager.get.return_value = session
        msgs = [{"role": "user", "content": "hi", "message_index": 1}]
        mock_message_manager.get_messages = AsyncMock(return_value=msgs)
        result = runner.invoke(sessions, ["messages", "sess-abc123", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1

    def test_messages_empty(
        self,
        runner: CliRunner,
        mock_session_manager: MagicMock,
        mock_message_manager: MagicMock,
        mock_resolve_session: MagicMock,
    ) -> None:
        session = _make_session()
        mock_session_manager.get.return_value = session
        mock_message_manager.get_messages = AsyncMock(return_value=[])
        result = runner.invoke(sessions, ["messages", "sess-abc123"])
        assert result.exit_code == 0
        assert "No messages found" in result.output

    def test_messages_with_tool(
        self,
        runner: CliRunner,
        mock_session_manager: MagicMock,
        mock_message_manager: MagicMock,
        mock_resolve_session: MagicMock,
    ) -> None:
        session = _make_session()
        mock_session_manager.get.return_value = session
        msgs = [{"role": "tool", "content": "result", "message_index": 1, "tool_name": "read_file"}]
        mock_message_manager.get_messages = AsyncMock(return_value=msgs)
        mock_message_manager.count_messages = AsyncMock(return_value=1)
        result = runner.invoke(sessions, ["messages", "sess-abc123"])
        assert result.exit_code == 0
        assert "read_file" in result.output

    def test_messages_long_content_truncated(
        self,
        runner: CliRunner,
        mock_session_manager: MagicMock,
        mock_message_manager: MagicMock,
        mock_resolve_session: MagicMock,
    ) -> None:
        session = _make_session()
        mock_session_manager.get.return_value = session
        msgs = [{"role": "user", "content": "x" * 300, "message_index": 1}]
        mock_message_manager.get_messages = AsyncMock(return_value=msgs)
        mock_message_manager.count_messages = AsyncMock(return_value=1)
        result = runner.invoke(sessions, ["messages", "sess-abc123"])
        assert result.exit_code == 0
        assert "..." in result.output

    def test_messages_session_not_found(
        self,
        runner: CliRunner,
        mock_session_manager: MagicMock,
        mock_message_manager: MagicMock,
        mock_resolve_session: MagicMock,
    ) -> None:
        mock_session_manager.get.return_value = None
        result = runner.invoke(sessions, ["messages", "sess-bad"])
        assert "Session not found" in result.output


# =============================================================================
# search - edge cases
# =============================================================================


class TestSearchEdgeCases:
    def test_search_json(self, runner: CliRunner, mock_message_manager: MagicMock) -> None:
        msgs = [{"role": "user", "content": "found", "session_id": "s1"}]
        mock_message_manager.search_messages = AsyncMock(return_value=msgs)
        result = runner.invoke(sessions, ["search", "query", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1

    def test_search_empty(self, runner: CliRunner, mock_message_manager: MagicMock) -> None:
        mock_message_manager.search_messages = AsyncMock(return_value=[])
        result = runner.invoke(sessions, ["search", "nothing"])
        assert result.exit_code == 0
        assert "No messages found" in result.output

    def test_search_long_content(self, runner: CliRunner, mock_message_manager: MagicMock) -> None:
        msgs = [{"role": "user", "content": "x" * 200, "session_id": "s1"}]
        mock_message_manager.search_messages = AsyncMock(return_value=msgs)
        result = runner.invoke(sessions, ["search", "query"])
        assert result.exit_code == 0
        assert "..." in result.output

    @patch("gobby.cli.sessions.resolve_session_id", side_effect=lambda x, **kw: x)
    def test_search_with_session_filter(
        self, mock_resolve: MagicMock, runner: CliRunner, mock_message_manager: MagicMock
    ) -> None:
        mock_message_manager.search_messages = AsyncMock(return_value=[])
        result = runner.invoke(sessions, ["search", "query", "--session", "sess-1"])
        assert result.exit_code == 0

    @patch("gobby.cli.sessions.resolve_project_ref", return_value="proj-1")
    def test_search_with_project_filter(
        self, mock_resolve: MagicMock, runner: CliRunner, mock_message_manager: MagicMock
    ) -> None:
        mock_message_manager.search_messages = AsyncMock(return_value=[])
        result = runner.invoke(sessions, ["search", "query", "--project", "myproj"])
        assert result.exit_code == 0


# =============================================================================
# delete - failure path
# =============================================================================


class TestDeleteFailure:
    def test_delete_failure(
        self, runner: CliRunner, mock_session_manager: MagicMock, mock_resolve_session: MagicMock
    ) -> None:
        session = _make_session()
        mock_session_manager.get.return_value = session
        mock_session_manager.delete.return_value = False
        result = runner.invoke(sessions, ["delete", "sess-abc123", "--yes"])
        assert "Failed to delete session" in result.output


# =============================================================================
# stats - with project
# =============================================================================


class TestStatsWithProject:
    @patch("gobby.cli.sessions.resolve_project_ref", return_value="proj-1")
    def test_stats_with_project(
        self,
        mock_resolve: MagicMock,
        runner: CliRunner,
        mock_session_manager: MagicMock,
        mock_message_manager: MagicMock,
    ) -> None:
        s1 = _make_session(status="active", source="claude")
        mock_session_manager.list.return_value = [s1]
        mock_message_manager.get_all_counts = AsyncMock(return_value={"sess-abc123": 5})
        result = runner.invoke(sessions, ["stats", "--project", "myproj"])
        assert result.exit_code == 0
        assert "Total Sessions: 1" in result.output

    def test_stats_empty(
        self, runner: CliRunner, mock_session_manager: MagicMock, mock_message_manager: MagicMock
    ) -> None:
        mock_session_manager.list.return_value = []
        result = runner.invoke(sessions, ["stats"])
        assert result.exit_code == 0
        assert "No sessions found" in result.output
