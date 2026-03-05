"""Tests for gobby-tests MCP tools (run_check, get_run_status, get_run_result)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations

pytestmark = pytest.mark.unit


@pytest.fixture
def temp_db(tmp_path: Path) -> Iterator[LocalDatabase]:
    """Create a temporary database with schema."""
    db_path = tmp_path / "test.db"
    db = LocalDatabase(db_path)
    run_migrations(db)
    yield db
    db.close()


@pytest.fixture
def registry(temp_db: LocalDatabase):
    """Create testing registry."""
    from gobby.mcp_proxy.tools.testing import create_testing_registry

    return create_testing_registry(db=temp_db)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Create a project directory with .gobby/project.json."""
    gobby_dir = tmp_path / "project" / ".gobby"
    gobby_dir.mkdir(parents=True)
    project_json = gobby_dir / "project.json"
    project_json.write_text(
        json.dumps(
            {
                "id": "test-proj-123",
                "name": "test-project",
                "verification": {
                    "unit_tests": "echo '42 passed in 3.2s'",
                    "lint": "echo 'All checks passed'",
                    "type_check": "echo 'Success: no issues found'",
                },
            }
        )
    )
    return tmp_path / "project"


class TestRegistryCreation:
    def test_registry_name(self, registry) -> None:
        assert registry.name == "gobby-tests"

    def test_registry_has_three_tools(self, registry) -> None:
        tools = registry.list_tools()
        tool_names = {t["name"] for t in tools}
        assert tool_names == {"run_check", "get_run_status", "get_run_result"}


class TestRunCheck:
    @pytest.mark.asyncio
    async def test_run_check_success(self, registry, project_dir: Path) -> None:
        """Test run_check resolves from project.json and returns summary."""

        mock_context = {
            "id": "test-proj-123",
            "project_path": str(project_dir),
            "verification": {
                "lint": "echo 'All checks passed'",
            },
        }

        with (
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value=mock_context,
            ),
            patch(
                "gobby.utils.project_context.get_verification_config",
            ) as mock_verif,
        ):
            from gobby.config.features import ProjectVerificationConfig

            mock_verif.return_value = ProjectVerificationConfig(lint="echo 'All checks passed'")

            tool_fn = registry.get_tool("run_check")
            result = await tool_fn(category="lint")

        assert result["success"] is True
        assert result["status"] == "completed"
        assert result["exit_code"] == 0
        assert "run_id" in result

    @pytest.mark.asyncio
    async def test_run_check_missing_category(self, registry) -> None:
        """Test run_check with unknown category lists available ones."""
        mock_context = {
            "id": "test-proj",
            "project_path": "/tmp/test",
            "verification": {"lint": "ruff check"},
        }

        with (
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value=mock_context,
            ),
            patch(
                "gobby.utils.project_context.get_verification_config",
            ) as mock_verif,
        ):
            from gobby.config.features import ProjectVerificationConfig

            mock_verif.return_value = ProjectVerificationConfig(lint="ruff check")

            tool_fn = registry.get_tool("run_check")
            result = await tool_fn(category="nonexistent")

        assert result["success"] is False
        assert "nonexistent" in result["error"]
        assert "available_categories" in result

    @pytest.mark.asyncio
    async def test_run_check_no_project(self, registry) -> None:
        """Test run_check with no project context."""
        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value=None,
        ):
            tool_fn = registry.get_tool("run_check")
            result = await tool_fn(category="lint")

        assert result["success"] is False
        assert "project context" in result["error"].lower()


class TestGetRunStatus:
    @pytest.mark.asyncio
    async def test_get_run_status_found(self, registry, temp_db: LocalDatabase) -> None:
        """Test get_run_status for an existing run."""
        from gobby.storage.test_runs import TestRunStorage

        storage = TestRunStorage(temp_db)
        run = storage.create_run(category="lint", command="ruff check")

        tool_fn = registry.get_tool("get_run_status")
        result = tool_fn(run_id=run.id)

        assert result["success"] is True
        assert result["run_id"] == run.id
        assert result["status"] == "running"

    def test_get_run_status_not_found(self, registry) -> None:
        tool_fn = registry.get_tool("get_run_status")
        result = tool_fn(run_id="tr-nonexistent")

        assert result["success"] is False
        assert "not found" in result["error"].lower()


class TestGetRunResult:
    @pytest.mark.asyncio
    async def test_get_run_result_summary(self, registry, temp_db: LocalDatabase) -> None:
        """Test get_run_result returns stored summary."""
        from gobby.storage.test_runs import TestRunStorage

        storage = TestRunStorage(temp_db)
        run = storage.create_run(category="lint", command="ruff check")
        storage.update_run(run.id, status="completed", exit_code=0, summary="All good")

        tool_fn = registry.get_tool("get_run_result")
        result = tool_fn(run_id=run.id)

        assert result["success"] is True
        assert result["summary"] == "All good"
        assert "output" not in result

    @pytest.mark.asyncio
    async def test_get_run_result_with_output(
        self, registry, temp_db: LocalDatabase, tmp_path: Path
    ) -> None:
        """Test get_run_result with include_output=True."""
        from gobby.storage.test_runs import TestRunStorage

        storage = TestRunStorage(temp_db)
        run = storage.create_run(category="lint", command="ruff check")

        # Create output file
        output_file = tmp_path / f"{run.id}.log"
        output_file.write_text("\n".join(f"line{i}" for i in range(20)))

        storage.update_run(
            run.id,
            status="completed",
            exit_code=0,
            summary="All good",
            output_file=str(output_file),
        )

        tool_fn = registry.get_tool("get_run_result")
        result = tool_fn(run_id=run.id, include_output=True, output_limit=10)

        assert result["success"] is True
        assert "output" in result
        assert len(result["output"]["lines"]) == 10
        assert result["output"]["total_lines"] == 20
        assert result["output"]["has_more"] is True
