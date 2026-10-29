"""MCP edits preserve runtime changes without exporting implicit bundled overrides."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.mcp_proxy.tools.workflows._pipelines import update_pipeline_definition
from gobby.mcp_proxy.tools.workflows._rules import update_rule
from gobby.mcp_proxy.tools.workflows._variables import update_variable
from gobby.storage.definitions.pipelines import PipelineDefinitionManager, PipelineDefinitionRow
from gobby.storage.definitions.rules import RuleDefinitionManager, RuleDefinitionRow
from gobby.storage.definitions.variables import (
    SessionVariableDefaultManager,
    SessionVariableDefaultRow,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.imports import sync_imported_definition, sync_imported_workflow_file
from gobby.workflows.pipeline_loader import PipelineLoader

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", ["rule", "pipeline", "variable"])
@pytest.mark.parametrize("bundled", [False, True])
@pytest.mark.parametrize("make_global", [False, True])
def test_mcp_edits_export_only_user_owned_names(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    bundled: bool,
    make_global: bool,
) -> None:
    monkeypatch.setattr("gobby.utils.dev.is_dev_mode", lambda _path: False)
    global_root = tmp_path / "global"
    monkeypatch.setattr("gobby.paths.get_global_rules_dir", lambda: global_root / "rules")
    monkeypatch.setattr("gobby.paths.get_global_pipelines_dir", lambda: global_root / "pipelines")
    monkeypatch.setattr("gobby.paths.get_global_variables_dir", lambda: global_root / "variables")
    definitions: dict[str, dict[str, Any]] = {
        "rule": {"event": "before_tool", "effects": [{"type": "block", "reason": "policy"}]},
        "pipeline": {"steps": [{"id": "check", "exec": "true"}]},
        "variable": {"value": 1},
    }
    row = sync_imported_definition(
        temp_db, {"type": kind, "name": "exported-definition", **definitions[kind]}, None
    )
    kwargs: dict[str, Any] = {"project_path": tmp_path, "make_global_template": make_global}
    tags = ["gobby"] if bundled else ["user"]
    updated: RuleDefinitionRow | PipelineDefinitionRow | SessionVariableDefaultRow
    if kind == "rule":
        rule_manager = RuleDefinitionManager(temp_db)
        rule_manager.update(row.id, tags=tags)
        result = update_rule(
            rule_manager, row.name, description="edited", enabled=False, priority=17, **kwargs
        )
        updated = rule_manager.get(row.id)
    elif kind == "pipeline":
        pipeline_manager = PipelineDefinitionManager(temp_db)
        pipeline_manager.update(row.id, tags=tags)
        result = update_pipeline_definition(
            pipeline_manager,
            PipelineLoader(db=temp_db),
            row.name,
            description="edited",
            enabled=False,
            version="2.0",
            **kwargs,
        )
        updated = pipeline_manager.get(row.id)
    else:
        variable_manager = SessionVariableDefaultManager(temp_db)
        variable_manager.update(row.id, tags=tags)
        result = update_variable(
            variable_manager, row.name, description="edited", value=2, **kwargs
        )
        updated = variable_manager.get(row.id)

    assert result["success"] is True
    assert updated is not None
    assert updated.description == "edited"
    exported = list(tmp_path.rglob("*.yaml"))
    if bundled:
        assert exported == []
    else:
        assert len(exported) == 1
        assert exported[0].name == "exported-definition.yaml"
        payload = yaml.safe_load(exported[0].read_text())
        body = payload["rules"][row.name] if kind == "rule" else payload
        assert body["description"] == "edited"
        if kind == "rule":
            assert body["enabled"] is False
            assert body["priority"] == 17
        elif kind == "pipeline":
            assert body["enabled"] is False
            assert body["version"] == "2.0"
        else:
            assert body["value"] == 2
        assert exported[0].is_relative_to(global_root) is make_global


@pytest.mark.parametrize("edit", ["rule-tags", "rule-body-tags", "pipeline-tags"])
@pytest.mark.parametrize("tags", [[], ["custom"]])
def test_mcp_tag_edits_preserve_bundled_ownership(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    edit: str,
    tags: list[str],
) -> None:
    monkeypatch.setattr("gobby.utils.dev.is_dev_mode", lambda _path: False)
    rule_body = {"event": "before_tool", "effects": [{"type": "block", "reason": "policy"}]}
    kind = "pipeline" if edit == "pipeline-tags" else "rule"
    body = {"steps": [{"id": "check", "exec": "true"}]} if kind == "pipeline" else rule_body
    row = sync_imported_definition(temp_db, {"type": kind, "name": "bundled", **body}, None)
    if kind == "pipeline":
        pipeline_manager = PipelineDefinitionManager(temp_db)
        pipeline_manager.update(row.id, tags=["gobby", "existing"])
        result = update_pipeline_definition(
            pipeline_manager,
            PipelineLoader(db=temp_db),
            row.name,
            tags=tags,
            description="edited",
            project_path=tmp_path,
        )
        updated = pipeline_manager.get(row.id)
        assert updated.tags == [*tags, "gobby"]
        later = update_pipeline_definition(
            pipeline_manager,
            PipelineLoader(db=temp_db),
            row.name,
            description="later",
            project_path=tmp_path,
        )
        assert pipeline_manager.get(row.id).tags == updated.tags
        assert pipeline_manager.get(row.id).description == "later"
    else:
        rule_manager = RuleDefinitionManager(temp_db)
        rule_manager.update(row.id, tags=["gobby", "existing"])
        updates: dict[str, Any] = (
            {"definition": {**rule_body, "tags": tags}}
            if edit == "rule-body-tags"
            else {"tags": tags}
        )
        result = update_rule(
            rule_manager, row.name, description="edited", project_path=tmp_path, **updates
        )
        updated_rule = rule_manager.get(row.id)
        assert updated_rule.tags == [*tags, "gobby"]
        later = update_rule(rule_manager, row.name, description="later", project_path=tmp_path)
        assert rule_manager.get(row.id).tags == updated_rule.tags
        assert rule_manager.get(row.id).description == "later"
    assert result["success"] is True
    assert later["success"] is True
    assert list(tmp_path.rglob("*.yaml")) == []


def test_untagged_custom_pipeline_export_reimports(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gobby.utils.dev.is_dev_mode", lambda _path: False)
    manager = PipelineDefinitionManager(temp_db)
    row = manager.create(
        name="untagged", definition_json={"steps": [{"id": "check", "exec": "true"}]}
    )
    assert row.tags is None
    result = update_pipeline_definition(
        manager, PipelineLoader(db=temp_db), row.name, description="edited", project_path=tmp_path
    )
    assert result["success"] is True
    exported = tmp_path / ".gobby" / "workflows" / "pipelines" / "untagged.yaml"
    assert exported.exists()
    manager.hard_delete(row.id)
    imported = sync_imported_workflow_file(temp_db, exported, None)
    assert imported.description == "edited"
    assert imported.tags == []
