from __future__ import annotations

import pytest

from gobby.build.service import BuildOptions, build
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _options(**overrides: object) -> BuildOptions:
    values = {
        "profile": "full",
        "skip_stages": [],
        "isolation": "none",
        "unattended": False,
        "composer_yolo": False,
        "target_branch": None,
        "assigned_agent": "backend-developer",
    }
    values.update(overrides)
    return BuildOptions(**values)


async def test_default_manifest_positions_are_zero_indexed_and_returned(
    temp_db,
    sample_project,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    leaf = task_manager.create_task(
        project_id=sample_project["id"],
        title="Manifest build",
        category="test",
        task_type="task",
    )

    result = await build(
        f"#{leaf.seq_num}",
        _options(),
        db=temp_db,
        project_id=sample_project["id"],
    )

    assert result.stage_manifest is not None
    payload_positions = [row["position"] for row in result.stage_manifest]
    assert payload_positions == list(range(len(result.stage_manifest)))

    rows = task_manager.stage_states.list_for_task(result.task_id)
    assert [row.position for row in rows] == payload_positions
    assert [row.stage_name for row in rows] == [row["stage_name"] for row in result.stage_manifest]
