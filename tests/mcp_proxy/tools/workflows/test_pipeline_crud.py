"""Pipeline MCP CRUD against PipelineDefinitionManager."""

from __future__ import annotations

import pytest
import yaml

from gobby.mcp_proxy.tools.workflows._pipelines import (
    _require_pipeline,
    create_pipeline_definition,
    delete_pipeline_definition,
    export_pipeline_definition,
    update_pipeline_definition,
)
from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.pipeline_loader import PipelineLoader

pytestmark = pytest.mark.integration

VALID_PIPELINE_YAML = """
name: test-pipeline
description: A test pipeline
type: pipeline
steps:
  - id: step1
    exec: echo hello
"""


@pytest.fixture
def manager(temp_db: HubDatabase) -> PipelineDefinitionManager:
    return PipelineDefinitionManager(temp_db)


@pytest.fixture
def loader(temp_db: HubDatabase) -> PipelineLoader:
    return PipelineLoader(db=temp_db)


def test_create_update_export_delete_pipeline(
    manager: PipelineDefinitionManager, loader: PipelineLoader
) -> None:
    created = create_pipeline_definition(manager, loader, VALID_PIPELINE_YAML)
    assert created["success"] is True
    assert created["definition"]["name"] == "test-pipeline"
    assert created["definition"]["workflow_type"] == "pipeline"
    row = manager.get_by_name("test-pipeline")
    assert row is not None
    assert row.enabled is True

    updated = update_pipeline_definition(
        manager, loader, name="test-pipeline", description="updated"
    )
    assert updated["success"] is True
    assert updated["definition"]["description"] == "updated"

    exported = export_pipeline_definition(manager, name="test-pipeline")
    assert exported["success"] is True
    data = yaml.safe_load(exported["yaml_content"])
    assert data["name"] == "test-pipeline"
    assert data["type"] == "pipeline"

    deleted = delete_pipeline_definition(manager, loader, name="test-pipeline", force=True)
    assert deleted["success"] is True
    assert manager.get_by_name("test-pipeline") is None


def test_create_rejects_non_pipeline_yaml(
    manager: PipelineDefinitionManager, loader: PipelineLoader
) -> None:
    result = create_pipeline_definition(
        manager, loader, "name: not-pipe\ntype: rule\nevent: stop\neffects: []\n"
    )
    assert result["success"] is False
    assert "type: pipeline" in result["error"]


def test_require_pipeline_not_found(manager: PipelineDefinitionManager) -> None:
    err = _require_pipeline(manager, name="missing")
    assert err is not None
    assert "not found" in err["error"]


def test_pipelines_module_has_no_generic_imports() -> None:
    from pathlib import Path

    source = Path("src/gobby/mcp_proxy/tools/workflows/_pipelines.py").read_text(encoding="utf-8")
    assert "from gobby.mcp_proxy.tools.workflows._definitions" not in source
    assert "RuleDefinitionManager" not in source
    assert "create_workflow_definition" not in source
