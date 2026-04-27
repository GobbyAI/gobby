"""Red tests for lifecycle-dispatch expansion contract helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.tasks import expansion_service as expansion_module
from gobby.tasks.expansion_service import ExpansionService
from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit


@pytest.fixture
def service(temp_db) -> ExpansionService:
    return ExpansionService(task_manager=LocalTaskManager(temp_db), llm_service=MagicMock())


def _parent(service: ExpansionService, sample_project, *, labels: list[str] | None = None):
    return service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Lifecycle epic",
        task_type="epic",
        labels=labels or [],
    )


def _store_agent(service: ExpansionService, name: str, description: str) -> None:
    body = AgentDefinitionBody(name=name, description=description, surfaces=["spawn"])
    LocalWorkflowDefinitionManager(service.db).create(
        name=name,
        definition_json=body.model_dump_json(),
        workflow_type="agent",
        description=description,
        enabled=True,
    )


def test_skipped_stages_helper_parses_resolved_stage_labels_not_profiles(
    service: ExpansionService,
    sample_project,
) -> None:
    assert not hasattr(expansion_module, "STAGE_BY_PROFILE")
    helper = expansion_module._skipped_stages
    epic = _parent(
        service,
        sample_project,
        labels=["profile:quick", "stage-:qa", "stage-:holistic_review"],
    )

    assert helper(epic) == {"qa", "holistic_review"}


def test_skipped_stages_helper_ignores_dev_only_profile_without_resolved_labels(
    service: ExpansionService,
    sample_project,
) -> None:
    helper = expansion_module._skipped_stages
    profile_only = _parent(service, sample_project, labels=["profile:dev-only"])
    resolved = _parent(
        service,
        sample_project,
        labels=["profile:dev-only", "stage-:qa", "stage-:pr"],
    )

    assert helper(profile_only) == set()
    assert helper(resolved) == {"qa", "pr"}


def test_automated_leaf_categories_are_explicit() -> None:
    assert expansion_module.AUTOMATED_LEAF_CATEGORIES == {
        "code",
        "config",
        "docs",
        "manual",
        "refactor",
        "research",
        "test",
    }


def test_validate_compiled_spec_rejects_planning_leaves(
    service: ExpansionService,
    sample_project,
) -> None:
    epic = _parent(service, sample_project)
    spec = service.normalize_compiled_spec(
        {
            "phases": [{"id": "phase-1", "title": "Phase", "task_ids": ["plan-leaf"]}],
            "tasks": [
                {
                    "id": "plan-leaf",
                    "phase_id": "phase-1",
                    "title": "Plan more",
                    "category": "planning",
                }
            ],
            "dependencies": [],
        },
        task=epic,
        plan_file=None,
    )

    validation = service.validate_compiled_spec(spec)

    assert validation["valid"] is False
    assert any("category:planning" in error for error in validation["errors"])


def test_normalize_preserves_registry_agent_selection_and_additional_skills(
    service: ExpansionService,
    sample_project,
) -> None:
    epic = _parent(service, sample_project)
    _store_agent(service, "frontend-developer", "Frontend development")
    spec = service.normalize_compiled_spec(
        {
            "phases": [{"id": "phase-1", "title": "Phase", "task_ids": ["ui"]}],
            "tasks": [
                {
                    "id": "ui",
                    "phase_id": "phase-1",
                    "title": "Build UI",
                    "category": "code",
                    "assigned_agent": "frontend-developer",
                    "additional_skills": ["playwright-cli"],
                }
            ],
            "dependencies": [],
        },
        task=epic,
        plan_file=None,
    )

    leaf = spec["tasks"][0]
    assert leaf["assigned_agent"] == "frontend-developer"
    assert leaf["additional_skills"] == ["playwright-cli"]


def test_normalize_defaults_ambiguous_automated_leaf_to_backend_with_audit_marker(
    service: ExpansionService,
    sample_project,
) -> None:
    epic = _parent(service, sample_project)
    spec = service.normalize_compiled_spec(
        {
            "phases": [{"id": "phase-1", "title": "Phase", "task_ids": ["docs"]}],
            "tasks": [
                {
                    "id": "docs",
                    "phase_id": "phase-1",
                    "title": "Document behavior",
                    "category": "docs",
                    "description": "Update the operator guide.",
                }
            ],
            "dependencies": [],
        },
        task=epic,
        plan_file=None,
    )

    leaf = spec["tasks"][0]
    assert leaf["assigned_agent"] == "backend-developer"
    assert leaf["additional_skills"] == []
    assert "## Agent Selection" in leaf["description"]
    assert "Defaulted to `backend-developer`" in leaf["description"]


def test_normalize_selects_best_fit_agent_from_registry(
    service: ExpansionService,
    sample_project,
) -> None:
    _store_agent(service, "backend-developer", "Backend storage and MCP implementation")
    _store_agent(service, "frontend-developer", "Frontend UI, React, CSS, and Playwright")
    epic = _parent(service, sample_project)

    spec = service.normalize_compiled_spec(
        {
            "phases": [{"id": "phase-1", "title": "Phase", "task_ids": ["ui"]}],
            "tasks": [
                {
                    "id": "ui",
                    "phase_id": "phase-1",
                    "title": "Build React UI",
                    "category": "code",
                    "description": "Implement browser components and CSS states.",
                    "affected_files": ["src/gobby/ui/App.tsx"],
                }
            ],
            "dependencies": [],
        },
        task=epic,
        plan_file=None,
    )

    leaf = spec["tasks"][0]
    assert leaf["assigned_agent"] == "frontend-developer"
    assert leaf["additional_skills"] == []
    assert "## Agent Selection" not in leaf["description"]
