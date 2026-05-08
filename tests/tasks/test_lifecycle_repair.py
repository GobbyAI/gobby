"""Scoped lifecycle manifest repair tests."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.lifecycle_repair import LifecycleRepair

pytestmark = pytest.mark.unit


def _stage_names(manager: LocalTaskManager, task_id: str) -> list[str]:
    return [row.stage_name for row in manager.stage_states.list_for_task(task_id)]


def test_repair_refuses_unscoped_invocation(temp_db) -> None:
    repair = LifecycleRepair(LocalTaskManager(temp_db))

    with pytest.raises(ValueError, match="--task or --provenance"):
        repair.run()


def test_repair_dry_runs_metadata_only_auto_seed_without_mutation(
    temp_db,
    sample_project,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Historical auto-seeded task",
        task_type="task",
    )
    manager.initialize_task_manifest(task.id, stage_names=["development", "pr", "merge"])

    result = LifecycleRepair(manager).run(task_id=task.id)

    assert result.apply is False
    assert len(result.candidates) == 1
    assert result.candidates[0].action == "remove_unused_manifest"
    assert _stage_names(manager, task.id) == ["development", "pr", "merge"]


def test_repair_apply_removes_metadata_only_auto_seed(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Historical auto-seeded task",
        task_type="task",
    )
    manager.initialize_task_manifest(task.id, stage_names=["development"])

    result = LifecycleRepair(manager).run(task_id=task.id, apply=True)

    assert result.candidates[0].applied is True
    assert manager.stage_states.list_for_task(task.id) == []
    assert manager.lifecycle_events.list_events(task.id)[-1].reason == (
        "repair-lifecycle:remove-unused-manifest"
    )


def test_repair_reseeds_expansion_child_from_parent_scope(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    parent = manager.create_task(
        project_id=sample_project["id"],
        title="Expansion parent",
        task_type="epic",
    )
    manager.initialize_task_manifest(parent.id, stage_names=["development", "pr", "merge"])
    child = manager.create_task(
        project_id=sample_project["id"],
        title="Historical expansion child",
        parent_task_id=parent.id,
        task_type="task",
        labels=["expansion-run:abc"],
    )
    manager.initialize_task_manifest(child.id, stage_names=["holistic_qa", "pr", "merge"])

    result = LifecycleRepair(manager).run(provenance="expansion-run:abc", apply=True)

    assert len(result.candidates) == 1
    assert result.candidates[0].applied is True
    assert _stage_names(manager, child.id) == ["development", "pr", "merge"]
    assert manager.lifecycle_events.list_events(child.id)[-1].reason == (
        "repair-lifecycle:reseed-expansion-manifest"
    )


def test_repair_does_not_remove_development_from_historical_leaf(
    temp_db,
    sample_project,
) -> None:
    manager = LocalTaskManager(temp_db)
    phase = manager.create_task(
        project_id=sample_project["id"],
        title="Historical phase",
        task_type="epic",
        labels=["expansion-run:abc"],
    )
    manager.initialize_task_manifest(phase.id, stage_names=["holistic_qa", "pr", "merge"])
    leaf = manager.create_task(
        project_id=sample_project["id"],
        title="Historical leaf",
        parent_task_id=phase.id,
        task_type="task",
        labels=["expansion-run:abc"],
    )
    manager.initialize_task_manifest(leaf.id, stage_names=["development", "pr", "merge"])

    result = LifecycleRepair(manager).run(task_id=leaf.id)

    assert result.candidates == []


def test_repair_reseeds_historical_phase_wrapper_to_development_first(
    temp_db,
    sample_project,
) -> None:
    manager = LocalTaskManager(temp_db)
    parent = manager.create_task(
        project_id=sample_project["id"],
        title="Metadata-only expansion parent",
        task_type="epic",
    )
    phase = manager.create_task(
        project_id=sample_project["id"],
        title="Historical phase",
        parent_task_id=parent.id,
        task_type="epic",
        labels=["expansion-run:abc"],
    )
    manager.initialize_task_manifest(phase.id, stage_names=["holistic_qa", "pr", "merge"])

    result = LifecycleRepair(manager).run(task_id=phase.id, apply=True)

    assert result.candidates[0].applied is True
    assert _stage_names(manager, phase.id) == ["development", "holistic_qa", "pr", "merge"]


def test_repair_skips_active_expansion_rows_without_force(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    parent = manager.create_task(
        project_id=sample_project["id"],
        title="Expansion parent",
        task_type="epic",
    )
    manager.initialize_task_manifest(parent.id, stage_names=["development", "pr", "merge"])
    child = manager.create_task(
        project_id=sample_project["id"],
        title="Active expansion child",
        parent_task_id=parent.id,
        task_type="task",
        labels=["expansion-run:active"],
    )
    manager.initialize_task_manifest(child.id, stage_names=["holistic_qa", "pr", "merge"])
    manager.stage_states.start_stage(child.id, "holistic_qa", by_session_id=None)

    result = LifecycleRepair(manager).run(provenance="expansion-run:active", apply=True)

    assert result.candidates[0].skipped is True
    assert result.candidates[0].skip_reason == "active_lifecycle_rows"
    assert _stage_names(manager, child.id) == ["holistic_qa", "pr", "merge"]


def test_repair_force_reseeds_active_task_scope(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    parent = manager.create_task(
        project_id=sample_project["id"],
        title="Expansion parent",
        task_type="epic",
    )
    manager.initialize_task_manifest(parent.id, stage_names=["development"])
    child = manager.create_task(
        project_id=sample_project["id"],
        title="Active expansion child",
        parent_task_id=parent.id,
        task_type="task",
        labels=["expansion-run:active"],
    )
    manager.initialize_task_manifest(child.id, stage_names=["holistic_qa"])
    manager.stage_states.start_stage(child.id, "holistic_qa", by_session_id=None)

    result = LifecycleRepair(manager).run(task_id=child.id, apply=True, force=True)

    assert result.candidates[0].applied is True
    assert _stage_names(manager, child.id) == ["development"]
