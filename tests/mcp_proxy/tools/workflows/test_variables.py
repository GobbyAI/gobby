"""Tests for variable MCP tools.

Covers:
- Scoped runtime variables (workflow-scoped and session-scoped)
- Variable definition CRUD (create, update, delete, export, list, get)
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.workflow_definitions import WorkflowDefinitionRow
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

        assert result["success"] is True
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

        assert result["success"] is True
        assert result["value"] == 42
        # Should write to session_var_manager
        mocks["session_var_manager"].set_variable.assert_called_once_with(
            "uuid-session-1", "counter", 42
        )

    @pytest.mark.parametrize(
        "name",
        ["verification_evidence_recorded", "verification_evidence"],
    )
    def test_set_variable_blocks_verification_evidence_variables(self, name: str) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import set_variable

        mocks = _make_mocks()

        result = set_variable(
            mocks["session_manager"],
            mocks["db"],
            name=name,
            value=True,
            session_id="#1",
            session_var_manager=mocks["session_var_manager"],
        )

        assert result["success"] is False
        assert "managed by verification observers" in result["error"]
        mocks["session_var_manager"].set_variable.assert_not_called()

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

        assert result["success"] is False
        assert "unknown" in result["error"]


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

        assert result["success"] is True
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

        assert result["success"] is True
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

        assert result["success"] is True
        assert result["variables"] == {"a": 1, "b": 2}


# ═══════════════════════════════════════════════════════════════════════════
# Variable definition CRUD tests
# ═══════════════════════════════════════════════════════════════════════════


def _make_var_row(
    name: str = "my_var",
    value: Any = "hello",
    description: str | None = None,
    source: str = "user",
    tags: list[str] | None = None,
    deleted_at: str | None = None,
) -> WorkflowDefinitionRow:
    """Create a WorkflowDefinitionRow for a variable definition."""
    body = {"variable": name, "value": value}
    if description:
        body["description"] = description
    return WorkflowDefinitionRow(
        id=f"id-{name}",
        name=name,
        workflow_type="variable",
        enabled=True,
        priority=100,
        definition_json=json.dumps(body),
        source=source,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        description=description,
        tags=tags or ["user"],
        deleted_at=deleted_at,
    )


@contextmanager
def _patch_auto_export(collision: bool = False):
    """Patch auto-export helpers at their source module (they're lazy-imported)."""
    with (
        patch(
            "gobby.mcp_proxy.tools.workflows._auto_export.has_gobby_name_collision",
            return_value=collision,
        ),
        patch(
            "gobby.mcp_proxy.tools.workflows._auto_export.auto_export_definition",
        ),
        patch(
            "gobby.mcp_proxy.tools.workflows._auto_export.auto_delete_definition",
        ),
    ):
        yield


def _mock_def_manager(
    existing: WorkflowDefinitionRow | None = None,
    deleted: WorkflowDefinitionRow | None = None,
) -> MagicMock:
    """Create a mock LocalWorkflowDefinitionManager."""
    mgr = MagicMock()
    mgr.db = MagicMock()

    def get_by_name(name: str, include_deleted: bool = False) -> WorkflowDefinitionRow | None:
        if include_deleted and deleted:
            return deleted
        if existing and existing.source != "template":
            return existing
        return None

    mgr.get_by_name.side_effect = get_by_name
    return mgr


class TestCreateVariable:
    """Tests for create_variable."""

    def test_create_variable_success(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import create_variable

        mgr = _mock_def_manager()
        created_row = _make_var_row("test_var", "hello", "A test variable")
        mgr.create.return_value = created_row

        with _patch_auto_export():
            result = create_variable(mgr, "test_var", "hello", "A test variable")

        assert result["success"] is True
        assert result["variable"]["name"] == "test_var"
        assert result["variable"]["value"] == "hello"
        mgr.create.assert_called_once()
        call_kwargs = mgr.create.call_args
        assert call_kwargs[1]["workflow_type"] == "variable"
        assert call_kwargs[1]["source"] == "installed"

    def test_create_variable_name_collision(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import create_variable

        mgr = _mock_def_manager()

        with _patch_auto_export(collision=True):
            result = create_variable(mgr, "gobby_var", "val")

        assert result["success"] is False
        assert "conflicts" in result["error"]
        mgr.create.assert_not_called()

    def test_create_variable_already_exists(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import create_variable

        existing = _make_var_row("dup_var")
        mgr = _mock_def_manager(existing=existing)

        with _patch_auto_export():
            result = create_variable(mgr, "dup_var", "val")

        assert result["success"] is False
        assert "already exists" in result["error"]

    def test_create_variable_hard_deletes_soft_deleted_blocker(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import create_variable

        deleted_row = _make_var_row("recycled", deleted_at="2026-01-01T00:00:00")
        mgr = _mock_def_manager(deleted=deleted_row)
        mgr.create.return_value = _make_var_row("recycled", "new_val")

        with _patch_auto_export():
            result = create_variable(mgr, "recycled", "new_val")

        assert result["success"] is True
        mgr.hard_delete.assert_called_once_with(deleted_row.id)


class TestUpdateVariable:
    """Tests for update_variable."""

    def test_update_variable_success(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import update_variable

        existing = _make_var_row("my_var", "old_val", "old desc")
        mgr = _mock_def_manager(existing=existing)
        updated_row = _make_var_row("my_var", "new_val", "new desc")
        mgr.update.return_value = updated_row

        with _patch_auto_export():
            result = update_variable(mgr, "my_var", value="new_val", description="new desc")

        assert result["success"] is True
        assert result["variable"]["value"] == "new_val"
        mgr.update.assert_called_once()

    def test_update_variable_not_found(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import update_variable

        mgr = _mock_def_manager()
        result = update_variable(mgr, "nonexistent", value="x")
        assert result["success"] is False
        assert "not found" in result["error"]


class TestDeleteVariable:
    """Tests for delete_variable."""

    def test_delete_variable_success(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import delete_variable

        existing = _make_var_row("doomed")
        mgr = _mock_def_manager(existing=existing)
        mgr.delete.return_value = True

        with _patch_auto_export():
            result = delete_variable(mgr, "doomed")

        assert result["success"] is True
        assert result["deleted"]["name"] == "doomed"
        mgr.delete.assert_called_once_with(existing.id)

    def test_delete_variable_protects_bundled(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import delete_variable

        bundled = _make_var_row("bundled_var", tags=["gobby"])
        mgr = _mock_def_manager(existing=bundled)
        result = delete_variable(mgr, "bundled_var")
        assert result["success"] is False
        assert "bundled" in result["error"]
        mgr.delete.assert_not_called()

    def test_delete_variable_force_overrides_protection(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import delete_variable

        bundled = _make_var_row("bundled_var", tags=["gobby"])
        mgr = _mock_def_manager(existing=bundled)
        mgr.delete.return_value = True

        with _patch_auto_export():
            result = delete_variable(mgr, "bundled_var", force=True)

        assert result["success"] is True
        mgr.delete.assert_called_once()

    def test_delete_variable_not_found(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import delete_variable

        mgr = _mock_def_manager()
        result = delete_variable(mgr, "ghost")
        assert result["success"] is False
        assert "not found" in result["error"]


class TestExportVariable:
    """Tests for export_variable."""

    def test_export_variable_success(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import export_variable

        existing = _make_var_row("cfg_timeout", 30, "Request timeout in seconds")
        mgr = _mock_def_manager(existing=existing)
        result = export_variable(mgr, "cfg_timeout")

        assert result["success"] is True
        assert "yaml_content" in result
        assert "cfg_timeout" in result["yaml_content"]
        assert "30" in result["yaml_content"]

    def test_export_variable_not_found(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import export_variable

        mgr = _mock_def_manager()
        result = export_variable(mgr, "ghost")
        assert result["success"] is False
        assert "not found" in result["error"]


class TestListVariables:
    """Tests for list_variables."""

    def test_list_variables_returns_all(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import list_variables

        rows = [
            _make_var_row("var_a", "a"),
            _make_var_row("var_b", "b"),
        ]
        mgr = MagicMock()
        mgr.list_all.return_value = rows
        result = list_variables(mgr)

        assert result["success"] is True
        assert result["count"] == 2
        names = [v["name"] for v in result["variables"]]
        assert "var_a" in names
        assert "var_b" in names


class TestGetVariableDefinition:
    """Tests for get_variable_definition."""

    def test_get_variable_definition_success(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import get_variable_definition

        existing = _make_var_row("my_var", "hello", "A greeting")
        mgr = _mock_def_manager(existing=existing)
        result = get_variable_definition(mgr, "my_var")

        assert result["success"] is True
        assert result["variable"]["name"] == "my_var"
        assert result["variable"]["value"] == "hello"

    def test_get_variable_definition_not_found(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import get_variable_definition

        mgr = _mock_def_manager()
        result = get_variable_definition(mgr, "ghost")
        assert result["success"] is False
        assert "not found" in result["error"]
