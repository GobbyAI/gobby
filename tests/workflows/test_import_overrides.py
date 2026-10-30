"""Imported files must declare their intent before shadowing bundled definitions."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.imports import sync_imported_definition, sync_imported_workflow_file
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "definition,manager_type",
    [
        (
            {
                "type": "agent",
                "prompts": {"agent": "Bundled seat."},
                "workflows": {"rule_selectors": {"include": []}},
            },
            AgentDefinitionManager,
        ),
        (
            {
                "type": "rule",
                "event": "before_tool",
                "effects": [{"type": "block", "reason": "bundled policy"}],
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
@pytest.mark.parametrize("project_scoped", [False, True])
@pytest.mark.parametrize("override", [None, False, "true", True])
def test_import_requires_explicit_bundled_override(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    definition: dict[str, Any],
    manager_type: type[AgentDefinitionManager]
    | type[RuleDefinitionManager]
    | type[SessionVariableDefaultManager]
    | type[PipelineDefinitionManager],
    project_scoped: bool,
    override: object,
) -> None:
    payload = {**definition, "name": "bundled-copy", "description": "bundled"}
    row = sync_imported_definition(temp_db, payload, None)
    manager = manager_type(temp_db)
    bundled = manager.update(row.id, tags=["gobby"])
    project_id = (
        isolated_checkout_factory(temp_db, "override-project").project.id
        if project_scoped
        else None
    )
    copied = {**payload, "description": "user copy"}
    if override is not None:
        copied["override"] = override

    if override is True:
        imported = sync_imported_definition(temp_db, copied, project_id)
        assert imported.description == "user copy"
        assert imported.project_id == project_id
        if project_scoped:
            assert manager.get(row.id) == bundled
        with pytest.raises(ValueError, match="override: true"):
            sync_imported_definition(temp_db, {**payload, "description": "later copy"}, project_id)
        assert manager.get(imported.id) == imported
        return

    with pytest.raises(ValueError, match="override: true"):
        sync_imported_definition(temp_db, copied, project_id)

    assert manager.get(row.id) == bundled
    assert manager.get_by_name("bundled-copy", project_id=project_id) == bundled


@pytest.mark.parametrize("project_scoped", [False, True])
def test_grouped_rule_collision_is_refused_before_any_write(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    project_scoped: bool,
) -> None:
    definition = {
        "event": "before_tool",
        "effects": [{"type": "block", "reason": "bundled policy"}],
    }
    row = sync_imported_definition(
        temp_db, {**definition, "type": "rule", "name": "bundled-rule"}, None
    )
    manager = RuleDefinitionManager(temp_db)
    bundled = manager.update(row.id, tags=["gobby"])
    project_id = (
        isolated_checkout_factory(temp_db, "grouped-project").project.id if project_scoped else None
    )
    path = tmp_path / "rules.yaml"
    path.write_text(
        yaml.safe_dump({"rules": {"a-new-rule": definition, "bundled-rule": definition}})
    )
    on_disk: set[tuple[str, str]] = set()

    with pytest.raises(ValueError, match="override: true"):
        sync_imported_workflow_file(temp_db, path, project_id, on_disk=on_disk)

    assert manager.get(row.id) == bundled
    assert manager.get_by_name("a-new-rule", project_id=project_id) is None
    assert on_disk == set()


@pytest.mark.parametrize("project_scoped", [False, True])
@pytest.mark.parametrize("file_override", [False, True])
def test_grouped_rule_explicit_override_updates_without_losing_bundled_ownership(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    project_scoped: bool,
    file_override: bool,
) -> None:
    definition = {
        "event": "before_tool",
        "effects": [{"type": "block", "reason": "bundled policy"}],
    }
    row = sync_imported_definition(
        temp_db, {**definition, "type": "rule", "name": "bundled-rule"}, None
    )
    manager = RuleDefinitionManager(temp_db)
    bundled = manager.update(row.id, tags=["gobby"])
    project_id = (
        isolated_checkout_factory(temp_db, "grouped-override").project.id
        if project_scoped
        else None
    )
    override_rule: dict[str, Any] = {**definition, "description": "explicit override"}
    payload: dict[str, Any] = {"rules": {"bundled-rule": override_rule, "new-rule": definition}}
    if file_override:
        payload["override"] = True
    else:
        override_rule["override"] = True
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(payload))
    on_disk: set[tuple[str, str]] = set()

    result = sync_imported_workflow_file(temp_db, path, project_id, on_disk=on_disk)

    assert result["errors"] == []
    imported = manager.get_by_name("bundled-rule", project_id=project_id)
    assert imported is not None
    assert imported.project_id == project_id
    assert imported.description == "explicit override"
    assert imported.definition_json["effects"][0]["reason"] == "bundled policy"
    assert manager.get_by_name("new-rule", project_id=project_id) is not None
    assert on_disk == {("rule", "bundled-rule"), ("rule", "new-rule")}
    if project_scoped:
        assert manager.get(row.id) == bundled
        assert imported.id != row.id
    else:
        assert imported.id == row.id
        assert "gobby" in (imported.tags or [])

    # A later unlabelled copy must still be refused after the explicit update.
    path.write_text(yaml.safe_dump({"rules": {"bundled-rule": definition}}))
    with pytest.raises(ValueError, match="override: true"):
        sync_imported_workflow_file(temp_db, path, project_id)
    assert manager.get(imported.id) == imported
