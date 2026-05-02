"""Tests for run-oriented task expansion MCP tools."""

import asyncio
import textwrap
from unittest.mock import AsyncMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._expansion import (
    _background_run_tasks,
    create_expansion_registry,
)
from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskSyncManager
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


@pytest.fixture
def task_manager(temp_db):
    return LocalTaskManager(temp_db)


@pytest.fixture
def sync_manager(task_manager, temp_dir):
    return TaskSyncManager(task_manager, temp_dir / "tasks.jsonl")


@pytest.fixture
def test_project(project_manager):
    project = project_manager.create(
        name="test-project",
        repo_path="/tmp/test-project",
    )
    return project.id


@pytest.fixture
def test_session(session_manager, test_project):
    session = session_manager.register(
        project_id=test_project,
        source="test",
        external_id="test-external",
        machine_id="test-machine",
    )
    return session.id


@pytest.fixture
def expansion_registry(task_manager, sync_manager):
    ctx = RegistryContext(
        task_manager=task_manager,
        sync_manager=sync_manager,
        task_validator=None,
        config=None,
    )
    return create_expansion_registry(ctx)


@pytest.fixture(autouse=True)
async def clear_background_runs():
    _background_run_tasks.clear()
    yield
    pending = list(_background_run_tasks.values())
    for task in pending:
        if not task.done():
            task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _background_run_tasks.clear()


@pytest.fixture
def parent_task(task_manager, test_project):
    task = task_manager.create_task(
        project_id=test_project,
        title="Parent task for expansion",
        task_type="feature",
    )
    return task.id


def _compiled_spec() -> dict:
    return {
        "phases": [
            {
                "id": "phase-1",
                "title": "Phase 1: Foundation",
                "summary": "Build the foundation.",
                "test_intent": {
                    "behaviors": ["Writes the new files"],
                    "suggested_test_files": ["tests/test_foundation.py"],
                },
                "task_ids": ["task-1"],
            }
        ],
        "tasks": [
            {
                "id": "task-1",
                "phase_id": "phase-1",
                "title": "Implement the foundation",
                "description": "Create the initial implementation.",
                "category": "code",
                "priority": 2,
                "task_type": "task",
                "validation": "Implementation is present.",
                "affected_files": ["src/foundation.py"],
            }
        ],
        "dependencies": [],
        "execution_groups": [],
    }


