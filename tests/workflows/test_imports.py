"""Runtime synchronization coverage for imported workflow YAML files."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.mcp_proxy.tools.workflows._agents import _export_row, toggle_agent_definition
from gobby.mcp_proxy.tools.workflows._auto_export import auto_export_definition
from gobby.mcp_proxy.tools.workflows._import import reload_cache
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.imports import (
    sync_imported_definition,
    sync_imported_workflow_file,
    sync_imported_workflows,
)
from gobby.workflows.pipeline_loader import PipelineLoader
from gobby.workflows.pipeline_models import PipelineDefinition
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("remove_directory", [False, True], ids=["file", "directory"])
def test_deleted_project_pipeline_is_retired_without_touching_other_rows(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remove_directory: bool,
) -> None:
    project_path = tmp_path / "retirement"
    project = isolated_checkout_factory(temp_db, "retirement", root=project_path).project
    other = isolated_checkout_factory(temp_db, "other-retirement").project
    root = project_path / ".gobby" / "workflows"
    stale_path = root / "pipelines" / "retired.yaml"
    _write_pipeline(stale_path, "retired")
    _write_pipeline(root / "pipelines" / "present.yml", "present")
    monkeypatch.setattr(
        "gobby.workflows.imports.get_global_workflows_dir", lambda: tmp_path / "global"
    )
    manager = PipelineDefinitionManager(temp_db)
    payload = {"type": "pipeline", "steps": [{"id": "work", "exec": "true"}]}
    global_row = manager.create("retired", payload, tags=["gobby"])
    other_row = manager.create("retired", payload, project_id=other.id)
    custom = manager.create("custom", payload, project_id=project.id, source="custom")
    bundled = manager.create("bundled", payload, project_id=project.id, tags=["gobby"])
    first = sync_imported_workflows(temp_db, project_path=project_path, project_id=project.id)
    stale = manager.get_by_name("retired", project_id=project.id)
    present = manager.get_by_name("present", project_id=project.id)
    assert first["errors"] == []
    assert stale is not None and stale.project_id == project.id
    assert present is not None

    if remove_directory:
        shutil.rmtree(root)
    else:
        stale_path.unlink()
    result = sync_imported_workflows(temp_db, project_path=project_path, project_id=project.id)

    assert result["errors"] == []
    assert result["orphaned"] == (2 if remove_directory else 1)
    assert manager.get(stale.id, include_deleted=True).deleted_at is not None
    assert (
        manager.get(present.id, include_deleted=True).deleted_at is not None
    ) is remove_directory
    for row in (global_row, other_row, custom, bundled):
        assert manager.get(row.id).deleted_at is None
    assert (
        sync_imported_workflows(temp_db, project_path=project_path, project_id=project.id)[
            "orphaned"
        ]
        == 0
    )
    _write_pipeline(stale_path, "retired")
    reimported = sync_imported_workflows(temp_db, project_path=project_path, project_id=project.id)
    assert reimported["errors"] == []
    restored = manager.get_by_name("retired", project_id=project.id)
    assert restored is not None and restored.project_id == project.id
    assert restored.deleted_at is None


@pytest.mark.parametrize(
    "kind,definition,manager_type",
    [
        (
            "agent",
            {"prompts": {"agent": "Inspect."}, "workflows": {"rule_selectors": {"include": []}}},
            AgentDefinitionManager,
        ),
        (
            "rule",
            {"event": "before_tool", "effects": [{"type": "block", "reason": "stop"}]},
            RuleDefinitionManager,
        ),
        ("variable", {"value": 1}, SessionVariableDefaultManager),
    ],
)
def test_deleted_project_definition_is_retired(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    definition: dict[str, Any],
    manager_type: type[AgentDefinitionManager]
    | type[RuleDefinitionManager]
    | type[SessionVariableDefaultManager],
) -> None:
    project_path = tmp_path / "typed-retirement"
    project = isolated_checkout_factory(temp_db, "typed-retirement", root=project_path).project
    path = project_path / ".gobby" / "workflows" / f"{kind}s" / "retired.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({"name": "retired", "type": kind, **definition}))
    monkeypatch.setattr(
        "gobby.workflows.imports.get_global_workflows_dir", lambda: tmp_path / "global"
    )
    result = sync_imported_workflows(temp_db, project_path=project_path, project_id=project.id)
    manager = manager_type(temp_db)
    row = manager.get_by_name("retired", project_id=project.id)
    assert result["errors"] == []
    assert row is not None
    path.unlink()

    result = sync_imported_workflows(temp_db, project_path=project_path, project_id=project.id)

    assert result["errors"] == []
    assert result["orphaned"] == 1
    assert manager.get(row.id, include_deleted=True).deleted_at is not None


@pytest.mark.parametrize("failure", ["invalid", "unreadable", "missing-checkout"])
def test_incomplete_project_scan_does_not_retire_rows(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    project_path = tmp_path / "incomplete"
    project = isolated_checkout_factory(temp_db, "incomplete", root=project_path).project
    path = project_path / ".gobby" / "workflows" / "pipelines" / "retired.yaml"
    _write_pipeline(path, "retired")
    monkeypatch.setattr(
        "gobby.workflows.imports.get_global_workflows_dir", lambda: tmp_path / "global"
    )
    sync_imported_workflows(temp_db, project_path=project_path, project_id=project.id)
    manager = PipelineDefinitionManager(temp_db)
    row = manager.get_by_name("retired", project_id=project.id)
    assert row is not None
    path.unlink()
    if failure == "invalid":
        (path.parent / "broken.yaml").write_text("name: [")
    elif failure == "unreadable":
        original_iterdir = Path.iterdir

        def fail_scan(directory: Path) -> Iterator[Path]:
            if directory == path.parent:
                raise PermissionError("unreadable workflow directory")
            return original_iterdir(directory)

        monkeypatch.setattr(Path, "iterdir", fail_scan)
    else:
        project_path.rename(tmp_path / "unavailable")

    result = sync_imported_workflows(temp_db, project_path=project_path, project_id=project.id)

    assert result["orphaned"] == 0
    assert bool(result["errors"]) is (failure != "missing-checkout")
    assert manager.get(row.id).deleted_at is None


def test_project_multi_rule_scan_retires_only_removed_rules(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_path = tmp_path / "grouped-retirement"
    project = isolated_checkout_factory(temp_db, "grouped-retirement", root=project_path).project
    path = project_path / ".gobby" / "workflows" / "rules" / "policy.yaml"
    path.parent.mkdir(parents=True)
    rule = {"event": "before_tool", "effects": [{"type": "block", "reason": "stop"}]}
    path.write_text(yaml.safe_dump({"rules": {"retired": rule, "present": rule}}))
    monkeypatch.setattr(
        "gobby.workflows.imports.get_global_workflows_dir", lambda: tmp_path / "global"
    )
    initial = sync_imported_workflows(temp_db, project_path=project_path, project_id=project.id)
    manager = RuleDefinitionManager(temp_db)
    stale = manager.get_by_name("retired", project_id=project.id)
    present = manager.get_by_name("present", project_id=project.id)
    assert initial["errors"] == []
    assert stale is not None and present is not None
    path.write_text(yaml.safe_dump({"rules": {"present": rule}}))

    result = sync_imported_workflows(temp_db, project_path=project_path, project_id=project.id)

    assert result["errors"] == []
    assert result["orphaned"] == 1
    assert manager.get(stale.id, include_deleted=True).deleted_at is not None
    assert manager.get(present.id).deleted_at is None


def test_pipeline_yaml_tags_persist_on_import(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
) -> None:
    project = isolated_checkout_factory(temp_db, "pipeline-tags").project
    path = tmp_path / "tagged.yaml"
    payload: dict[str, Any] = {
        "name": "tagged-import",
        "type": "pipeline",
        "tags": ["runbook"],
        "steps": [{"id": "work", "exec": "echo done"}],
    }
    manager = PipelineDefinitionManager(temp_db)
    for tags in (["runbook"], ["release", "operations"], []):
        payload["tags"] = tags
        path.write_text(yaml.safe_dump(payload), encoding="utf-8")
        imported = sync_imported_workflow_file(temp_db, path, project.id)
        persisted = manager.get(imported.id)
        assert persisted.tags == tags
        assert persisted.project_id == project.id
        assert persisted.definition_json["tags"] == tags

    payload.pop("tags")
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    imported = sync_imported_workflow_file(temp_db, path, project.id)
    assert manager.get(imported.id).tags == []


@pytest.mark.parametrize("tags", [[""], ["   "], [1], "runbook"])
def test_pipeline_yaml_rejects_invalid_tags(tags: object) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PipelineDefinition.model_validate(
            {"name": "invalid-tags", "tags": tags, "steps": [{"id": "work", "exec": "true"}]}
        )


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
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_path = tmp_path / "project"
    project = isolated_checkout_factory(
        temp_db, "workflow-import-project", root=project_path
    ).project
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


@pytest.mark.parametrize(
    "definition,manager_type",
    [
        (
            {
                "type": "agent",
                "prompts": {"agent": "Inspect only."},
                "workflows": {"rule_selectors": {"include": []}},
            },
            AgentDefinitionManager,
        ),
        (
            {
                "type": "rule",
                "event": "before_tool",
                "effects": [{"type": "block", "reason": "stop"}],
            },
            RuleDefinitionManager,
        ),
        ({"type": "variable", "value": 1}, SessionVariableDefaultManager),
        (
            {"type": "pipeline", "steps": [{"id": "check", "exec": "true"}]},
            PipelineDefinitionManager,
        ),
    ],
    ids=["agent", "rule", "variable", "pipeline"],
)
def test_project_import_preserves_global_definition(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    definition: dict[str, Any],
    manager_type: type[AgentDefinitionManager]
    | type[RuleDefinitionManager]
    | type[SessionVariableDefaultManager]
    | type[PipelineDefinitionManager],
) -> None:
    project = isolated_checkout_factory(temp_db, "scoped-import").project
    payload = {**definition, "name": "shared-import", "description": "global"}
    global_row = sync_imported_definition(temp_db, payload, None)
    scoped = sync_imported_definition(temp_db, {**payload, "description": "project"}, project.id)
    manager = manager_type(temp_db)

    assert scoped.project_id == project.id
    assert scoped.id != global_row.id
    assert manager.get(global_row.id).description == "global"
    updated = sync_imported_definition(temp_db, {**payload, "description": "updated"}, project.id)
    assert updated.id == scoped.id
    assert manager.get(scoped.id).description == "updated"
    assert manager.get(global_row.id).description == "global"


def test_sync_imported_definition_writes_all_four_kinds(temp_db: HubDatabase) -> None:
    agent = sync_imported_definition(
        temp_db,
        {
            "name": "imported-agent",
            "type": "agent",
            "provider": "claude",
            "workflows": {"rule_selectors": {"include": []}},
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


def test_sync_imported_one_shot_rule_persists_on_receipt(temp_db: HubDatabase) -> None:
    row = sync_imported_definition(
        temp_db,
        {
            "name": "imported-once",
            "type": "rule",
            "event": "turn_start",
            "when": "not variables.get('shown')",
            "effects": [
                {"type": "inject_context", "template": "hello"},
                {"type": "set_variable", "variable": "shown", "value": True},
            ],
        },
        None,
    )
    stored = RuleDefinitionManager(temp_db).get(row.id)
    assert [effect.get("delivery", "eager") for effect in stored.definition_json["effects"]] == [
        "on_receipt",
        "on_receipt",
    ]


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
            "workflows": {"rule_selectors": {"include": []}},
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


def test_auto_exported_agent_reimports_every_field(temp_db: HubDatabase, tmp_path: Path) -> None:
    """The YAML an MCP agent write auto-exports restores the same body on import."""
    manager = AgentDefinitionManager(temp_db)
    row = manager.create(
        "exported-agent",
        {
            "name": "exported-agent",
            "description": "Round-trips through YAML",
            "version": "2.4.0",
            "provider": "claude",
            "prompts": {"agent": "Run the exported workflow."},
            "workflows": {"rule_selectors": {"include": ["tag:review"]}},
            "step_workflow": _STEP_WORKFLOW,
        },
    )
    original = AgentDefinitionBody.model_validate(manager.get(row.id).definition_json)

    exported = auto_export_definition(_export_row(manager.get(row.id)), tmp_path, kind="agent")
    assert exported is not None
    assert manager.hard_delete(row.id)

    sync_imported_workflow_file(temp_db, exported, None)

    restored = manager.get_by_name("exported-agent")
    assert restored is not None
    assert AgentDefinitionBody.model_validate(restored.definition_json) == original


@pytest.mark.parametrize("enabled", [False, True])
def test_mcp_toggled_agent_auto_exports_its_state(
    temp_db: HubDatabase, tmp_path: Path, enabled: bool
) -> None:
    """An MCP toggle reaches the auto-exported YAML and survives re-import."""
    manager = AgentDefinitionManager(temp_db)
    row = manager.create(
        "toggled-agent",
        {
            "name": "toggled-agent",
            "prompts": {"agent": "Run it."},
            "workflows": {"rule_selectors": {"include": []}},
        },
        enabled=not enabled,
    )
    assert toggle_agent_definition(manager, "toggled-agent", enabled)["success"] is True

    exported = auto_export_definition(_export_row(manager.get(row.id)), tmp_path, kind="agent")
    assert exported is not None
    assert yaml.safe_load(exported.read_text())["enabled"] is enabled
    assert manager.hard_delete(row.id)

    sync_imported_workflow_file(temp_db, exported, None)

    restored = manager.get_by_name("toggled-agent")
    assert restored is not None
    assert restored.enabled is enabled


_RULE_EFFECTS: list[dict[str, Any]] = [{"type": "inject_context", "template": "policy"}]


def test_multi_rule_project_file_imports_with_file_tags(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
) -> None:
    project = isolated_checkout_factory(temp_db, "multi-rule").project
    path = tmp_path / "policy.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "tags": ["default", "seat"],
                "rules": {
                    "policy-one": {"event": "turn_start", "effects": _RULE_EFFECTS},
                    "policy-two": {"event": "session_start", "effects": _RULE_EFFECTS},
                },
            }
        ),
        encoding="utf-8",
    )

    sync_imported_workflow_file(temp_db, path, project.id)

    manager = RuleDefinitionManager(temp_db)
    for name in ("policy-one", "policy-two"):
        row = manager.get_by_name(name, project_id=project.id)
        assert row is not None
        assert row.project_id == project.id
        assert {"default", "seat"} <= set(row.tags or [])


def test_single_definition_rule_keeps_tags(temp_db: HubDatabase) -> None:
    row = sync_imported_definition(
        temp_db,
        {
            "name": "tagged-rule",
            "type": "rule",
            "tags": ["default", "seat"],
            "event": "turn_start",
            "effects": _RULE_EFFECTS,
        },
        None,
    )

    assert RuleDefinitionManager(temp_db).get(row.id).tags == ["default", "seat"]


def test_shipped_project_operating_policy_imports(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
) -> None:
    project = isolated_checkout_factory(temp_db, "operating-policy").project
    policy = (
        Path(__file__).resolve().parents[2]
        / ".gobby"
        / "workflows"
        / "rules"
        / "project-operating-policy.yaml"
    )

    sync_imported_workflow_file(temp_db, policy, project.id)

    manager = RuleDefinitionManager(temp_db)
    for name in ("project-operating-policy", "reset-project-operating-policy-on-context-loss"):
        row = manager.get_by_name(name, project_id=project.id)
        assert row is not None
        assert row.enabled
        assert {"default", "seat"} <= set(row.tags or [])
