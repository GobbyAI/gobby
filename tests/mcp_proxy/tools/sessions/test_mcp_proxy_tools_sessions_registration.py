"""Tests for register_session MCP tool."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.runner_init.helpers import ensure_machine_identity
from gobby.storage.machines import MachineNotRegisteredError
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._update_sentinel import UNSET
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


def _make_registry(
    session_manager: Any = None,
) -> InternalToolRegistry:
    return create_session_messages_registry(session_manager=session_manager)


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
        mock_session.project_id = "11111111-1111-4111-8111-111111110001"
        session_manager.register.return_value = mock_session

        registry = _make_registry(session_manager=session_manager)
        register = registry.get_tool("register_session")
        assert register is not None

        result = register(
            external_id="ext-abc",
            source="agent-sdk",
            machine_id="21000000-0000-4000-8000-000000000001",
            project_id="11111111-1111-4111-8111-111111110001",
        )

        assert result["session_id"] == "uuid-123"
        assert result["session_ref"] == "#42"
        assert result["external_id"] == "ext-abc"
        assert result["status"] == "active"
        assert result["source"] == "agent-sdk"

        session_manager.register.assert_called_once_with(
            external_id="ext-abc",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="agent-sdk",
            project_id="11111111-1111-4111-8111-111111110001",
            title=None,
            title_source=None,
            git_branch=None,
            parent_session_id=UNSET,
            agent_depth=0,
            sandbox_enabled=None,
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
        mock_session.project_id = "11111111-1111-4111-8111-111111110001"
        session_manager.register.return_value = mock_session

        registry = _make_registry(session_manager=session_manager)
        register = registry.get_tool("register_session")
        assert register is not None

        r1 = register(
            external_id="ext-1",
            source="claude",
            machine_id="21000000-0000-4000-8000-000000000005",
            project_id="11111111-1111-4111-8111-111111110001",
        )
        r2 = register(
            external_id="ext-1",
            source="claude",
            machine_id="21000000-0000-4000-8000-000000000005",
            project_id="11111111-1111-4111-8111-111111110001",
        )

        assert r1["session_id"] == r2["session_id"]
        assert session_manager.register.call_count == 2

    def test_uuid_ingress_reuses_persisted_machine_neutral_registration(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        # register() resolves absent machine identity to the local machine and
        # rejects explicit foreign machine ids (#19649), so an explicit local
        # machine id must reuse the earlier machine-neutral registration.
        from gobby.utils.machine_id import get_machine_id

        machine_id = get_machine_id()
        assert machine_id is not None
        ensure_machine_identity(session_manager.db, machine_id)
        external_id = "mcp-machine-attribution-transition"
        project_id = str(sample_project["id"])
        canonical = session_manager.register(
            external_id=external_id,
            machine_id=None,
            source="agent-sdk",
            project_id=project_id,
        )
        registry = _make_registry(session_manager=session_manager)
        register = registry.get_tool("register_session")
        assert register is not None

        result = register(
            external_id=external_id,
            source="agent-sdk",
            machine_id=machine_id,
            project_id=project_id,
        )

        row = session_manager.db.fetchone(
            """
            SELECT count(*) AS session_count, max(machine_id::text) AS machine_id
            FROM sessions
            WHERE external_id = %s AND source = %s AND project_id = %s
              AND session_type = 'terminal'
            """,
            (external_id, "agent-sdk", project_id),
        )
        assert result["session_id"] == canonical.id
        assert row is not None
        assert row["session_count"] == 1
        assert row["machine_id"] == machine_id

    @patch(
        "gobby.utils.machine_id.get_machine_id", return_value="21000000-0000-4000-8000-000000000013"
    )
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
            machine_id="21000000-0000-4000-8000-000000000013",
            source="agent-sdk",
            project_id="auto-proj",
            title=None,
            title_source=None,
            git_branch=None,
            parent_session_id=UNSET,
            agent_depth=0,
            sandbox_enabled=None,
        )

    @patch("gobby.utils.machine_id.get_machine_id", return_value=None)
    def test_machine_id_can_remain_unattributed(self, mock_machine_id: MagicMock) -> None:
        session_manager = MagicMock()
        session_manager.register.return_value = MagicMock(
            id="uuid-unattributed",
            seq_num=9,
            external_id="ext-1",
            status="active",
            source="claude",
            project_id="11111111-1111-4111-8111-111111110001",
        )
        registry = _make_registry(session_manager=session_manager)
        register = registry.get_tool("register_session")
        assert register is not None

        result = register(
            external_id="ext-1",
            source="claude",
            project_id="11111111-1111-4111-8111-111111110001",
        )

        assert result["session_id"] == "uuid-unattributed"
        assert session_manager.register.call_args.kwargs["machine_id"] is None

    @patch(
        "gobby.utils.machine_id.get_machine_id", return_value="21000000-0000-4000-8000-000000000005"
    )
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
        mock_session.source = "qwen"
        mock_session.project_id = "11111111-1111-4111-8111-111111110001"
        session_manager.register.return_value = mock_session

        registry = _make_registry(session_manager=session_manager)
        register = registry.get_tool("register_session")
        assert register is not None

        register(
            external_id="ext-opt",
            source="qwen",
            machine_id="21000000-0000-4000-8000-000000000005",
            project_id="11111111-1111-4111-8111-111111110001",
            title="My Session",
            git_branch="feature/foo",
            parent_session_id="parent-uuid",
            agent_depth=2,
        )

        session_manager.register.assert_called_once_with(
            external_id="ext-opt",
            machine_id="21000000-0000-4000-8000-000000000005",
            source="qwen",
            project_id="11111111-1111-4111-8111-111111110001",
            title="My Session",
            title_source="manual",
            git_branch="feature/foo",
            parent_session_id="parent-uuid",
            agent_depth=2,
            sandbox_enabled=None,
        )

    def test_register_session_not_registered_when_session_manager_is_none(self) -> None:
        """register_session is omitted when session_manager is None."""
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
            machine_id="21000000-0000-4000-8000-000000000005",
            project_id="11111111-1111-4111-8111-111111110001",
        )

        assert "error" in result
        assert "DB locked" in result["error"]

    def test_unknown_machine_returns_actionable_stable_error(self) -> None:
        session_manager = MagicMock()
        session_manager.register.side_effect = MachineNotRegisteredError(
            "Machine renamed-machine is not registered; run authenticated enrollment first"
        )

        registry = _make_registry(session_manager=session_manager)
        register = registry.get_tool("register_session")
        assert register is not None

        result = register(
            external_id="ext-unknown-machine",
            source="claude",
            machine_id="21000000-0000-4000-8000-000000000099",
            project_id="11111111-1111-4111-8111-111111110001",
        )

        assert result == {
            "error": (
                "Machine renamed-machine is not registered; run authenticated enrollment first"
            ),
            "error_code": "machine_not_registered",
        }

    def test_ambient_matching_identity_reactivates_and_rereads_same_row(self) -> None:
        session_manager = MagicMock()
        ambient = MagicMock(
            id="uuid-ambient",
            ref="#42",
            external_id="provider-stable-id",
            machine_id=None,
            source="codex",
            project_id="11111111-1111-4111-8111-111111110001",
        )
        revived = MagicMock(
            id="uuid-ambient",
            ref="#42",
            seq_num=42,
            external_id="provider-stable-id",
            status="active",
            source="codex",
            project_id="11111111-1111-4111-8111-111111110001",
        )
        session_manager.resolve_session_reference.return_value = ambient.id
        session_manager.get.return_value = ambient
        session_manager.register.return_value = revived
        register = _make_registry(session_manager=session_manager).get_tool("register_session")
        assert register is not None

        with session_context_for_test("#42"):
            result = register(
                external_id="provider-stable-id",
                source="codex",
                machine_id="21000000-0000-4000-8000-000000000001",
                project_id="11111111-1111-4111-8111-111111110001",
            )

        assert result["session_id"] == ambient.id
        assert result["session_ref"] == "#42"
        assert result["status"] == "active"
        assert session_manager.register.call_args.kwargs["machine_id"] == (
            "21000000-0000-4000-8000-000000000001"
        )
        session_manager.update_status_from_activity.assert_not_called()

    def test_ambient_conflicting_external_id_returns_identity_mismatch(self) -> None:
        session_manager = MagicMock()
        ambient = MagicMock(
            id="uuid-ambient",
            ref="#42",
            external_id="provider-stable-id",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id="11111111-1111-4111-8111-111111110001",
        )
        session_manager.resolve_session_reference.return_value = ambient.id
        session_manager.get.return_value = ambient
        register = _make_registry(session_manager=session_manager).get_tool("register_session")
        assert register is not None

        with session_context_for_test("#42"):
            result = register(
                external_id="conflicting-observed-id",
                source="codex",
                machine_id="21000000-0000-4000-8000-000000000001",
                project_id="11111111-1111-4111-8111-111111110001",
            )

        assert result["error_code"] == "identity_mismatch"
        assert result["session_id"] == ambient.id
        assert result["canonical_external_id"] == "provider-stable-id"
        session_manager.update_status_from_activity.assert_not_called()
        session_manager.register.assert_not_called()

    def test_ambient_resolver_failure_returns_not_found(self) -> None:
        session_manager = MagicMock()
        session_manager.resolve_session_reference.side_effect = ValueError("unknown session")
        register = _make_registry(session_manager=session_manager).get_tool("register_session")
        assert register is not None

        with session_context_for_test("#42"):
            result = register(
                external_id="provider-id",
                source="codex",
                machine_id="21000000-0000-4000-8000-000000000001",
                project_id="11111111-1111-4111-8111-111111110001",
            )

        assert result["error_code"] == "ambient_session_not_found"
        session_manager.get.assert_not_called()
        session_manager.register.assert_not_called()
