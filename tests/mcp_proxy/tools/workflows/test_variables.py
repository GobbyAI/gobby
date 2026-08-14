"""Tests for variable MCP tools.

Covers:
- Scoped runtime variables (session/step) and definition CRUD
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.sessions.compact_markers import SKILL_LIST_VARIABLE_NAMES
from gobby.storage.workflow_definitions import WorkflowDefinitionRow
from gobby.workflows.step_instances import AgentStepInstance
from tests.workflows.step_instance_fixtures import make_step_instance

pytestmark = pytest.mark.unit


def _make_mocks(
    instance: AgentStepInstance | None = None,
    session_variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create mock dependencies for variable functions."""
    session_manager = MagicMock()
    session_manager.resolve_session_reference.return_value = "uuid-session-1"

    instance_manager = MagicMock()
    instance_manager.get_for_session.return_value = instance
    instance_manager.merge_variables.return_value = instance

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
    """Tests for set_variable with session/step scoping."""

    def test_set_variable_with_step_scope_writes_to_instance(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import set_variable

        instance = make_step_instance(
            "uuid-session-1",
            agent_name="dev",
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
            scope="step",
            instance_manager=mocks["instance_manager"],
        )

        assert result["success"] is True
        assert result["value"] is True
        assert result["scope"] == "step"
        mocks["instance_manager"].merge_variables.assert_called_once_with(
            "uuid-session-1",
            {"my_flag": True},
        )
        mocks["instance_manager"].save.assert_not_called()

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

    @pytest.mark.parametrize("name", sorted(SKILL_LIST_VARIABLE_NAMES))
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("plan", id="string"),
            pytest.param(None, id="null"),
            pytest.param({"name": "plan"}, id="object"),
            pytest.param([""], id="empty-name"),
            pytest.param(["   "], id="whitespace-name"),
            pytest.param(["plan", 7], id="non-string-item"),
        ],
    )
    def test_set_variable_rejects_invalid_skill_lists_atomically(
        self,
        name: str,
        value: Any,
    ) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import set_variable

        mocks = _make_mocks()

        result = set_variable(
            mocks["session_manager"],
            mocks["db"],
            name=name,
            value=value,
            session_id="#1",
            session_var_manager=mocks["session_var_manager"],
        )

        assert result == {
            "success": False,
            "error": f"Variable '{name}' requires a JSON array of non-empty skill names.",
        }
        mocks["session_var_manager"].set_variable.assert_not_called()
        mocks["instance_manager"].merge_variables.assert_not_called()

    @pytest.mark.parametrize("name", sorted(SKILL_LIST_VARIABLE_NAMES))
    def test_set_variable_accepts_empty_skill_lists(self, name: str) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import set_variable

        mocks = _make_mocks()

        result = set_variable(
            mocks["session_manager"],
            mocks["db"],
            name=name,
            value=[],
            session_id="#1",
            session_var_manager=mocks["session_var_manager"],
        )

        assert result["success"] is True
        mocks["session_var_manager"].set_variable.assert_called_once_with(
            "uuid-session-1", name, []
        )

    @pytest.mark.parametrize(
        "name",
        [
            "enforce_tool_schema_check",
            "unlocked_tools",
            "listed_servers",
            "consecutive_tool_blocks",
            "open_tool_errors",
            "_last_blocked_tool",
            "edit_write_stop_blocks",
        ],
    )
    def test_set_variable_blocks_runtime_managed_variables(self, name: str) -> None:
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
        assert "managed by the workflow runtime" in result["error"]
        mocks["session_var_manager"].set_variable.assert_not_called()

    def test_set_variable_blocks_open_tool_errors_in_workflow_scope(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import set_variable

        mocks = _make_mocks()

        result = set_variable(
            mocks["session_manager"],
            mocks["db"],
            name="open_tool_errors",
            value=[],
            session_id="#1",
            scope="step",
            instance_manager=mocks["instance_manager"],
        )

        assert result["success"] is False
        assert "managed by the workflow runtime" in result["error"]
        mocks["instance_manager"].merge_variables.assert_not_called()

    def test_set_variable_with_step_scope_not_found(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import set_variable

        mocks = _make_mocks(instance=None)

        result = set_variable(
            mocks["session_manager"],
            mocks["db"],
            name="flag",
            value=True,
            session_id="#1",
            scope="step",
            instance_manager=mocks["instance_manager"],
        )

        assert result["success"] is False
        assert "agent-step instance" in result["error"]
        mocks["instance_manager"].merge_variables.assert_called_once_with(
            "uuid-session-1",
            {"flag": True},
        )


class TestGetVariableScoped:
    """Tests for get_variable with session/step scoping."""

    def test_get_variable_with_step_scope_reads_from_instance(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import get_variable

        instance = make_step_instance(
            "uuid-session-1",
            agent_name="dev",
            current_step="work",
            variables={"my_flag": True},
        )
        mocks = _make_mocks(instance=instance)

        result = get_variable(
            mocks["session_manager"],
            mocks["db"],
            name="my_flag",
            session_id="#1",
            scope="step",
            instance_manager=mocks["instance_manager"],
        )

        assert result["success"] is True
        assert result["value"] is True
        assert result["exists"] is True
        mocks["instance_manager"].get_for_session.assert_called_once_with("uuid-session-1")

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

    def test_get_all_variables_with_step_scope(self) -> None:
        from gobby.mcp_proxy.tools.workflows._variables import get_variable

        instance = make_step_instance(
            "uuid-session-1",
            agent_name="dev",
            current_step="work",
            variables={"a": 1, "b": 2},
        )
        mocks = _make_mocks(instance=instance)

        result = get_variable(
            mocks["session_manager"],
            mocks["db"],
            name=None,
            session_id="#1",
            scope="step",
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
    tags: list[str] | None = None,
    deleted_at: datetime | None = None,
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
        source="custom",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        description=description,
        tags=tags or ["user"],
        deleted_at=deleted_at,
    )


@contextmanager
def _patch_auto_export(collision: bool = False) -> Iterator[None]:
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

        deleted_row = _make_var_row(
            "recycled",
            deleted_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
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
