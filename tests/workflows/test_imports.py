"""Runtime synchronization coverage for imported workflow YAML files."""

from pathlib import Path

import pytest

from gobby.mcp_proxy.tools.workflows._import import reload_cache
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.workflows.imports import sync_imported_definition
from gobby.workflows.pipeline_loader import PipelineLoader

pytestmark = pytest.mark.integration


def _write_pipeline(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""name: {name}
type: pipeline
steps:
  - id: work
    exec: echo {name}
""",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_sync_imported_workflows_loads_project_and_global_files_without_restart(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_path = tmp_path / "project"
    project = LocalProjectManager(temp_db).create(
        "workflow-import-project",
        repo_path=str(project_path),
    )
    global_dir = tmp_path / "global-workflows"
    _write_pipeline(global_dir / "pipelines" / "global-import.yaml", "global-import")
    _write_pipeline(
        project_path / ".gobby" / "workflows" / "pipelines" / "project-import.yml",
        "project-import",
    )
    monkeypatch.setattr(
        "gobby.workflows.imports.get_global_workflows_dir",
        lambda: global_dir,
    )
    loader = PipelineLoader(db=temp_db)

    def zero_sync(_db: object) -> dict[str, int]:
        return {"synced": 0, "updated": 0}

    monkeypatch.setattr("gobby.workflows.sync_pipelines.sync_bundled_pipelines", zero_sync)
    monkeypatch.setattr("gobby.workflows.sync_rules.sync_bundled_rules", zero_sync)
    monkeypatch.setattr("gobby.workflows.sync_variables.sync_bundled_variables", zero_sync)
    monkeypatch.setattr("gobby.agents.sync.sync_bundled_agents", zero_sync)

    result = reload_cache(loader, db=temp_db)

    assert result["imported_workflows_synced"] == 2
    assert await loader.load_pipeline("global-import") is not None
    assert await loader.load_pipeline("project-import", project.id) is not None


def test_sync_imported_definition_writes_all_four_kinds(temp_db: HubDatabase) -> None:
    agent = sync_imported_definition(
        temp_db,
        {"name": "imported-agent", "type": "agent", "provider": "claude"},
        None,
    )
    rule = sync_imported_definition(
        temp_db,
        {
            "name": "imported-rule",
            "type": "rule",
            "event": "before_tool",
            "effects": [{"type": "block", "reason": "nope"}],
        },
        None,
    )
    variable = sync_imported_definition(
        temp_db,
        {"name": "imported-variable", "type": "variable", "value": 1},
        None,
    )
    pipeline = sync_imported_definition(
        temp_db,
        {
            "name": "imported-pipeline",
            "type": "pipeline",
            "steps": [{"id": "s1", "exec": "echo hi"}],
        },
        None,
    )
    assert AgentDefinitionManager(temp_db).get(agent.id).name == "imported-agent"
    assert RuleDefinitionManager(temp_db).get(rule.id).name == "imported-rule"
    assert SessionVariableDefaultManager(temp_db).get(variable.id).name == "imported-variable"
    assert PipelineDefinitionManager(temp_db).get(pipeline.id).name == "imported-pipeline"


def test_sync_imported_definition_refuses_kind_change_by_table(temp_db: HubDatabase) -> None:
    sync_imported_definition(
        temp_db,
        {
            "name": "shared-name",
            "type": "rule",
            "event": "before_tool",
            "effects": [{"type": "block", "reason": "nope"}],
        },
        None,
    )
    with pytest.raises(ValueError, match="from 'rule' to 'pipeline'"):
        sync_imported_definition(
            temp_db,
            {
                "name": "shared-name",
                "type": "pipeline",
                "steps": [{"id": "s1", "exec": "echo hi"}],
            },
            None,
        )
