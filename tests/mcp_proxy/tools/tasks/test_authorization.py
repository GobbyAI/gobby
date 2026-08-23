"""Tests for the MCP-layer claim-authority guard (#20821)."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._authorization import (
    has_delegated_agent_run,
    require_claim_authority,
)
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_delete import register_delete_task
from gobby.mcp_proxy.tools.tasks._session import create_session_registry
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit

OWNER_SESSION = "11111111-1111-4111-8111-aaaaaaaaaaaa"
CALLER_SESSION = "11111111-1111-4111-8111-bbbbbbbbbbbb"
TASK_UUID = "550e8400-e29b-41d4-a716-446655440000"


def _task(claimed_by: str | None) -> Task:
    return Task(
        id=TASK_UUID,
        project_id="11111111-1111-4111-8111-111111110001",
        title="Guarded Task",
        priority=2,
        task_type="task",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        updated_at=datetime(2024, 1, 1, tzinfo=UTC),
        claimed_by_session_id=claimed_by,
    )


@pytest.fixture
def task_manager() -> MagicMock:
    manager = MagicMock(spec=LocalTaskManager)
    manager.db = MagicMock()
    manager.db.fetchone.return_value = None
    return manager


class TestRequireClaimAuthority:
    def test_unclaimed_allowed_without_session_context(self, task_manager: MagicMock) -> None:
        assert require_claim_authority(task_manager, _task(None), "update_task") is None
        task_manager.db.fetchone.assert_not_called()

    def test_owner_allowed(self, task_manager: MagicMock) -> None:
        with session_context_for_test(OWNER_SESSION):
            denied = require_claim_authority(task_manager, _task(OWNER_SESSION), "update_task")
        assert denied is None
        task_manager.db.fetchone.assert_not_called()

    def test_foreign_session_refused(self, task_manager: MagicMock) -> None:
        with session_context_for_test(CALLER_SESSION):
            denied = require_claim_authority(task_manager, _task(OWNER_SESSION), "update_task")
        assert denied is not None
        assert denied["error_code"] == "TASK_CLAIM_CONFLICT"
        assert denied["claimed_by"] == OWNER_SESSION
        assert "gobby-agents:send_message" in denied["message"]
        assert "claim_task" in denied["message"]

    def test_no_session_on_claimed_refused(self, task_manager: MagicMock) -> None:
        denied = require_claim_authority(task_manager, _task(OWNER_SESSION), "update_task")
        assert denied is not None
        assert denied["error_code"] == "SESSION_REQUIRED"
        assert denied["claimed_by"] == OWNER_SESSION

    def test_delegated_caller_allowed(self, task_manager: MagicMock) -> None:
        task_manager.db.fetchone.return_value = {"id": "run-1"}
        with session_context_for_test(CALLER_SESSION):
            denied = require_claim_authority(task_manager, _task(OWNER_SESSION), "update_task")
        assert denied is None


class TestHasDelegatedAgentRun:
    def test_queries_lineage_in_both_directions(self) -> None:
        db = MagicMock()
        db.fetchone.return_value = {"id": "run-1"}
        assert has_delegated_agent_run(
            db,
            caller_session_id=CALLER_SESSION,
            task_id=TASK_UUID,
            owner_session_id=OWNER_SESSION,
        )
        params = db.fetchone.call_args.args[1]
        assert params == (
            TASK_UUID,
            CALLER_SESSION,
            OWNER_SESSION,
            OWNER_SESSION,
            CALLER_SESSION,
        )

    def test_no_owner_short_circuits(self) -> None:
        db = MagicMock()
        assert not has_delegated_agent_run(
            db,
            caller_session_id=CALLER_SESSION,
            task_id=TASK_UUID,
            owner_session_id=None,
        )
        db.fetchone.assert_not_called()

    def test_missing_row_refused(self) -> None:
        db = MagicMock()
        db.fetchone.return_value = None
        assert not has_delegated_agent_run(
            db,
            caller_session_id=CALLER_SESSION,
            task_id=TASK_UUID,
            owner_session_id=OWNER_SESSION,
        )

    def test_non_string_run_id_refused(self) -> None:
        db = MagicMock()
        db.fetchone.return_value = {"id": 42}
        assert not has_delegated_agent_run(
            db,
            caller_session_id=CALLER_SESSION,
            task_id=TASK_UUID,
            owner_session_id=OWNER_SESSION,
        )

    def test_lookup_failure_fails_closed(self) -> None:
        db = MagicMock()
        db.fetchone.side_effect = RuntimeError("db down")
        assert not has_delegated_agent_run(
            db,
            caller_session_id=CALLER_SESSION,
            task_id=TASK_UUID,
            owner_session_id=OWNER_SESSION,
        )


def _delete_registry(task_manager: MagicMock) -> InternalToolRegistry:
    ctx = RegistryContext(task_manager=task_manager)
    registry = InternalToolRegistry(name="test-delete", description="test")
    register_delete_task(registry, ctx)
    return registry


class TestDeleteTaskGuard:
    def test_cascade_defaults_false_in_schema(self, task_manager: MagicMock) -> None:
        registry = _delete_registry(task_manager)
        schema = registry.get_schema("delete_task")
        assert schema is not None
        assert schema["inputSchema"]["properties"]["cascade"]["default"] is False

    def test_delete_unclaimed_defaults_to_no_cascade(self, task_manager: MagicMock) -> None:
        task_manager.get_task.return_value = _task(None)
        task_manager.delete_task.return_value = True
        tool = _delete_registry(task_manager).get_tool("delete_task")
        assert tool is not None

        result = tool(task_id=TASK_UUID)

        assert result["success"] is True
        task_manager.delete_task.assert_called_once_with(TASK_UUID, cascade=False, unlink=False)

    def test_delete_foreign_claimed_refused(self, task_manager: MagicMock) -> None:
        task_manager.get_task.return_value = _task(OWNER_SESSION)
        tool = _delete_registry(task_manager).get_tool("delete_task")
        assert tool is not None

        with session_context_for_test(CALLER_SESSION):
            result = tool(task_id=TASK_UUID)

        assert result["error_code"] == "TASK_CLAIM_CONFLICT"
        task_manager.delete_task.assert_not_called()


class TestLinkTaskToSessionGuard:
    def test_link_explicit_session_without_context_refused(self, task_manager: MagicMock) -> None:
        task_manager.get_task.return_value = _task(None)
        ctx = RegistryContext(task_manager=task_manager)
        tool = create_session_registry(ctx).get_tool("link_task_to_session")
        assert tool is not None

        result = tool(task_id=TASK_UUID, session_id="#42")

        assert result["error_code"] == "SESSION_REQUIRED"

    def test_link_foreign_claimed_task_refused(self, task_manager: MagicMock) -> None:
        task_manager.get_task.return_value = _task(OWNER_SESSION)
        ctx = RegistryContext(task_manager=task_manager)
        tool = create_session_registry(ctx).get_tool("link_task_to_session")
        assert tool is not None

        with session_context_for_test(CALLER_SESSION):
            result = tool(task_id=TASK_UUID)

        assert result["error_code"] == "TASK_CLAIM_CONFLICT"
        assert result["claimed_by"] == OWNER_SESSION
