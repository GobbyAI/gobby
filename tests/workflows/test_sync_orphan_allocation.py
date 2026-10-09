"""Bundled orphan cleanup must not hydrate unrelated definition bodies."""

import gc
import tracemalloc
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import pytest
import yaml

from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.delivery_disposition import (
    apply_rule_delivery_dispositions,
    migrate_rule_delivery_dispositions,
)
from gobby.workflows.sync_pipelines import sync_bundled_pipelines
from gobby.workflows.sync_rules import sync_bundled_rules
from gobby.workflows.sync_variables import sync_bundled_variables

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", ["pipelines", "rules", "variables"])
def test_orphan_sync_allocation_ignores_unrelated_payload(
    temp_db: HubDatabase, tmp_path: Path, kind: str
) -> None:
    pipeline_manager = PipelineDefinitionManager(temp_db)
    rule_manager = RuleDefinitionManager(temp_db)
    variable_manager = SessionVariableDefaultManager(temp_db)

    def seed(name: str, size: int, managed: bool) -> str:
        source: Literal["installed", "custom"] = "installed" if managed else "custom"
        payload: dict[str, Any] = {"unrelated_blob": "x" * size}
        if kind == "pipelines":
            return pipeline_manager.create(name, payload, source=source, tags=["gobby"]).id
        if kind == "rules":
            payload["effect"] = {"type": "inject_context", "content": "x" * size}
            return rule_manager.create(name, payload, source=source, tags=["gobby"]).id
        return variable_manager.create(name, payload, source=source, tags=["gobby"]).id

    def sync() -> dict[str, Any]:
        if kind == "pipelines":
            with patch(
                "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=tmp_path
            ):
                return sync_bundled_pipelines(temp_db)
        if kind == "rules":
            return sync_bundled_rules(temp_db, rules_path=tmp_path)
        return sync_bundled_variables(temp_db, variables_path=tmp_path)

    template: dict[str, Any]
    if kind == "pipelines":
        template = {"name": "kept", "type": "pipeline", "steps": [{"id": "work", "exec": "true"}]}
    elif kind == "rules":
        template = {
            "rules": {
                "kept": {
                    "event": "before_tool",
                    "effect": {"type": "block", "reason": "allocation fixture"},
                }
            }
        }
    else:
        template = {"variables": {"kept": {"default": False}}}
    (tmp_path / "kept.yaml").write_text(yaml.safe_dump(template), encoding="utf-8")
    initial = sync()
    assert initial["errors"] == []
    assert initial["synced"] == 1

    managers: dict[
        str, PipelineDefinitionManager | RuleDefinitionManager | SessionVariableDefaultManager
    ] = {
        "pipelines": pipeline_manager,
        "rules": rule_manager,
        "variables": variable_manager,
    }
    manager = managers[kind]
    peaks: list[int] = []
    for size in (1024, 4 * 1024 * 1024):
        custom_id = seed(f"custom-{size}", size, False)
        orphan_id = seed(f"orphan-{size}", 1, True)
        gc.collect()
        tracemalloc.start()
        try:
            result = sync()
            peaks.append(tracemalloc.get_traced_memory()[1])
        finally:
            tracemalloc.stop()
        assert result["errors"] == []
        assert result["orphaned"] == 1
        assert manager.get(custom_id).source == "custom"
        assert manager.get(orphan_id, include_deleted=True).deleted_at is not None
        manager.hard_delete(custom_id)
        manager.hard_delete(orphan_id)

    print(f"{kind} orphan sync allocation: small={peaks[0]} large={peaks[1]}")
    assert peaks[1] < peaks[0] + 512 * 1024


@pytest.mark.parametrize(
    "definition",
    [
        {"effect": {"type": "block", "acknowledge_variable": "seen", "reason": "large"}},
        {
            "when": "not variables.get('seen')",
            "effects": [
                {"type": "inject_context", "content": "large"},
                {"type": "set_variable", "variable": "seen", "value": True},
            ],
        },
        {
            "when": "not variables.get('seen')",
            "effects": [
                {"type": "inject_context", "content": "large"},
                {"type": "set_variable", "variable": "seen", "when": "True"},
            ],
        },
        {"effects": [None, "scalar", {"type": "mcp_call", "success_variable": "seen"}]},
        {"effects": [], "effect": {"type": "block", "acknowledge_variable": "ignored"}},
        {"effects": None, "effect": {"type": "block", "acknowledge_variable": "seen"}},
        {"effect": {"type": "inject_context", "content": "large", "delivery": "on_receipt"}},
        {
            "when": "not variables.get('seen')",
            "effects": [
                {"type": "mcp_call", "inject_result": True},
                {"type": "set_variable", "variable": "seen", "value": True},
            ],
        },
    ],
)
def test_delivery_projection_preserves_classification_and_full_rewrite(
    temp_db: HubDatabase, definition: dict[str, Any]
) -> None:
    manager = RuleDefinitionManager(temp_db)
    definition = {**definition, "unrelated_blob": "x" * 4096}
    row = manager.create("projection", definition, source="custom", enabled=False)
    candidates = manager.list_delivery_disposition_candidates()
    assert len(candidates) == 1
    compact = candidates[0]
    assert "unrelated_blob" not in compact.definition_json
    expected = apply_rule_delivery_dispositions(row.name, definition)
    classified = apply_rule_delivery_dispositions(compact.name, compact.definition_json)
    assert classified.changed == expected.changed
    assert classified.errors == expected.errors

    result = migrate_rule_delivery_dispositions(temp_db)
    assert result["errors"] == list(expected.errors)
    assert result["success"] == (not expected.errors)
    assert result["updated"] == int(expected.changed and not expected.errors)
    stored = manager.get(row.id)
    assert stored.definition_json == expected.definition
    assert stored.enabled is False
    assert stored.source == "custom"
