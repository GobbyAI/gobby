"""Red tests for applying lifecycle-dispatch expansion metadata."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks import Lifecycle, LocalTaskManager
from gobby.tasks.expansion_service import ExpansionService

pytestmark = pytest.mark.unit


@pytest.fixture
def task_manager(temp_db) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


@pytest.fixture
def run_manager(temp_db) -> LocalExpansionRunManager:
    return LocalExpansionRunManager(temp_db)


@pytest.fixture
def service(
    task_manager: LocalTaskManager,
    run_manager: LocalExpansionRunManager,
) -> ExpansionService:
    return ExpansionService(
        task_manager=task_manager,
        llm_service=MagicMock(),
        run_manager=run_manager,
    )


def _compiled_spec(*, category: str = "code") -> dict:
    return {
        "phases": [
            {
                "id": "phase-1",
                "title": "Phase 1",
                "summary": "Build the leaf",
                "task_ids": ["leaf-1"],
            }
        ],
        "tasks": [
            {
                "id": "leaf-1",
                "phase_id": "phase-1",
                "title": "Implement leaf",
                "description": "Implement the selected behavior.",
                "category": category,
                "validation": "Focused validation passes.",
                "assigned_agent": "frontend-developer",
                "additional_skills": ["playwright-cli"],
            }
        ],
        "dependencies": [],
    }


def test_prompt_context_includes_skipped_stages_from_epic_labels(
    service: ExpansionService,
    run_manager: LocalExpansionRunManager,
    sample_project,
) -> None:
    epic = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Skipped-stage epic",
        labels=["profile:quick", "stage-:qa", "stage-:pr"],
    )
    run = run_manager.create(
        parent_task_id=epic.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="task",
    )

    context = service._build_prompt_context(run, epic)

    assert context["skipped_stages"] == ["pr", "qa"]
    assert "STAGE_BY_PROFILE" not in context
    assert "profile:quick" not in context["context_str"]


@pytest.mark.asyncio
async def test_compile_run_skips_tree_when_dev_is_only_enabled_stage(
    service: ExpansionService,
    run_manager: LocalExpansionRunManager,
    sample_project,
) -> None:
    epic = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Dev-only epic",
        task_type="epic",
        labels=[
            "stage-:plan_review",
            "stage-:test_arch",
            "stage-:expanding",
            "stage-:qa",
            "stage-:holistic_review",
            "stage-:pr",
        ],
    )
    run = run_manager.create(
        parent_task_id=epic.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="task",
    )

    raw_spec = AsyncMock()
    with patch.object(service, "_generate_raw_spec", raw_spec):
        refreshed = await service.compile_and_apply_run(run.id, session_id=None)

    assert raw_spec.await_count == 0
    assert refreshed.status == "completed"
    assert refreshed.created_task_ids == []
    assert service.task_manager.list_tasks(parent_task_id=epic.id) == []
    assert service.task_manager.get_task(epic.id).lifecycle is Lifecycle.in_development


def test_apply_run_persists_agent_selection_fields_to_created_leaf(
    service: ExpansionService,
    run_manager: LocalExpansionRunManager,
    sample_project,
) -> None:
    epic = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Agent selection epic",
    )
    run = run_manager.create(
        parent_task_id=epic.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="task",
    )
    run_manager.save_compiled_spec(run.id, _compiled_spec())

    applied = service.apply_run(run.id, session_id=None)

    child_id = applied.task_id_map["leaf-1"]
    child = service.task_manager.get_task(child_id)
    assert child.assigned_agent == "frontend-developer"
    assert child.additional_skills == ["playwright-cli"]


def test_apply_run_rejects_planning_leaf_without_creating_children(
    service: ExpansionService,
    run_manager: LocalExpansionRunManager,
    sample_project,
) -> None:
    epic = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Planning leaf epic",
    )
    run = run_manager.create(
        parent_task_id=epic.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="task",
    )
    run_manager.save_compiled_spec(run.id, _compiled_spec(category="planning"))

    with pytest.raises(ValueError, match="category:planning"):
        service.apply_run(run.id, session_id=None)

    children = service.task_manager.list_tasks(parent_task_id=epic.id)
    assert children == []


def test_no_expansion_service_start_run_wrapper() -> None:
    assert not hasattr(ExpansionService, "start_run")
