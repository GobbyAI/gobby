"""Phase 2 tests for expansion apply behavior."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.hub._ambient import ambient_transaction
from gobby.storage.task_dependencies import DependencyCycleError
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.expansion_service import ExpansionService

pytestmark = pytest.mark.unit


def test_add_dependency_ignores_dependency_cycles() -> None:
    from gobby.tasks.expansion import _apply

    class CyclicDependencyManager:
        called = False

        def add_dependency(self, **_kwargs: str) -> None:
            self.called = True
            raise DependencyCycleError("cycle")

    dep_manager = CyclicDependencyManager()
    service = MagicMock(dep_manager=dep_manager)

    _apply._add_dependency(service, "task", "blocker")

    assert dep_manager.called is True


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
        validation_criteria="Test task completion is observable.",
    )
    artifact_manager.set_artifact(parent.id, "target_branch", "release/0.4")
    run = run_manager.create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="task",
    )
    run_manager.start(run.id)
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
                    "validation": "Test task completion is observable.",
                }
            ],
            "dependencies": [],
        },
    )

    applied = _apply.apply_run(service, run.id, session_id=None)

    child_id = applied.task_id_map["leaf"]
    child = task_manager.get_task(child_id)
    assert child is not None
    assert child.title == "Implement leaf"
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
    service.run_manager.start(run.id)
    service.run_manager.save_compiled_spec(run.id, spec)
    return run


def test_contract_apply_stage_manifests_and_created_ids(temp_db, sample_project) -> None:
    service = _service(temp_db)
    parent = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Expansion parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    service.task_manager.initialize_task_manifest(
        parent.id,
        stage_names=["development", "epic_qa", "pr", "merge"],
        stage_caps=[
            {"stage_name": "development", "max_review_rounds": 3},
            {"stage_name": "epic_qa", "max_review_rounds": 4},
            {"stage_name": "pr", "max_work_attempts": 2},
        ],
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
                "validation_criteria": "Test task completion is observable.",
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
                "validation_criteria": "Test task completion is observable.",
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
    phase_rows = service.task_manager.stage_states.list_for_task(phase_epic_id)
    leaf_rows = service.task_manager.stage_states.list_for_task(leaf_id)
    assert [row.stage_name for row in phase_rows] == [
        "development",
        "epic_qa",
        "pr",
        "merge",
    ]
    assert [row.stage_name for row in leaf_rows] == ["development", "pr", "merge"]
    assert phase_rows[0].max_review_rounds == 3
    assert phase_rows[1].max_review_rounds == 4
    assert leaf_rows[0].max_review_rounds == 3
    assert leaf_rows[1].max_work_attempts == 2
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
        validation_criteria="Test task completion is observable.",
    )
    spec = {
        "phases": [{"id": "phase-1", "title": "Phase 1", "task_ids": ["leaf"]}],
        "tasks": [
            {
                "id": "leaf",
                "phase_id": "phase-1",
                "title": "Leaf",
                "category": "code",
                "validation": "Test task completion is observable.",
            }
        ],
        "dependencies": [],
    }
    run = _save_run(service, parent, sample_project, spec)

    applied = service.apply_run(run.id, session_id=None)

    assert applied.status == "completed"
    assert service.task_manager.stage_states.list_for_task(parent.id) == []
    assert service.task_manager.stage_states.list_for_task(applied.task_id_map["leaf"]) == []


def test_apply_completes_current_expansion_stage(temp_db, sample_project) -> None:
    service = _service(temp_db)
    parent = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Expansion stage parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    service.task_manager.initialize_task_manifest(
        parent.id,
        stage_names=["expansion", "development"],
    )
    service.task_manager.stage_states.start_stage(parent.id, "expansion", by_session_id=None)
    spec = {
        "phases": [{"id": "phase-1", "title": "Phase 1", "task_ids": ["leaf"]}],
        "tasks": [
            {
                "id": "leaf",
                "phase_id": "phase-1",
                "title": "Leaf",
                "category": "code",
                "validation": "Test task completion is observable.",
            }
        ],
        "dependencies": [],
    }
    run = _save_run(service, parent, sample_project, spec)

    service.apply_run(run.id, session_id=None)

    row = service.task_manager.stage_states.get(parent.id, "expansion")
    assert row is not None
    assert row.state == "done"


def test_apply_can_suppress_parent_expansion_stage_transition(temp_db, sample_project) -> None:
    service = _service(temp_db)
    parent = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Pipeline-owned expansion parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    service.task_manager.initialize_task_manifest(
        parent.id,
        stage_names=["expansion", "development"],
    )
    service.task_manager.stage_states.start_stage(parent.id, "expansion", by_session_id=None)
    spec = {
        "phases": [{"id": "phase-1", "title": "Phase 1", "task_ids": ["leaf"]}],
        "tasks": [
            {
                "id": "leaf",
                "phase_id": "phase-1",
                "title": "Leaf",
                "category": "code",
                "validation": "Test task completion is observable.",
            }
        ],
        "dependencies": [],
    }
    run = _save_run(service, parent, sample_project, spec)

    applied = service.apply_run(
        run.id,
        session_id=None,
        suppress_parent_stage_transition=True,
    )

    row = service.task_manager.stage_states.get(parent.id, "expansion")
    assert row is not None
    assert row.state == "in_progress"
    assert applied.task_id_map["leaf"] in applied.created_task_ids


def test_reset_deletes_only_expansion_output(temp_db, sample_project) -> None:
    service = _service(temp_db)
    parent = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Reset parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    service.task_manager.initialize_task_manifest(
        parent.id,
        stage_names=["expansion", "development"],
    )
    unrelated = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Historical planning child",
        parent_task_id=parent.id,
        task_type="task",
        category="planning",
        validation_criteria="Test task completion is observable.",
    )
    spec = {
        "phases": [{"id": "phase-p1", "title": "Phase 1", "summary": "P1", "task_ids": ["leaf"]}],
        "tasks": [
            {
                "id": "leaf",
                "phase_id": "phase-p1",
                "title": "Leaf",
                "category": "code",
                "validation": "Test task completion is observable.",
            }
        ],
        "dependencies": [],
    }
    run = _save_run(service, parent, sample_project, spec)
    applied = service.apply_run(run.id, session_id=None)

    result = service.reset_expansion_output(parent.id, run_id=run.id)

    assert set(result.deleted_task_ids) == set(applied.created_task_ids)
    assert service.task_manager.get_task(unrelated.id).id == unrelated.id
    assert service.task_manager.artifacts.get_artifacts(parent.id).expansion_run_id is None
    assert service.task_manager.stage_states.get(parent.id, "expansion").state == "ready"


def test_reset_rolls_back_all_deletions_when_one_fails(temp_db, sample_project) -> None:
    service = _service(temp_db)
    parent = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Atomic reset parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    spec = {
        "phases": [
            {
                "id": "phase-p1",
                "title": "Phase 1",
                "summary": "P1",
                "task_ids": ["leaf-1", "leaf-2"],
            }
        ],
        "tasks": [
            {
                "id": "leaf-1",
                "phase_id": "phase-p1",
                "title": "Leaf 1",
                "category": "code",
                "validation": "Test task completion is observable.",
            },
            {
                "id": "leaf-2",
                "phase_id": "phase-p1",
                "title": "Leaf 2",
                "category": "code",
                "validation": "Test task completion is observable.",
            },
        ],
        "dependencies": [],
    }
    run = _save_run(service, parent, sample_project, spec)
    applied = service.apply_run(run.id, session_id=None)
    original_delete = service.task_manager.delete_task
    calls = 0

    def fail_second_delete(task_id: str, *, unlink: bool) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("delete failed")
        return original_delete(task_id, unlink=unlink)

    service.task_manager.delete_task = fail_second_delete

    with pytest.raises(RuntimeError, match="delete failed"):
        service.reset_expansion_output(parent.id, run_id=run.id)

    for task_id in applied.created_task_ids:
        assert service.task_manager.get_task(task_id).id == task_id
    assert ambient_transaction(temp_db) is None


def test_reset_discovers_historical_phase_ancestor(temp_db, sample_project) -> None:
    service = _service(temp_db)
    parent = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Historical reset parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    phase = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Phase",
        parent_task_id=parent.id,
        task_type="epic",
        category="planning",
        validation_criteria="Test task completion is observable.",
    )
    leaf = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Leaf",
        parent_task_id=phase.id,
        task_type="task",
        category="code",
        validation_criteria="Test task completion is observable.",
    )
    run = service.run_manager.create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="task",
    )
    service.run_manager.db.execute(
        "UPDATE expansion_runs SET status = 'applying' WHERE id = %s",
        (run.id,),
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
        validation_criteria="Test task completion is observable.",
    )
    leaf = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Leaf",
        parent_task_id=parent.id,
        task_type="task",
        category="code",
        validation_criteria="Test task completion is observable.",
    )
    service.task_manager.initialize_task_manifest(leaf.id, stage_names=["development"])
    service.task_manager.stage_states.start_stage(leaf.id, "development", by_session_id=None)
    run = service.run_manager.create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="task",
    )
    service.run_manager.db.execute(
        "UPDATE expansion_runs SET status = 'applying' WHERE id = %s",
        (run.id,),
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
        validation_criteria="Test task completion is observable.",
    )
    spec = {
        "phases": [{"id": "phase-1", "title": "Phase 1", "task_ids": ["leaf"]}],
        "tasks": [
            {
                "id": "leaf",
                "phase_id": "phase-1",
                "title": "Leaf",
                "category": "code",
                "validation": "Test task completion is observable.",
            }
        ],
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


def test_concurrent_apply_creates_one_subtask_tree(temp_db, sample_project) -> None:
    service = _service(temp_db)
    parent = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Concurrent parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    spec = {
        "phases": [{"id": "phase-1", "title": "Phase 1", "task_ids": ["leaf"]}],
        "tasks": [
            {
                "id": "leaf",
                "phase_id": "phase-1",
                "title": "Concurrent leaf",
                "category": "code",
                "validation": "Test task completion is observable.",
            }
        ],
        "dependencies": [],
    }
    runs = [_save_run(service, parent, sample_project, spec) for _ in range(2)]
    original_check = service.find_apply_blocking_expansion_output
    outside_transaction_checks = threading.Barrier(2)

    def synchronized_check(parent_task_id: str):
        result = original_check(parent_task_id)
        if ambient_transaction(temp_db) is None:
            outside_transaction_checks.wait(timeout=5)
        return result

    service.find_apply_blocking_expansion_output = synchronized_check
    start = threading.Barrier(2)

    def apply(run_id: str):
        start.wait(timeout=5)
        return service.apply_run(run_id, session_id=None)

    applied = []
    errors = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(apply, run.id) for run in runs]
        for future in futures:
            try:
                applied.append(future.result(timeout=10))
            except ValueError as exc:
                errors.append(str(exc))

    assert len(applied) == 1
    assert errors == [
        "Expansion output already exists for this task. "
        "Reset expansion output before applying a new run."
    ]
    children = service.task_manager.list_tasks(parent_task_id=parent.id)
    assert [child.title for child in children] == ["Concurrent leaf"]


def test_apply_ignores_closed_obsolete_historical_output(temp_db, sample_project) -> None:
    service = _service(temp_db)
    parent = service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Historical duplicate parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    spec = {
        "phases": [{"id": "phase-1", "title": "Phase 1", "task_ids": ["leaf"]}],
        "tasks": [
            {
                "id": "leaf",
                "phase_id": "phase-1",
                "title": "Leaf",
                "category": "code",
                "validation": "Test task completion is observable.",
            }
        ],
        "dependencies": [],
    }
    first = _save_run(service, parent, sample_project, spec)
    applied = service.apply_run(first.id, session_id=None)
    for task_id in applied.created_task_ids:
        service.task_manager.close_task(task_id, reason="obsolete", force=True)
    second_spec = {
        **spec,
        "tasks": [{**spec["tasks"][0], "id": "leaf-2"}],
        "phases": [{"id": "phase-1", "title": "Phase 1", "task_ids": ["leaf-2"]}],
    }
    second = _save_run(service, parent, sample_project, second_spec)

    reapplied = service.apply_run(second.id, session_id=None)

    assert "leaf-2" in reapplied.task_id_map
    for task_id in applied.created_task_ids:
        old_task = service.task_manager.get_task(task_id)
        assert old_task.closed_at is not None
        assert old_task.closed_reason == "obsolete"
