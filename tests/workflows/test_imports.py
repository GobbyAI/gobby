"""Runtime synchronization coverage for imported workflow YAML files."""

from pathlib import Path

import pytest

from gobby.mcp_proxy.tools.workflows._import import reload_cache
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.workflows.imports import sync_imported_definition
from gobby.workflows.loader import WorkflowLoader

pytestmark = pytest.mark.integration


def _write_workflow(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""name: {name}
type: step
steps:
  - name: work
    allowed_tools: all
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
    _write_workflow(global_dir / "global-import.yaml", "global-import")
    _write_workflow(
        project_path / ".gobby" / "workflows" / "project-import.yml",
        "project-import",
    )
    monkeypatch.setattr(
        "gobby.workflows.imports.get_global_workflows_dir",
        lambda: global_dir,
    )
    loader = WorkflowLoader(db=temp_db)

    def zero_sync(_db: object) -> dict[str, int]:
        return {"synced": 0, "updated": 0}

    monkeypatch.setattr("gobby.workflows.sync_pipelines.sync_bundled_pipelines", zero_sync)
    monkeypatch.setattr("gobby.workflows.sync_rules.sync_bundled_rules", zero_sync)
    monkeypatch.setattr("gobby.workflows.sync_variables.sync_bundled_variables", zero_sync)
    monkeypatch.setattr("gobby.agents.sync.sync_bundled_agents", zero_sync)

    result = reload_cache(loader, db=temp_db)

    assert result["imported_workflows_synced"] == 2
    assert await loader.load_workflow("global-import") is not None
    assert await loader.load_workflow("project-import", project.id) is not None


def test_sync_imported_definition_rejects_agent_kind() -> None:
    with pytest.raises(ValueError, match="agent import path"):
        sync_imported_definition(
            None,
            {"name": "rogue-agent", "type": "agent", "provider": "claude"},
            None,
        )


def test_sync_imported_definition_rejects_rule_kind() -> None:
    with pytest.raises(ValueError, match="rule tools"):
        sync_imported_definition(
            None,
            {
                "name": "rogue-rule",
                "type": "rule",
                "event": "before_tool",
                "effects": [{"type": "block", "reason": "nope"}],
            },
            None,
        )


def test_sync_imported_definition_rejects_variable_kind() -> None:
    with pytest.raises(ValueError, match="variable domain MCP tools"):
        sync_imported_definition(
            None,
            {"name": "rogue-variable", "type": "variable", "value": 1},
            None,
        )
