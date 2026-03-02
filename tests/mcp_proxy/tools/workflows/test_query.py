"""Tests for workflow query tools — status and list_workflows with DB."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.workflow_definitions import WorkflowDefinitionRow
from gobby.workflows.definitions import WorkflowInstance

pytestmark = pytest.mark.unit


def _make_mocks(
    instances: list[WorkflowInstance] | None = None,
    session_variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create mock dependencies for query functions."""
    session_manager = MagicMock()
    session_manager.resolve_session_reference.return_value = "uuid-session-1"

    instance_manager = MagicMock()
    instance_manager.get_active_instances.return_value = instances or []

    session_var_manager = MagicMock()
    session_var_manager.get_variables.return_value = session_variables or {}

    return {
        "session_manager": session_manager,
        "instance_manager": instance_manager,
        "session_var_manager": session_var_manager,
    }


class TestGetWorkflowStatusMultiWorkflow:
    """Tests for get_workflow_status with multi-workflow support."""

    def test_returns_all_active_instances(self) -> None:
        """get_workflow_status returns all active workflow instances."""
        from gobby.mcp_proxy.tools.workflows._query import get_workflow_status

        instances = [
            WorkflowInstance(
                id="inst-1",
                session_id="uuid-session-1",
                workflow_name="auto-task",
                enabled=True,
                priority=10,
                current_step="work",
                variables={"session_task": "task-uuid"},
            ),
            WorkflowInstance(
                id="inst-2",
                session_id="uuid-session-1",
                workflow_name="plan-execute",
                enabled=True,
                priority=20,
                current_step="plan",
                variables={"plan_ready": False},
            ),
        ]
        mocks = _make_mocks(
            instances=instances,
            session_variables={"counter": 5},
        )

        result = get_workflow_status(
            mocks["session_manager"],
            session_id="#1",
            instance_manager=mocks["instance_manager"],
            session_var_manager=mocks["session_var_manager"],
        )

        assert result["success"] is True
        assert len(result["workflows"]) == 2
        assert result["workflows"][0]["workflow_name"] == "auto-task"
        assert result["workflows"][0]["current_step"] == "work"
        assert result["workflows"][0]["variables"] == {"session_task": "task-uuid"}
        assert result["workflows"][1]["workflow_name"] == "plan-execute"

    def test_shows_session_variables_separately(self) -> None:
        """get_workflow_status shows session variables in a separate field."""
        from gobby.mcp_proxy.tools.workflows._query import get_workflow_status

        instances = [
            WorkflowInstance(
                id="inst-1",
                session_id="uuid-session-1",
                workflow_name="auto-task",
                enabled=True,
                priority=10,
                current_step="work",
                variables={},
            ),
        ]
        mocks = _make_mocks(
            instances=instances,
            session_variables={"shared_flag": True, "counter": 42},
        )

        result = get_workflow_status(
            mocks["session_manager"],
            session_id="#1",
            instance_manager=mocks["instance_manager"],
            session_var_manager=mocks["session_var_manager"],
        )

        assert result["success"] is True
        assert result["session_variables"] == {"shared_flag": True, "counter": 42}

    def test_no_instance_manager_returns_no_workflows(self) -> None:
        """get_workflow_status without instance_manager returns has_workflow=False."""
        from gobby.mcp_proxy.tools.workflows._query import get_workflow_status

        mocks = _make_mocks()

        result = get_workflow_status(
            mocks["session_manager"],
            session_id="#1",
        )

        assert result["success"] is True
        assert result["has_workflow"] is False

    def test_each_instance_shows_priority_and_enabled(self) -> None:
        """Each workflow instance includes priority and enabled fields."""
        from gobby.mcp_proxy.tools.workflows._query import get_workflow_status

        instances = [
            WorkflowInstance(
                id="inst-1",
                session_id="uuid-session-1",
                workflow_name="dev",
                enabled=True,
                priority=5,
                current_step="code",
                variables={},
            ),
        ]
        mocks = _make_mocks(instances=instances)

        result = get_workflow_status(
            mocks["session_manager"],
            session_id="#1",
            instance_manager=mocks["instance_manager"],
            session_var_manager=mocks["session_var_manager"],
        )

        assert result["success"] is True
        wf = result["workflows"][0]
        assert wf["enabled"] is True
        assert wf["priority"] == 5

    def test_empty_instances_returns_no_workflows(self) -> None:
        """get_workflow_status with no active instances returns empty list."""
        from gobby.mcp_proxy.tools.workflows._query import get_workflow_status

        mocks = _make_mocks(instances=[])

        result = get_workflow_status(
            mocks["session_manager"],
            session_id="#1",
            instance_manager=mocks["instance_manager"],
            session_var_manager=mocks["session_var_manager"],
        )

        assert result["success"] is True
        assert result["workflows"] == []


def _make_db_row(
    name: str = "test-wf",
    workflow_type: str = "workflow",
    description: str = "A test workflow",
    source: str = "installed",
    enabled: bool = True,
    priority: int = 100,
) -> WorkflowDefinitionRow:
    """Create a mock WorkflowDefinitionRow."""
    return WorkflowDefinitionRow(
        id=f"uuid-{name}",
        name=name,
        workflow_type=workflow_type,
        enabled=enabled,
        priority=priority,
        definition_json="{}",
        source=source,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        description=description,
    )


