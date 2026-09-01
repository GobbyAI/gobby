"""Tests for expansion QA storage on expansion runs."""

from pathlib import Path
from typing import Any

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._expansion import create_expansion_registry
from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.unit


@pytest.fixture
def task_manager(temp_db: HubDatabase) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


@pytest.fixture
def test_project(temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    return install_isolated_checkout_project(
        temp_db, tmp_path / "test-project", name="test-project", monkeypatch=monkeypatch
    ).project.id


@pytest.fixture
def expansion_registry(task_manager: LocalTaskManager) -> InternalToolRegistry:
    ctx = RegistryContext(
        task_manager=task_manager,
        task_validator_resolver=None,
    )
    return create_expansion_registry(ctx)


@pytest.fixture
def expansion_run(task_manager: LocalTaskManager, test_project: Any) -> Any:
    parent = task_manager.create_task(
        project_id=test_project,
        title="Parent task for expansion QA",
        task_type="feature",
        validation_criteria="Test task completion is observable.",
    )
    run_manager = LocalExpansionRunManager(task_manager.db)
    return run_manager.create(
        parent_task_id=parent.id,
        project_id=parent.project_id,
        triggering_session_id=None,
        input_source="task",
    )


class TestExpansionQaResult:
    @pytest.mark.asyncio
    async def test_save_expansion_qa_result_persists_on_run(
        self,
        expansion_registry: InternalToolRegistry,
        task_manager: LocalTaskManager,
        expansion_run: Any,
    ) -> None:
        qa_result = {
            "passed": True,
            "fixes": [{"type": "added_dependency", "detail": "Added missing edge"}],
            "escalations": [],
        }

        result = await expansion_registry.call(
            "save_expansion_qa_result",
            {"run_id": expansion_run.id, "qa_result": qa_result},
        )

        assert result["success"] is True
        assert result["run"]["qa_result"] == qa_result

        persisted = LocalExpansionRunManager(task_manager.db).get(expansion_run.id)
        assert persisted is not None
        assert persisted.qa_result == qa_result

    @pytest.mark.asyncio
    async def test_check_expansion_qa_result_returns_skipped_when_missing(
        self,
        expansion_registry: InternalToolRegistry,
        expansion_run: Any,
    ) -> None:
        result = await expansion_registry.call(
            "check_expansion_qa_result",
            {"run_id": expansion_run.id},
        )

        assert result["success"] is True
        assert result["qa_status"] == "skipped"
        assert result["reason"] == "No QA result stored on expansion run"

    @pytest.mark.asyncio
    async def test_check_expansion_qa_result_returns_failed_when_passed_false(
        self,
        expansion_registry: InternalToolRegistry,
        task_manager: LocalTaskManager,
        expansion_run: Any,
    ) -> None:
        run_manager = LocalExpansionRunManager(task_manager.db)
        run_manager.save_qa_result(
            expansion_run.id,
            {
                "passed": False,
                "fixes": [],
                "escalations": [{"type": "missing_plan_section", "detail": "Phase dropped"}],
            },
        )

        result = await expansion_registry.call(
            "check_expansion_qa_result",
            {"run_id": expansion_run.id},
        )

        assert result["success"] is True
        assert result["qa_status"] == "failed"
        assert result["qa_result"]["passed"] is False
