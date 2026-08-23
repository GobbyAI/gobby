"""Runtime synchronization coverage for imported workflow YAML files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
        {
            "name": "imported-agent",
            "type": "agent",
            "provider": "claude",
            # surfaces defaults to ["spawn"], which requires prompts.agent.
            "prompts": {"agent": "Do the imported work."},
        },
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


_STEP_WORKFLOW: dict[str, Any] = {
    "variables": {"required_skills": ["tdd"], "goal": "ship"},
    "exit_condition": "done",
    "steps": [
        {"name": "implement", "prompt": "write the code"},
        {"name": "review", "prompt": "check the diff"},
    ],
}


def test_sync_imported_agent_persists_nested_step_workflow(temp_db: HubDatabase) -> None:
    created = sync_imported_definition(
        temp_db,
        {
            "name": "imported-step-agent",
            "type": "agent",
            "provider": "claude",
            # surfaces defaults to ["spawn"], which requires prompts.agent.
            "prompts": {"agent": "Run the imported step workflow."},
            "step_workflow": _STEP_WORKFLOW,
        },
        None,
    )
    manager = AgentDefinitionManager(temp_db)
    row = manager.get(created.id)
    assert row.step_workflow_id is not None
    stored = row.definition_json["step_workflow"]
    assert stored["exit_condition"] == "done"
    assert [step["name"] for step in stored["steps"]] == ["implement", "review"]

    raw = temp_db.fetchone(
        "SELECT definition_json FROM agent_definitions WHERE id = %s",
        (created.id,),
    )
    assert raw is not None
    payload = raw["definition_json"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert "step_workflow" not in payload
    assert "steps" not in payload


def test_sync_imported_variable_uses_one_name_for_kind_and_upsert(temp_db: HubDatabase) -> None:
    created = sync_imported_definition(
        temp_db,
        {"name": "name-only-var", "type": "variable", "value": 1},
        None,
    )
    assert created.name == "name-only-var"
    updated = sync_imported_definition(
        temp_db,
        {"name": "name-only-var", "type": "variable", "value": 2},
        None,
    )
    assert updated.id == created.id
    stored = SessionVariableDefaultManager(temp_db).get_by_name("name-only-var")
    assert stored is not None
    assert stored.default_value == 2

    preferred = sync_imported_definition(
        temp_db,
        {"variable": "pref-var", "name": "other-name", "type": "variable", "value": 3},
        None,
    )
    assert preferred.name == "pref-var"
    assert SessionVariableDefaultManager(temp_db).get_by_name("other-name") is None
    with pytest.raises(ValueError, match="from 'variable' to 'agent'"):
        sync_imported_definition(
            temp_db,
            {"name": "pref-var", "type": "agent", "provider": "claude"},
            None,
        )
