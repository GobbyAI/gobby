"""Tests for scoped variable MCP tools (workflow-scoped and session-scoped)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.workflows.definitions import WorkflowInstance

pytestmark = pytest.mark.unit


def _make_mocks(
    instance: WorkflowInstance | None = None,
    session_variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create mock dependencies for variable functions."""
    session_manager = MagicMock()
    session_manager.resolve_session_reference.return_value = "uuid-session-1"

    instance_manager = MagicMock()
    instance_manager.get_instance.return_value = instance

    session_var_manager = MagicMock()
    session_var_manager.get_variables.return_value = session_variables or {}

    db = MagicMock()

    return {
        "session_manager": session_manager,
        "instance_manager": instance_manager,
        "session_var_manager": session_var_manager,
        "db": db,
    }


class TestSetVariableScoped:
    """Tests for set_variable with workflow scoping."""

    def test_set_variable_with_workflow_writes_to_instance(self) -> None:
        """set_variable(workflow='dev') writes to workflow_instances.variables."""
        from gobby.mcp_proxy.tools.workflows._variables import set_variable

        instance = WorkflowInstance(
            id="inst-1",
            session_id="uuid-session-1",
            workflow_name="dev",
            enabled=True,
            priority=10,
            current_step="work",
            variables={"existing": "val"},
        )
        mocks = _make_mocks(instance=instance)

        result = set_variable(
            mocks["session_manager"],
            mocks["db"],
            name="my_flag",
            value=True,
            session_id="#1",
            workflow="dev",
            instance_manager=mocks["instance_manager"],
        )

        assert result["ok"] is True
        assert result["value"] is True
        # Verify instance_manager was used to save
        mocks["instance_manager"].get_instance.assert_called_once_with("uuid-session-1", "dev")
        mocks["instance_manager"].save_instance.assert_called_once()
        saved = mocks["instance_manager"].save_instance.call_args[0][0]
        assert saved.variables["my_flag"] is True
        assert saved.variables["existing"] == "val"

    def test_set_variable_without_workflow_writes_to_session_variables(self) -> None:
        """set_variable() without workflow writes to session_variables."""
        from gobby.mcp_proxy.tools.workflows._variables import set_variable

        mocks = _make_mocks()

        result = set_variable(
            mocks["session_manager"],
            mocks["db"],
            name="counter",
            value=42,
            session_id="#1",
            session_var_manager=mocks["session_var_manager"],
        )

        assert result["ok"] is True
        assert result["value"] == 42
        # Should write to session_var_manager
        mocks["session_var_manager"].set_variable.assert_called_once_with(
            "uuid-session-1", "counter", 42
        )

    def test_set_variable_with_workflow_not_found(self) -> None:
        """set_variable(workflow='unknown') errors if no instance found."""
        from gobby.mcp_proxy.tools.workflows._variables import set_variable

        mocks = _make_mocks(instance=None)

        result = set_variable(
            mocks["session_manager"],
            mocks["db"],
            name="flag",
            value=True,
            session_id="#1",
            workflow="unknown",
            instance_manager=mocks["instance_manager"],
        )

        assert result["ok"] is False
        assert "unknown" in result["error"]


class TestSetSessionVariable:
    """Tests for set_session_variable tool."""

    def test_set_session_variable_writes_to_session_variables(self) -> None:
        """set_session_variable() writes to session_variables table."""
        from gobby.mcp_proxy.tools.workflows._variables import set_session_variable

        mocks = _make_mocks()

        result = set_session_variable(
            mocks["session_manager"],
            mocks["session_var_manager"],
            name="shared_flag",
            value=True,
            session_id="#1",
        )

        assert result["ok"] is True
        assert result["value"] is True
        mocks["session_var_manager"].set_variable.assert_called_once_with(
            "uuid-session-1", "shared_flag", True
        )


class TestGetVariableScoped:
    """Tests for get_variable with workflow scoping."""

    def test_get_variable_with_workflow_reads_from_instance(self) -> None:
        """get_variable(workflow='dev') reads from workflow_instances.variables."""
        from gobby.mcp_proxy.tools.workflows._variables import get_variable

        instance = WorkflowInstance(
            id="inst-1",
            session_id="uuid-session-1",
            workflow_name="dev",
            enabled=True,
            priority=10,
            current_step="work",
            variables={"my_flag": True, "counter": 5},
        )
        mocks = _make_mocks(instance=instance)

        result = get_variable(
            mocks["session_manager"],
            mocks["db"],
            name="my_flag",
            session_id="#1",
            workflow="dev",
            instance_manager=mocks["instance_manager"],
        )

        assert result["ok"] is True
        assert result["value"] is True
        assert result["exists"] is True
        mocks["instance_manager"].get_instance.assert_called_once_with("uuid-session-1", "dev")

    def test_get_variable_without_workflow_reads_from_session_variables(self) -> None:
        """get_variable() without workflow reads from session_variables."""
        from gobby.mcp_proxy.tools.workflows._variables import get_variable

        mocks = _make_mocks(session_variables={"counter": 42, "flag": True})

        result = get_variable(
            mocks["session_manager"],
            mocks["db"],
            name="counter",
            session_id="#1",
            session_var_manager=mocks["session_var_manager"],
        )

        assert result["ok"] is True
        assert result["value"] == 42
        assert result["exists"] is True
        mocks["session_var_manager"].get_variables.assert_called_once_with("uuid-session-1")

    def test_get_all_variables_with_workflow(self) -> None:
        """get_variable(workflow='dev') without name returns all workflow variables."""
        from gobby.mcp_proxy.tools.workflows._variables import get_variable

        instance = WorkflowInstance(
            id="inst-1",
            session_id="uuid-session-1",
            workflow_name="dev",
            enabled=True,
            priority=10,
            current_step="work",
            variables={"a": 1, "b": 2},
        )
        mocks = _make_mocks(instance=instance)

        result = get_variable(
            mocks["session_manager"],
            mocks["db"],
            name=None,
            session_id="#1",
            workflow="dev",
            instance_manager=mocks["instance_manager"],
        )

        assert result["ok"] is True
        assert result["variables"] == {"a": 1, "b": 2}