def _write_plan_missing_target(tmp_path) -> str:
    path = tmp_path / "plan.md"
    path.write_text(
        textwrap.dedent(
            """
            > **Plan ID:** missing-target

            # Missing Target

            ## P1: Work
            `kind: framing`

            ### 1.1 Work [category: code]
            `kind: deliverable`

            Update implementation.

            **Acceptance:**
            - 1.1.1 - Implementation exists. file: `src/app.py`.
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return str(path)


class TestExpansionRuns:
    @pytest.mark.asyncio
    async def test_validate_plan_file_returns_semantic_lint_errors(
        self,
        expansion_registry,
        tmp_path,
    ) -> None:
        result = await expansion_registry.call(
            "validate_plan_file",
            {"plan_file": _write_plan_missing_target(tmp_path)},
        )

        assert result["valid"] is False
        assert any("target-coverage" in error for error in result["errors"])
        assert result["semantic_lint"]["valid"] is False

    @pytest.mark.asyncio
    async def test_start_expansion_run_creates_run(
        self,
        expansion_registry,
        task_manager,
        parent_task,
        test_session,
    ) -> None:
        run_manager = LocalExpansionRunManager(task_manager.db)

        with patch(
            "gobby.mcp_proxy.tools.tasks._expansion._execute_run_background",
            new=AsyncMock(return_value=None),
        ):
            with session_context_for_test(test_session):
                result = await expansion_registry.call(
                    "start_expansion_run",
                    {"task_id": parent_task, "auto_apply": False},
                )
            await asyncio.sleep(0)

        assert result["success"] is True
        assert result["status"] == "running"
        run = run_manager.get(result["run_id"])
        assert run is not None
        assert run.parent_task_id == parent_task
        assert run.input_source == "task"
        assert run.options == {"auto_apply": False}

    @pytest.mark.asyncio
    async def test_get_latest_expansion_run_returns_most_recent(
        self,
        expansion_registry,
        task_manager,
        parent_task,
    ) -> None:
        parent = task_manager.get_task(parent_task)
        assert parent is not None
        run_manager = LocalExpansionRunManager(task_manager.db)
        first = run_manager.create(
            parent_task_id=parent.id,
            project_id=parent.project_id,
            triggering_session_id=None,
            input_source="task",
        )
        latest = run_manager.create(
            parent_task_id=parent.id,
            project_id=parent.project_id,
            triggering_session_id=None,
            input_source="plan",
            plan_file="docs/plans/example.md",
        )

        result = await expansion_registry.call(
            "get_latest_expansion_run",
            {"task_id": parent_task},
        )

        assert result["success"] is True
        assert result["run"]["id"] == latest.id
        assert result["run"]["id"] != first.id
        assert result["run"]["plan_file"] == "docs/plans/example.md"

    @pytest.mark.asyncio
    async def test_validate_expansion_run_checks_compiled_and_applied(
        self,
        expansion_registry,
        task_manager,
        parent_task,
    ) -> None:
        parent = task_manager.get_task(parent_task)
        assert parent is not None
        run_manager = LocalExpansionRunManager(task_manager.db)
        run = run_manager.create(
            parent_task_id=parent.id,
            project_id=parent.project_id,
            triggering_session_id=None,
            input_source="task",
        )
        run_manager.save_compiled_spec(run.id, _compiled_spec())
        child = task_manager.create_task(
            project_id=parent.project_id,
            title="Implement the foundation",
            parent_task_id=parent.id,
            category="code",
        )
        run_manager.save_apply_result(
            run.id,
            task_id_map={"task-1": child.id},
            created_task_ids=[child.id],
            completed=True,
        )

        result = await expansion_registry.call(
            "validate_expansion_run",
            {"run_id": run.id},
        )

        assert result["success"] is True
        assert result["compiled"]["valid"] is True
        assert result["applied"]["valid"] is True

    @pytest.mark.asyncio
    async def test_cancel_expansion_run_marks_run_cancelled(
        self,
        expansion_registry,
        task_manager,
        parent_task,
    ) -> None:
        parent = task_manager.get_task(parent_task)
        assert parent is not None
        run_manager = LocalExpansionRunManager(task_manager.db)
        run = run_manager.create(
            parent_task_id=parent.id,
            project_id=parent.project_id,
            triggering_session_id=None,
            input_source="task",
        )

        result = await expansion_registry.call(
            "cancel_expansion_run",
            {"run_id": run.id},
        )

        assert result["success"] is True
        assert result["run"]["status"] == "cancelled"
        refreshed = run_manager.get(run.id)
        assert refreshed is not None
        assert refreshed.status == "cancelled"

    @pytest.mark.asyncio
    async def test_resume_expansion_run_restarts_failed_run(
        self,
        expansion_registry,
        task_manager,
        parent_task,
        test_session,
    ) -> None:
        parent = task_manager.get_task(parent_task)
        assert parent is not None
        run_manager = LocalExpansionRunManager(task_manager.db)
        run = run_manager.create(
            parent_task_id=parent.id,
            project_id=parent.project_id,
            triggering_session_id=None,
            input_source="task",
            options={"auto_apply": False},
        )
        run_manager.fail(run.id, "failed before resume")

        with patch(
            "gobby.mcp_proxy.tools.tasks._expansion._execute_run_background",
            new=AsyncMock(return_value=None),
        ):
            with session_context_for_test(test_session):
                result = await expansion_registry.call(
                    "resume_expansion_run",
                    {"run_id": run.id},
                )
            await asyncio.sleep(0)

        assert result["success"] is True
        assert result["status"] == "running"
