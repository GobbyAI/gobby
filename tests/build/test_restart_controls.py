"""Tests for task-scoped build restart controls."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

pytestmark = pytest.mark.unit


def test_restart_manifest_replacement_rolls_back_when_reinitialize_fails(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project: dict[str, object],
) -> None:
    from gobby.build.restart_controls import _reset_restart_stage_manifests_from_options
    from gobby.build.service import BuildOptions
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.tasks._stage_states import StageStatesManager
    from gobby.storage.tasks._stage_types import StageManifestSpec

    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=str(sample_project["id"]),
        title="Restart target",
        category="code",
        task_type="feature",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.stage_states.initialize_manifest(
        task.id,
        [StageManifestSpec("development", 0)],
        by_session_id=None,
    )

    def fail_replace(
        self: StageStatesManager,
        task_id: str,
        specs: Sequence[StageManifestSpec],
        **_kwargs: object,
    ) -> None:
        # replace_manifest returns None when the existing manifest shape
        # changed underneath the restart; the caller must raise and leave
        # the prior manifest untouched.
        return None

    monkeypatch.setattr(StageStatesManager, "replace_manifest", fail_replace)

    with pytest.raises(RuntimeError, match="stage manifest changed while restarting"):
        _reset_restart_stage_manifests_from_options(
            temp_db,
            task,
            [task],
            BuildOptions(isolation="worktree"),
        )

    rows = task_manager.stage_states.list_for_task(task.id)
    assert [(row.stage_name, row.position) for row in rows] == [("development", 0)]