class TestListWorkflowsDBIntegration:
    """Tests for list_workflows with DB + filesystem merge."""

    def test_returns_db_stored_definitions(self) -> None:
        """list_workflows returns DB-stored definitions when DB is available."""
        from gobby.mcp_proxy.tools.workflows._query import list_workflows

        db = MagicMock()
        loader = MagicMock()
        loader.global_dirs = []

        rows = [
            _make_db_row("my-workflow", "workflow", "Workflow from DB"),
            _make_db_row("my-pipeline", "pipeline", "Pipeline from DB"),
        ]

        with patch("gobby.storage.workflow_definitions.LocalWorkflowDefinitionManager") as MockMgr:
            MockMgr.return_value.list_all.return_value = rows
            result = list_workflows(loader, project_path="/fake/path", db=db)

        assert result["success"] is True
        assert result["count"] == 2
        names = [w["name"] for w in result["workflows"]]
        assert "my-workflow" in names
        assert "my-pipeline" in names
        # DB entries include enabled and priority
        wf = next(w for w in result["workflows"] if w["name"] == "my-workflow")
        assert wf["enabled"] is True
        assert wf["priority"] == 100
        assert wf["source"] == "installed"

    def test_merges_db_and_filesystem(self, tmp_path: Path) -> None:
        """list_workflows merges DB + filesystem results, DB takes precedence."""
        from gobby.mcp_proxy.tools.workflows._query import list_workflows

        db = MagicMock()
        loader = MagicMock()
        loader.global_dirs = []

        # DB has one workflow
        db_rows = [_make_db_row("shared-name", "workflow", "DB version")]

        # Filesystem has a different workflow + same name
        wf_dir = tmp_path / ".gobby" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "shared-name.yaml").write_text("name: shared-name\ndescription: FS version\n")
        (wf_dir / "fs-only.yaml").write_text("name: fs-only\ndescription: Filesystem only\n")

        with patch("gobby.storage.workflow_definitions.LocalWorkflowDefinitionManager") as MockMgr:
            MockMgr.return_value.list_all.return_value = db_rows
            result = list_workflows(loader, project_path=str(tmp_path), db=db)

        assert result["success"] is True
        names = [w["name"] for w in result["workflows"]]
        # DB version of shared-name wins, fs-only also included
        assert "shared-name" in names
        assert "fs-only" in names
        assert result["count"] == 2
        # The shared-name entry should be from DB (has source=custom)
        shared = next(w for w in result["workflows"] if w["name"] == "shared-name")
        assert shared["source"] == "installed"
        assert shared["description"] == "DB version"

    def test_falls_back_to_filesystem_when_db_empty(self, tmp_path: Path) -> None:
        """list_workflows falls back to filesystem when DB has no results."""
        from gobby.mcp_proxy.tools.workflows._query import list_workflows

        db = MagicMock()
        loader = MagicMock()
        loader.global_dirs = []

        # DB returns empty
        with patch("gobby.storage.workflow_definitions.LocalWorkflowDefinitionManager") as MockMgr:
            MockMgr.return_value.list_all.return_value = []

            wf_dir = tmp_path / ".gobby" / "workflows"
            wf_dir.mkdir(parents=True)
            (wf_dir / "fs-workflow.yaml").write_text(
                "name: fs-workflow\ndescription: From filesystem\n"
            )

            result = list_workflows(loader, project_path=str(tmp_path), db=db)

        assert result["success"] is True
        assert result["count"] == 1
        assert result["workflows"][0]["name"] == "fs-workflow"
        assert result["workflows"][0]["source"] == "project"

    def test_falls_back_to_filesystem_when_no_db(self, tmp_path: Path) -> None:
        """list_workflows works without DB (backward compatible)."""
        from gobby.mcp_proxy.tools.workflows._query import list_workflows

        loader = MagicMock()
        loader.global_dirs = []

        wf_dir = tmp_path / ".gobby" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "legacy.yaml").write_text("name: legacy\ndescription: Legacy workflow\n")

        # No db parameter — pure filesystem
        result = list_workflows(loader, project_path=str(tmp_path))

        assert result["success"] is True
        assert result["count"] == 1
        assert result["workflows"][0]["name"] == "legacy"

    def test_db_error_falls_back_gracefully(self, tmp_path: Path) -> None:
        """list_workflows handles DB errors gracefully, falling back to filesystem."""
        from gobby.mcp_proxy.tools.workflows._query import list_workflows

        db = MagicMock()
        loader = MagicMock()
        loader.global_dirs = []

        wf_dir = tmp_path / ".gobby" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "fallback.yaml").write_text("name: fallback\ndescription: Fallback\n")

        with patch("gobby.storage.workflow_definitions.LocalWorkflowDefinitionManager") as MockMgr:
            MockMgr.return_value.list_all.side_effect = RuntimeError("DB crashed")
            result = list_workflows(loader, project_path=str(tmp_path), db=db)

        assert result["success"] is True
        assert result["count"] == 1
        assert result["workflows"][0]["name"] == "fallback"
