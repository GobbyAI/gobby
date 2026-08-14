"""Pipeline extends merges parent and child payload through PipelineLoader."""

from __future__ import annotations

import pytest

from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.pipeline_loader import PipelineLoader

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_pipeline_extends_merges_inputs_and_steps(temp_db: HubDatabase) -> None:
    manager = PipelineDefinitionManager(temp_db)
    manager.create(
        name="parent-pipe",
        definition_json={
            "name": "parent-pipe",
            "type": "pipeline",
            "inputs": {"shared": {"type": "string", "default": "from-parent"}},
            "steps": [{"id": "parent", "exec": "echo parent"}],
        },
    )
    manager.create(
        name="child-pipe",
        definition_json={
            "name": "child-pipe",
            "type": "pipeline",
            "extends": "parent-pipe",
            "inputs": {"child": {"type": "string", "default": "from-child"}},
            "steps": [{"id": "child", "exec": "echo child"}],
        },
    )
    loader = PipelineLoader(db=temp_db)
    child = await loader.load_pipeline("child-pipe")
    assert child is not None
    assert [step.id for step in child.steps] == ["parent", "child"]
    assert child.inputs["shared"]["default"] == "from-parent"
    assert child.inputs["child"]["default"] == "from-child"
