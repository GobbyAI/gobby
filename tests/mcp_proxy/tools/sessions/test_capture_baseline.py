"""Tests for capture_baseline_dirty_files MCP tool."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit


class _TestRegistry(InternalToolRegistry):
    """Registry subclass with get_tool for testing."""

    def get_tool(self, name: str) -> Callable[..., Any] | None:
        tool = self._tools.get(name)
        return tool.func if tool else None


def _make_registry(db: Any = None) -> _TestRegistry:
    from gobby.mcp_proxy.tools.sessions._actions import register_action_tools

    session_manager = MagicMock()
    reg = _TestRegistry(name="test", description="test")
    register_action_tools(reg, session_manager=session_manager, db=db)
    return reg


class TestCaptureBaselineDirtyFiles:
    """Tests for capture_baseline_dirty_files persisting to session variables."""

    @pytest.fixture
    def db(self, temp_db: HubDatabase):
        database = temp_db
        yield database

    @patch("gobby.mcp_proxy.tools.sessions._actions.get_dirty_files")
    def test_persists_baseline_to_session_variables(self, mock_dirty, db) -> None:
        """Should store baseline_dirty_files in session variables."""
        mock_dirty.return_value = {"file_a.py", "file_b.py"}

        registry = _make_registry(db=db)
        tool = registry.get_tool("capture_baseline_dirty_files")
        assert tool is not None

        with session_context_for_test("sess-1"):
            result = asyncio.run(tool(project_path="/tmp"))

        assert result["success"] is True
        assert result["file_count"] == 2

        svm = SessionVariableManager(db=db)
        variables = svm.get_variables("sess-1")
        assert sorted(variables["baseline_dirty_files"]) == ["file_a.py", "file_b.py"]

    @patch("gobby.mcp_proxy.tools.sessions._actions.get_dirty_files")
    def test_no_persist_without_session_id(self, mock_dirty, db) -> None:
        """Should not persist when no session context is set."""
        mock_dirty.return_value = {"file_a.py"}

        registry = _make_registry(db=db)
        tool = registry.get_tool("capture_baseline_dirty_files")
        assert tool is not None

        # No session_context_for_test — session_id will be None
        result = asyncio.run(tool(project_path="/tmp"))

        assert result["success"] is True

    @patch("gobby.mcp_proxy.tools.sessions._actions.get_dirty_files")
    def test_no_persist_without_db(self, mock_dirty) -> None:
        """Should succeed without db (no persistence)."""
        mock_dirty.return_value = {"file_a.py"}

        registry = _make_registry(db=None)
        tool = registry.get_tool("capture_baseline_dirty_files")
        assert tool is not None

        with session_context_for_test("sess-1"):
            result = asyncio.run(tool(project_path="/tmp"))

        assert result["success"] is True
        assert result["file_count"] == 1

    @patch("gobby.mcp_proxy.tools.sessions._actions.get_dirty_files")
    def test_empty_baseline_persisted(self, mock_dirty, db) -> None:
        """Should persist empty list when no dirty files."""
        mock_dirty.return_value = set()

        registry = _make_registry(db=db)
        tool = registry.get_tool("capture_baseline_dirty_files")
        assert tool is not None

        with session_context_for_test("sess-1"):
            result = asyncio.run(tool(project_path="/tmp"))

        assert result["success"] is True
        assert result["file_count"] == 0

        svm = SessionVariableManager(db=db)
        variables = svm.get_variables("sess-1")
        assert variables["baseline_dirty_files"] == []
