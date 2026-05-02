from __future__ import annotations

import pytest

from tests.storage.tasks._stage_test_helpers import create_task, initialize_manifest, spec

pytestmark = pytest.mark.unit


def test_stages_loaded(temp_db, sample_project) -> None:
    from gobby.dispatch.dispatcher import reload_candidate

    task = create_task(
        temp_db,
        sample_project,
        title="Hydrate dispatch stages",
        category="test",
        task_type="task",
        allow_automation=True,
    )
    initialize_manifest(
        temp_db,
        task.id,
        [spec("planning", 0), spec("development", 1), spec("merge", 2)],
    )

    reloaded = reload_candidate(task.id, db=temp_db, project_id=sample_project["id"])

    assert reloaded is not None
    assert [stage.stage_name for stage in reloaded.stages] == [
        "planning",
        "development",
        "merge",
    ]
    assert [stage.position for stage in reloaded.stages] == [0, 1, 2]
    assert [stage.state for stage in reloaded.stages] == ["ready", "ready", "ready"]
