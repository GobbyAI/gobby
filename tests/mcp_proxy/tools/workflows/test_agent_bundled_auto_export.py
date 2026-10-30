"""MCP agent edits must not create implicit copies of bundled definitions."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.mcp_proxy.tools.workflows._agents import (
    update_agent_rules,
    update_agent_step_workflow,
    update_agent_variables,
)
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from tests.fixtures.agent_definitions import make_agent_definition
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("edit", ["rules", "variables", "steps"])
@pytest.mark.parametrize("ownership", ["bundled", "project-shadow", "custom"])
@pytest.mark.parametrize("make_global", [False, True])
def test_agent_edits_export_only_custom_names(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    edit: str,
    ownership: str,
    make_global: bool,
) -> None:
    monkeypatch.setattr("gobby.utils.dev.is_dev_mode", lambda _path: False)
    global_dir = tmp_path / "global-agents"
    monkeypatch.setattr("gobby.paths.get_global_agents_dir", lambda: global_dir)
    manager = AgentDefinitionManager(temp_db)
    body = make_agent_definition(
        name="export-agent", prompts={"agent": "Run the assigned task."}
    ).model_dump(mode="json")
    global_row = manager.create(
        name="export-agent",
        definition_json=body,
        tags=["user"] if ownership == "custom" else ["gobby"],
        source="installed",
    )
    project_id = None
    row = global_row
    if ownership == "project-shadow":
        project_id = isolated_checkout_factory(temp_db, "export-shadow").project.id
        row = manager.create(
            name="export-agent", definition_json=body, tags=["user"], project_id=project_id
        )
    kwargs: dict[str, Any] = {
        "project_path": tmp_path,
        "make_global_template": make_global,
        "project_id": project_id,
    }
    if edit == "rules":
        result = update_agent_rules(manager, row.name, add=["added-rule"], **kwargs)
    elif edit == "variables":
        result = update_agent_variables(manager, row.name, set_vars={"edited": True}, **kwargs)
    else:
        result = update_agent_step_workflow(
            manager,
            row.name,
            step_workflow={"steps": [{"name": "start"}]},
            **kwargs,
        )

    assert result["success"] is True
    updated = manager.get(row.id)
    assert updated is not None
    if edit == "rules":
        assert "added-rule" in updated.definition_json["workflows"]["rules"]
    elif edit == "variables":
        assert updated.definition_json["workflows"]["variables"]["edited"] is True
    else:
        workflow = manager.get_step_workflow(row.id)
        assert workflow is not None
        assert workflow.steps_json[0]["name"] == "start"
    exported = list(tmp_path.rglob("*.yaml"))
    if ownership == "custom":
        assert len(exported) == 1
        assert yaml.safe_load(exported[0].read_text())["name"] == row.name
    else:
        assert exported == []
    if ownership == "project-shadow":
        assert manager.get(global_row.id) == global_row
