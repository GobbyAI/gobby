"""Tests for pipeline loader override conflict handling."""

from __future__ import annotations

import pytest

from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.pipeline_loader import PipelineLoader
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit


def _pipeline_body(name: str, *, override: bool = False) -> dict[str, object]:
    data: dict[str, object] = {
        "name": name,
        "type": "pipeline",
        "steps": [{"id": "work", "exec": "echo hi"}],
    }
    if override:
        data["override"] = True
    return data


@pytest.mark.asyncio
async def test_conflict_without_override_label_fails_loud(
    isolated_checkout_factory: IsolatedCheckoutFactory, temp_db: HubDatabase
) -> None:
    project = isolated_checkout_factory(temp_db, "test-project").project
    manager = PipelineDefinitionManager(temp_db)
    manager.create(
        name="shared-pipeline",
        definition_json=_pipeline_body("shared-pipeline"),
        tags=["gobby"],
    )
    manager.create(
        name="shared-pipeline",
        definition_json=_pipeline_body("shared-pipeline"),
        project_id=project.id,
        source="project",
        tags=["user"],
    )

    loader = PipelineLoader(db=temp_db)
    with pytest.raises(ValueError, match="override: true"):
        await loader.load_pipeline("shared-pipeline", project_path=project.id)


@pytest.mark.asyncio
async def test_conflict_with_override_label_loads_project_copy(
    isolated_checkout_factory: IsolatedCheckoutFactory, temp_db: HubDatabase
) -> None:
    project = isolated_checkout_factory(temp_db, "test-project").project
    manager = PipelineDefinitionManager(temp_db)
    manager.create(
        name="shared-pipeline",
        definition_json=_pipeline_body("shared-pipeline"),
        tags=["gobby"],
    )
    manager.create(
        name="shared-pipeline",
        definition_json=_pipeline_body("shared-pipeline", override=True),
        project_id=project.id,
        source="project",
        tags=["user"],
    )

    pipeline = await PipelineLoader(db=temp_db).load_pipeline(
        "shared-pipeline", project_path=project.id
    )
    assert pipeline is not None
    assert pipeline.name == "shared-pipeline"
