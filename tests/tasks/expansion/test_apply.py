"""Phase 2 tests for expansion apply behavior."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.expansion_service import ExpansionService

pytestmark = pytest.mark.unit


def test_apply_copies_parent_target_branch_onto_generated_leaves(temp_db, sample_project) -> None:
    from gobby.tasks.expansion import _apply

    task_manager = LocalTaskManager(temp_db)
    artifact_manager = task_manager.artifacts
    run_manager = LocalExpansionRunManager(temp_db)
    service = ExpansionService(
        task_manager=task_manager,
        llm_service=MagicMock(),
        run_manager=run_manager,
    )
    parent = task_manager.create_task(
        project_id=sample_project["id"],
        title="Expansion parent",
        task_type="epic",
    )
    artifact_manager.set_artifact(parent.id, "target_branch", "release/0.4")
    run = run_manager.create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="task",
    )
    run_manager.save_compiled_spec(
        run.id,
        {
            "phases": [{"id": "phase-1", "title": "Phase 1", "task_ids": ["leaf"]}],
            "tasks": [
                {
                    "id": "leaf",
                    "phase_id": "phase-1",
                    "title": "Implement leaf",
                    "category": "code",
                    "assigned_agent": "backend-developer",
                }
            ],
            "dependencies": [],
        },
    )

    applied = _apply.apply_run(service, run.id, session_id=None)

    child_id = applied.task_id_map["leaf"]
    assert artifact_manager.get_artifacts(child_id).target_branch == "release/0.4"


def _service(temp_db) -> ExpansionService:
    return ExpansionService(
        task_manager=LocalTaskManager(temp_db),
        llm_service=MagicMock(),
        run_manager=LocalExpansionRunManager(temp_db),
    )


def _save_run(
    service: ExpansionService,
    parent,
    sample_project,
    spec: dict,
):
    run = service.run_manager.create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="plan",
    )
    service.run_manager.save_compiled_spec(run.id, spec)
    return run


def test_contract_apply_stage_manifests_and_created_ids(temp_db, sample_project) -> None:
    service = _service(temp_db)
    parent = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Expansion parent",
        task_type="epic",
    )
    spec = {
        "version": 1,
        "parent_task_id": parent.id,
        "contract_plan": True,
        "phases": [
            {"id": "phase-p1", "title": "Phase 1", "summary": "P1", "task_ids": ["leaf-1"]},
            {"id": "phase-p2", "title": "Phase 2", "summary": "P2", "task_ids": ["leaf-2"]},
        ],
        "tasks": [
            {
                "id": "leaf-1",
                "phase_id": "phase-p1",
                "title": "Implement leaf 1",
                "category": "code",
                "task_type": "feature",
                "validation": "leaf 1 exists",
                "labels": ["covers:12761:1.1:1.1.1"],
                "source_section_id": "1.1",
            },
            {
                "id": "leaf-2",
                "phase_id": "phase-p2",
                "title": "Implement leaf 2",
                "category": "docs",
                "task_type": "task",
                "validation": "leaf 2 exists",
                "labels": ["covers:12761:2.1:2.1.1"],
                "source_section_id": "2.1",
            },
        ],
        "dependencies": [],
    }
    run = _save_run(service, parent, sample_project, spec)

    applied = service.apply_run(run.id, session_id=None)

    phase_parent_map = applied.checkpoints["phase_parent_map"]
    phase_epic_id = phase_parent_map["phase-p1"]
    leaf_id = applied.task_id_map["leaf-1"]
    assert phase_epic_id in applied.created_task_ids
    assert leaf_id in applied.created_task_ids
    assert [
        row.stage_name for row in service.task_manager.stage_states.list_for_task(phase_epic_id)
    ] == ["holistic_qa", "pr", "merge"]
    assert [row.stage_name for row in service.task_manager.stage_states.list_for_task(leaf_id)] == [
        "development",
        "pr",
        "merge",
    ]
    leaf = service.task_manager.get_task(leaf_id)
    assert f"expansion-run:{run.id}" in (leaf.labels or [])
    assert service.task_manager.artifacts.get_artifacts(parent.id).expansion_run_id == run.id


def test_apply_parent_with_no_stages_is_noop_for_expansion_completion(
    temp_db,
    sample_project,
) -> None:
    service = _service(temp_db)
    parent = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="No stages parent",
        task_type="epic",
    )
    temp_db.execute("DELETE FROM task_stage_states WHERE task_id = ?", (parent.id,))
    spec = {
        "phases": [{"id": "phase-1", "title": "Phase 1", "task_ids": ["leaf"]}],
        "tasks": [{"id": "leaf", "phase_id": "phase-1", "title": "Leaf", "category": "code"}],
        "dependencies": [],
    }
    run = _save_run(service, parent, sample_project, spec)

    applied = service.apply_run(run.id, session_id=None)

    assert applied.status == "completed"
    assert service.task_manager.stage_states.list_for_task(parent.id) == []


def test_apply_completes_current_expansion_stage(temp_db, sample_project) -> None:
    service = _service(temp_db)
    parent = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Expansion stage parent",
        task_type="epic",
        stages_override=["expansion", "development"],
    )
    service.task_manager.stage_states.start_stage(parent.id, "expansion", by_session_id=None)
    spec = {
        "phases": [{"id": "phase-1", "title": "Phase 1", "task_ids": ["leaf"]}],
        "tasks": [{"id": "leaf", "phase_id": "phase-1", "title": "Leaf", "category": "code"}],
        "dependencies": [],
    }
    run = _save_run(service, parent, sample_project, spec)

    service.apply_run(run.id, session_id=None)

    row = service.task_manager.stage_states.get(parent.id, "expansion")
    assert row is not None
    assert row.state == "done"


def test_reset_deletes_only_expansion_output(temp_db, sample_project) -> None:
    service = _service(temp_db)
    parent = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Reset parent",
        task_type="epic",
        stages_override=["expansion", "development"],
    )
    unrelated = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Historical planning child",
        parent_task_id=parent.id,
        task_type="task",
        category="planning",
    )
    spec = {
        "phases": [{"id": "phase-p1", "title": "Phase 1", "summary": "P1", "task_ids": ["leaf"]}],
        "tasks": [{"id": "leaf", "phase_id": "phase-p1", "title": "Leaf", "category": "code"}],
        "dependencies": [],
    }
    run = _save_run(service, parent, sample_project, spec)
    applied = service.apply_run(run.id, session_id=None)

    result = service.reset_expansion_output(parent.id, run_id=run.id)

    assert set(result.deleted_task_ids) == set(applied.created_task_ids)
    assert service.task_manager.get_task(unrelated.id).id == unrelated.id
    assert service.task_manager.artifacts.get_artifacts(parent.id).expansion_run_id is None
    assert service.task_manager.stage_states.get(parent.id, "expansion").state == "ready"


def test_reset_discovers_historical_phase_ancestor(temp_db, sample_project) -> None:
    service = _service(temp_db)
    parent = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Historical reset parent",
        task_type="epic",
    )
    phase = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Phase",
        parent_task_id=parent.id,
        task_type="epic",
        category="planning",
    )
    leaf = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Leaf",
        parent_task_id=phase.id,
        task_type="task",
        category="code",
    )
    run = service.run_manager.create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="task",
    )
    service.run_manager.save_apply_result(
        run.id,
        task_id_map={"leaf": leaf.id},
        created_task_ids=[leaf.id],
    )

    result = service.reset_expansion_output(parent.id, run_id=run.id)

    assert set(result.deleted_task_ids) == {leaf.id, phase.id}


def test_reset_refuses_progressed_generated_task(temp_db, sample_project) -> None:
    service = _service(temp_db)
    parent = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Refuse reset parent",
        task_type="epic",
    )
    leaf = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Leaf",
        parent_task_id=parent.id,
        task_type="task",
        category="code",
    )
    service.task_manager.stage_states.start_stage(leaf.id, "development", by_session_id=None)
    run = service.run_manager.create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="task",
    )
    service.run_manager.save_apply_result(
        run.id,
        task_id_map={"leaf": leaf.id},
        created_task_ids=[leaf.id],
    )

    with pytest.raises(ValueError, match="progressed stage state"):
        service.reset_expansion_output(parent.id, run_id=run.id)


def test_apply_refuses_duplicate_output_without_reset(temp_db, sample_project) -> None:
    service = _service(temp_db)
    parent = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Duplicate parent",
        task_type="epic",
    )
    spec = {
        "phases": [{"id": "phase-1", "title": "Phase 1", "task_ids": ["leaf"]}],
        "tasks": [{"id": "leaf", "phase_id": "phase-1", "title": "Leaf", "category": "code"}],
        "dependencies": [],
    }
    first = _save_run(service, parent, sample_project, spec)
    service.apply_run(first.id, session_id=None)
    second_spec = {
        **spec,
        "tasks": [{**spec["tasks"][0], "id": "leaf-2"}],
        "phases": [{"id": "phase-1", "title": "Phase 1", "task_ids": ["leaf-2"]}],
    }
    second = _save_run(service, parent, sample_project, second_spec)

    with pytest.raises(ValueError, match="Reset expansion output"):
        service.apply_run(second.id, session_id=None)
