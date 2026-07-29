"""Tests for build dispatch state cascades in task storage."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import (
    Isolation,
    LocalTaskManager,
    StageManifestSpec,
    StageState,
    cascade_build_state_to_subtree,
)
from gobby.storage.tasks._build_cascade import (
    _remove_pristine_omitted_stages_for_build_cascade,
)
from gobby.storage.tasks._runtime_mutex import (
    DispatchMutexUnavailableError,
    RuntimeDispatchMutex,
)
from gobby.storage.tasks._stage_states import StageStatesManager
from tests.phase5_contract_helpers import source_text

pytestmark = pytest.mark.unit


def test_cascade_build_state_updates_subtree_without_agent_or_lifecycle_fields(
    temp_db,
    sample_project,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=sample_project["id"],
        title="Automated epic",
        task_type="epic",
        category="planning",
        labels=["keep-me"],
        validation_criteria="Test task completion is observable.",
    )
    child_epic = task_manager.create_task(
        project_id=sample_project["id"],
        title="Child epic",
        parent_task_id=epic.id,
        task_type="epic",
        category="planning",
        validation_criteria="Test task completion is observable.",
    )
    leaf = task_manager.create_task(
        project_id=sample_project["id"],
        title="Leaf task",
        parent_task_id=child_epic.id,
        category="code",
        assigned_agent="backend-developer",
        additional_skills=["sql-review"],
        validation_criteria="Test task completion is observable.",
    )
    sibling = task_manager.create_task(
        project_id=sample_project["id"],
        title="Sibling task",
        parent_task_id=epic.id,
        category="docs",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(epic.id, stage_names=["development", "merge"])

    kwargs = {
        "isolation": Isolation.clone,
        "unattended": True,
        "allow_automation": True,
    }

    result = cascade_build_state_to_subtree(temp_db, epic.id, **kwargs)

    assert result.updated_count == 4
    assert result.failures == ()
    for task_id in (epic.id, child_epic.id, leaf.id, sibling.id):
        task = task_manager.get_task(task_id)
        assert task.allow_automation is True
        assert task.unattended is True
        assert task.isolation is Isolation.clone
        assert not any(label.startswith("stage-:") for label in task.labels or [])

    updated_epic = task_manager.get_task(epic.id)
    updated_leaf = task_manager.get_task(leaf.id)
    assert "keep-me" in (updated_epic.labels or [])
    assert updated_leaf.assigned_agent == "backend-developer"
    assert updated_leaf.additional_skills == ["sql-review"]


def test_cascade_uses_initialize_manifest() -> None:
    source = source_text("src/gobby/storage/tasks/_build_cascade.py")

    assert "initialize_manifest(" in source


def test_pristine_stage_prune_holds_dispatch_mutex_against_competing_start(
    temp_db,
    sample_project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Prunable child",
        category="code",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(task.id, stage_names=["development", "merge"])
    stage_states = task_manager.stage_states
    competing_stage_states = LocalTaskManager(temp_db).stage_states
    original_list = stage_states.list_for_task
    competing_start_refused = False

    def list_while_start_competes(task_id: str) -> list[StageState]:
        nonlocal competing_start_refused
        with pytest.raises(DispatchMutexUnavailableError):
            competing_stage_states.start_stage(
                task_id,
                "development",
                by_session_id="competing-dispatcher",
            )
        competing_start_refused = True
        return original_list(task_id)

    monkeypatch.setattr(stage_states, "list_for_task", list_while_start_competes)

    pruned = _remove_pristine_omitted_stages_for_build_cascade(
        temp_db,
        stage_states,
        task.id,
        [StageManifestSpec("merge", 0)],
    )

    assert pruned is True
    assert competing_start_refused is True
    assert [row.stage_name for row in original_list(task.id)] == ["merge"]


def test_pristine_stage_prune_rechecks_state_after_acquiring_mutex(
    temp_db,
    sample_project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Progressed child",
        category="code",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(task.id, stage_names=["development", "merge"])
    original_enter = RuntimeDispatchMutex.__enter__

    def enter_after_stage_progresses(mutex: RuntimeDispatchMutex) -> RuntimeDispatchMutex:
        entered = original_enter(mutex)
        temp_db.execute(
            """
            UPDATE task_stage_states
               SET state = 'in_progress', work_attempt_count = 1
             WHERE task_id = %s AND stage_name = 'development'
            """,
            (task.id,),
        )
        return entered

    monkeypatch.setattr(RuntimeDispatchMutex, "__enter__", enter_after_stage_progresses)

    pruned = _remove_pristine_omitted_stages_for_build_cascade(
        temp_db,
        task_manager.stage_states,
        task.id,
        [StageManifestSpec("merge", 0)],
    )

    rows = task_manager.stage_states.list_for_task(task.id)
    assert pruned is False
    assert [(row.stage_name, row.state) for row in rows] == [
        ("development", "in_progress"),
        ("merge", "ready"),
    ]


@pytest.mark.parametrize(
    ("injected_error", "expected_retryable"),
    [
        (DispatchMutexUnavailableError("injected busy child"), True),
        (RuntimeError("injected manifest failure"), False),
    ],
)
def test_cascade_reports_manifest_failure_without_enabling_failed_child(
    temp_db,
    sample_project,
    monkeypatch: pytest.MonkeyPatch,
    injected_error: Exception,
    expected_retryable: bool,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=sample_project["id"],
        title="Automated epic",
        task_type="epic",
        category="planning",
        validation_criteria="Test task completion is observable.",
    )
    failed_child = task_manager.create_task(
        project_id=sample_project["id"],
        title="Busy child",
        parent_task_id=epic.id,
        category="code",
        validation_criteria="Test task completion is observable.",
    )
    healthy_child = task_manager.create_task(
        project_id=sample_project["id"],
        title="Healthy child",
        parent_task_id=epic.id,
        category="code",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(epic.id, stage_names=["development", "merge"])
    original_initialize = StageStatesManager.initialize_manifest

    def initialize_with_busy_child(
        self: StageStatesManager,
        task_id: str,
        *args: object,
        **kwargs: object,
    ) -> object:
        if task_id == failed_child.id:
            raise injected_error
        return original_initialize(self, task_id, *args, **kwargs)

    monkeypatch.setattr(StageStatesManager, "initialize_manifest", initialize_with_busy_child)

    result = cascade_build_state_to_subtree(
        temp_db,
        epic.id,
        isolation=Isolation.worktree,
        unattended=True,
        allow_automation=True,
    )

    assert result.updated_count == 2
    assert [(failure.task_id, failure.retryable) for failure in result.failures] == [
        (failed_child.id, expected_retryable)
    ]
    assert task_manager.get_task(failed_child.id).allow_automation is False
    assert task_manager.stage_states.list_for_task(failed_child.id) == []
    assert task_manager.get_task(healthy_child.id).allow_automation is True
    assert task_manager.stage_states.list_for_task(healthy_child.id)
    invalid_automated = temp_db.fetchone(
        """
        SELECT tasks.id
        FROM tasks
        LEFT JOIN task_stage_states ON task_stage_states.task_id = tasks.id
        WHERE tasks.allow_automation = TRUE
        GROUP BY tasks.id
        HAVING COUNT(task_stage_states.task_id) = 0
        """
    )
    assert invalid_automated is None


def test_cascade_can_force_merge_into_legacy_child_manifest_scope(
    temp_db,
    sample_project,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=sample_project["id"],
        title="Legacy parent scope",
        task_type="epic",
        category="planning",
        validation_criteria="Test task completion is observable.",
    )
    child = task_manager.create_task(
        project_id=sample_project["id"],
        title="Child must merge",
        parent_task_id=epic.id,
        task_type="task",
        category="docs",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(epic.id, stage_names=["development"])

    cascade_build_state_to_subtree(
        temp_db,
        epic.id,
        isolation=Isolation.worktree,
        unattended=False,
        allow_automation=True,
        include_merge_stage=True,
    )

    child_rows = task_manager.stage_states.list_for_task(child.id)
    assert [row.stage_name for row in child_rows] == ["development", "merge"]


def test_cascade_never_forces_merge_onto_an_expansion_only_parent(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=sample_project["id"],
        title="Expansion-only parent scope",
        task_type="epic",
        category="planning",
        validation_criteria="Test task completion is observable.",
    )
    child = task_manager.create_task(
        project_id=sample_project["id"],
        title="Child has nothing to merge yet",
        parent_task_id=epic.id,
        task_type="task",
        category="docs",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(epic.id, stage_names=["expansion"])
    task_manager.initialize_task_manifest(child.id, stage_names=["development"])

    result = cascade_build_state_to_subtree(
        temp_db,
        epic.id,
        isolation=Isolation.worktree,
        unattended=False,
        allow_automation=True,
        include_merge_stage=True,
    )

    child_rows = task_manager.stage_states.list_for_task(child.id)
    assert [row.stage_name for row in child_rows] == []
    # An empty derivation is a build with no per-child lifecycle, not a failure:
    # the child still receives build state.
    assert result.failures == ()
    assert result.updated_count == 2
    refreshed = task_manager.get_task(child.id)
    assert refreshed is not None
    assert refreshed.allow_automation is True


def test_cascade_no_legacy_label_writes() -> None:
    source = source_text("src/gobby/storage/tasks/_build_cascade.py")

    assert "skip_stage_labels" not in source
    assert "_normalize_skip_stage_labels" not in source
    assert "stage-:" not in source
