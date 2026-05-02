"""Runtime artifact manager must stop exposing legacy cap columns."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._artifacts import _ARTIFACT_FIELDS, TaskArtifactManager, TaskArtifacts
from tests.phase5_contract_helpers import LEGACY_CAP_COLUMNS

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("field", LEGACY_CAP_COLUMNS)
def test_artifact_fields_excludes_legacy_caps(field: str) -> None:
    assert field not in _ARTIFACT_FIELDS


def test_taskartifacts_dataclass_fields_excludes_legacy_caps() -> None:
    dataclass_fields = set(TaskArtifacts.__dataclass_fields__)
    assert dataclass_fields.isdisjoint(LEGACY_CAP_COLUMNS)


def test_set_artifact_rejects_max_review_rounds_kwarg(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Artifact cap",
    )
    manager = TaskArtifactManager(temp_db)

    with pytest.raises(ValueError, match="Unknown task artifact field"):
        manager.set_artifact(task.id, field="max_review_rounds", value=3)


def test_set_artifacts_atomic_rejects_max_review_rounds_kwarg(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Artifact caps",
    )
    manager = TaskArtifactManager(temp_db)

    with pytest.raises(ValueError, match="Unknown task artifact field"):
        manager.set_artifacts_atomic(task.id, max_review_rounds=3)
