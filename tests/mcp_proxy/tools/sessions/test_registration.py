"""Tests for register_session MCP tool."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions import create_session_messages_registry

pytestmark = pytest.mark.unit


class _TestRegistry(InternalToolRegistry):
    """Registry subclass with get_tool for testing."""

    def get_tool(self, name: str) -> Callable[..., Any] | None:
        tool = self._tools.get(name)
        return tool.func if tool else None


def _make_registry(
    session_manager: Any = None,
) -> _TestRegistry:
    real = create_session_messages_registry(session_manager=session_manager)
    test_reg = _TestRegistry(name=real.name, description=real.description)
    test_reg._tools = real._tools
    return test_reg


class TestRegisterSession:
    """Tests for register_session tool."""

    def test_tool_is_registered(self) -> None:
        """register_session tool exists in the registry."""
        session_manager = MagicMock()
        registry = _make_registry(session_manager=session_manager)
        assert registry.get_tool("register_session") is not None

    def test_basic_registration(self) -> None:
        """Registers a session and returns expected fields."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "uuid-123"
        mock_session.seq_num = 42
        mock_session.external_id = "ext-abc"
        mock_session.status = "active"
        mock_session.source = "agent-sdk"
        mock_session.project_id = "proj-1"
        session_manager.register.return_value = mock_session

        registry = _make_registry(session_manager=session_manager)
        register = registry.get_tool("register_session")
        assert register is not None

        result = register(
            external_id="ext-abc",
            source="agent-sdk",
            machine_id="machine-1",
            project_id="proj-1",
        )

        assert result["session_id"] == "uuid-123"
        assert result["session_ref"] == "#42"
        assert result["external_id"] == "ext-abc"
        assert result["status"] == "active"
        assert result["source"] == "agent-sdk"

        session_manager.register.assert_called_once_with(
            external_id="ext-abc",
            machine_id="machine-1",
            source="agent-sdk",
            project_id="proj-1",
            title=None,
            git_branch=None,
            parent_session_id=None,
            agent_depth=0,
        )

    def test_idempotent_returns_existing(self) -> None:
        """Calling twice with same identity returns same session."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "uuid-123"
        mock_session.seq_num = 7
        mock_session.external_id = "ext-1"
        mock_session.status = "active"
        mock_session.source = "claude"
        mock_session.project_id = "proj-1"
        session_manager.register.return_value = mock_session

        registry = _make_registry(session_manager=session_manager)
        register = registry.get_tool("register_session")
        assert register is not None

        r1 = register(external_id="ext-1", source="claude", machine_id="m1", project_id="proj-1")
        r2 = register(external_id="ext-1", source="claude", machine_id="m1", project_id="proj-1")

        assert r1["session_id"] == r2["session_id"]
        assert session_manager.register.call_count == 2

    @patch("gobby.utils.machine_id.get_machine_id", return_value="auto-machine")
    @patch(
        "gobby.utils.project_context.get_project_context",
        return_value={"id": "auto-proj"},
    )
    def test_auto_resolves_machine_and_project(
        self, mock_project_ctx: MagicMock, mock_machine_id: MagicMock
    ) -> None:
        """Auto-resolves machine_id and project_id when omitted."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "uuid-auto"
        mock_session.seq_num = 1
        mock_session.external_id = "ext-auto"
        mock_session.status = "active"
        mock_session.source = "agent-sdk"
        mock_session.project_id = "auto-proj"
        session_manager.register.return_value = mock_session

        registry = _make_registry(session_manager=session_manager)
        register = registry.get_tool("register_session")
        assert register is not None

        result = register(external_id="ext-auto", source="agent-sdk")

        assert result["session_id"] == "uuid-auto"
        session_manager.register.assert_called_once_with(
            external_id="ext-auto",
            machine_id="auto-machine",
            source="agent-sdk",
            project_id="auto-proj",
            title=None,
            git_branch=None,
            parent_session_id=None,
            agent_depth=0,
        )

    @patch("gobby.utils.machine_id.get_machine_id", return_value=None)
    def test_error_when_machine_id_unresolvable(self, mock_machine_id: MagicMock) -> None:
        """Returns error when machine_id can't be resolved."""
        session_manager = MagicMock()
        registry = _make_registry(session_manager=session_manager)
        register = registry.get_tool("register_session")
        assert register is not None

        result = register(external_id="ext-1", source="claude")
        assert "error" in result
        assert "machine_id" in result["error"]

    @patch("gobby.utils.machine_id.get_machine_id", return_value="m1")
    @patch("gobby.utils.project_context.get_project_context", return_value=None)
    def test_error_when_project_id_unresolvable(
        self, mock_project_ctx: MagicMock, mock_machine_id: MagicMock
    ) -> None:
        """Returns error when project_id can't be resolved."""
        session_manager = MagicMock()
        registry = _make_registry(session_manager=session_manager)
        register = registry.get_tool("register_session")
        assert register is not None

        result = register(external_id="ext-1", source="claude")
        assert "error" in result
        assert "project_id" in result["error"]

    def test_passes_optional_fields(self) -> None:
        """Optional fields are forwarded to session_manager.register()."""
        session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "uuid-opt"
        mock_session.seq_num = 99
        mock_session.external_id = "ext-opt"
        mock_session.status = "active"
        mock_session.source = "gemini"
        mock_session.project_id = "proj-1"
        session_manager.register.return_value = mock_session

        registry = _make_registry(session_manager=session_manager)
        register = registry.get_tool("register_session")
        assert register is not None

        register(
            external_id="ext-opt",
            source="gemini",
            machine_id="m1",
            project_id="proj-1",
            title="My Session",
            git_branch="feature/foo",
            parent_session_id="parent-uuid",
            agent_depth=2,
        )

        session_manager.register.assert_called_once_with(
            external_id="ext-opt",
            machine_id="m1",
            source="gemini",
            project_id="proj-1",
            title="My Session",
            git_branch="feature/foo",
            parent_session_id="parent-uuid",
            agent_depth=2,
        )

    def test_session_manager_none_returns_error(self) -> None:
        """Returns error when session_manager is None."""
        registry = _make_registry(session_manager=None)
        register = registry.get_tool("register_session")
        # Tool won't be registered if session_manager is None (factory guard)
        assert register is None

    def test_register_exception_returns_error(self) -> None:
        """Returns error dict on storage exception."""
        session_manager = MagicMock()
        session_manager.register.side_effect = RuntimeError("DB locked")

        registry = _make_registry(session_manager=session_manager)
        register = registry.get_tool("register_session")
        assert register is not None

        result = register(
            external_id="ext-1",
            source="claude",
            machine_id="m1",
            project_id="proj-1",
        )

        assert "error" in result
        assert "DB locked" in result["error"]
