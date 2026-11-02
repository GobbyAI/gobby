"""MCP rule authors cannot persist runtime-owned variable writers."""

from typing import Any

import pytest

from gobby.mcp_proxy.tools.workflows._rules import (
    create_rule,
    delete_rule,
    get_rule,
    toggle_rule,
    update_rule,
)
from gobby.storage.definitions.rules import RuleDefinitionManager, RuleDefinitionRow
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("variable", ["project_policy", "unlocked_tools", "_agent_srt_socket_root"])
@pytest.mark.parametrize("operation", ["create", "update-enabled", "update-disabled"])
def test_reserved_variable_writer_is_refused_before_persistence(
    temp_db: HubDatabase, variable: str, operation: str
) -> None:
    manager = RuleDefinitionManager(temp_db)
    original: dict[str, Any] = {
        "event": "turn_start",
        "effects": [{"type": "set_variable", "variable": "ordinary_value", "value": 1}],
    }
    definition: dict[str, Any] = {
        "event": "turn_start",
        "effects": [
            *original["effects"],
            {"type": "set_variable", "variable": variable, "value": {}},
        ],
    }
    name = "synthetic-writer"
    if operation == "create":
        result = create_rule(manager, name, definition)
        assert result["success"] is False
        assert manager.get_by_name(name) is None
    else:
        row = manager.create(name=name, definition_json=original, source="custom")
        result = update_rule(
            manager, name, definition=definition, enabled=operation == "update-enabled"
        )
        assert result["success"] is False
        unchanged = manager.get(row.id)
        assert unchanged is not None
        assert unchanged.definition_json == row.definition_json
        assert unchanged.enabled is True
    assert variable in result["error"]
    assert "reserved" in result["error"].lower()


def test_ordinary_variable_rule_can_be_created_and_updated(temp_db: HubDatabase) -> None:
    manager = RuleDefinitionManager(temp_db)
    body: dict[str, Any] = {
        "event": "turn_start",
        "effects": [{"type": "set_variable", "variable": "ordinary_value", "value": 1}],
    }
    assert create_rule(manager, "synthetic-ordinary", body)["success"] is True
    body["effects"][0]["value"] = 2
    assert update_rule(manager, "synthetic-ordinary", definition=body)["success"] is True
    stored = get_rule(manager, "synthetic-ordinary")
    assert stored["rule"]["effects"][0]["value"] == 2


@pytest.mark.parametrize("operation", ["replace", "disable", "toggle", "delete"])
def test_existing_reserved_writer_cannot_be_removed_by_mcp_mutation(
    temp_db: HubDatabase, operation: str
) -> None:
    manager = RuleDefinitionManager(temp_db)
    name = "synthetic-runtime-policy"
    body = {
        "event": "turn_start",
        "effects": [
            {
                "type": "set_variable",
                "variable": "project_policy",
                "value": {"security_paths": ["src/**"]},
            }
        ],
    }
    row = manager.create(name=name, definition_json=body, source="installed")
    if operation == "replace":
        result = update_rule(
            manager,
            name,
            definition={
                "event": "turn_start",
                "effects": [{"type": "set_variable", "variable": "ordinary_value", "value": 1}],
            },
        )
    elif operation == "disable":
        result = update_rule(manager, name, enabled=False)
    elif operation == "toggle":
        result = toggle_rule(manager, name, False)
    else:
        result = delete_rule(manager, name, force=True)
    assert result["success"] is False
    assert "reserved" in result["error"].lower()
    unchanged = manager.get(row.id)
    assert unchanged is not None
    assert unchanged.definition_json == row.definition_json
    assert unchanged.enabled is True
    assert unchanged.deleted_at is None


@pytest.mark.parametrize("operation", ["replace", "disable", "toggle", "delete"])
def test_rule_mutation_rechecks_writer_installed_after_initial_lookup(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    manager = RuleDefinitionManager(temp_db)
    name = "synthetic-raced-policy"
    ordinary = {
        "event": "turn_start",
        "effects": [{"type": "set_variable", "variable": "ordinary_value", "value": 1}],
    }
    protected = {
        "event": "turn_start",
        "effects": [
            {
                "type": "set_variable",
                "variable": "project_policy",
                "value": {"security_paths": ["src/**"]},
            }
        ],
    }
    row = manager.create(name=name, definition_json=ordinary, source="installed")
    lookup = manager.get_by_name

    def raced_lookup(ref: str, **kwargs: Any) -> RuleDefinitionRow | None:
        stale = lookup(ref, **kwargs)
        manager.update(row.id, definition_json=protected)
        return stale

    monkeypatch.setattr(manager, "get_by_name", raced_lookup)
    if operation == "replace":
        result = update_rule(manager, name, definition=ordinary)
    elif operation == "disable":
        result = update_rule(manager, name, enabled=False)
    elif operation == "toggle":
        result = toggle_rule(manager, name, False)
    else:
        result = delete_rule(manager, name, force=True)
    assert result["success"] is False
    assert "reserved" in result["error"].lower()
    monkeypatch.setattr(manager, "get_by_name", lookup)
    current = manager.get(row.id)
    assert current.enabled is True
    assert current.deleted_at is None
    assert get_rule(manager, name)["rule"]["effects"] == protected["effects"]


def test_create_reuses_deleted_name_without_destroying_tombstone(temp_db: HubDatabase) -> None:
    manager = RuleDefinitionManager(temp_db)
    body = {
        "event": "turn_start",
        "effects": [{"type": "set_variable", "variable": "ordinary", "value": 1}],
    }
    old = manager.create(name="synthetic-reused", definition_json=body, source="custom")
    manager.delete(old.id)
    assert create_rule(manager, old.name, body)["success"] is True
    assert manager.get(old.id, include_deleted=True).deleted_at is not None


@pytest.mark.parametrize("operation", ["update", "toggle", "delete"])
def test_disappeared_rule_returns_not_found(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    manager = RuleDefinitionManager(temp_db)
    body = {
        "event": "turn_start",
        "effects": [{"type": "set_variable", "variable": "ordinary", "value": 1}],
    }
    row = manager.create(name="synthetic-vanished", definition_json=body, source="custom")
    lookup = manager.get_by_name

    def raced_lookup(ref: str, **kwargs: Any) -> RuleDefinitionRow | None:
        stale = lookup(ref, **kwargs)
        manager.delete(row.id)
        return stale

    monkeypatch.setattr(manager, "get_by_name", raced_lookup)
    if operation == "update":
        result = update_rule(manager, row.name, enabled=False)
    elif operation == "toggle":
        result = toggle_rule(manager, row.name, False)
    else:
        result = delete_rule(manager, row.name, force=True)
    assert result["success"] is False
    assert "not found" in result["error"].lower()


@pytest.mark.parametrize("operation", ["create", "update"])
def test_mcp_result_cannot_write_reserved_variable(temp_db: HubDatabase, operation: str) -> None:
    manager = RuleDefinitionManager(temp_db)
    name = "synthetic-result-writer"
    body = {
        "event": "turn_start",
        "effects": [
            {
                "type": "mcp_call",
                "server": "synthetic",
                "tool": "read",
                "inject_result": True,
                "success_variable": "project_policy",
            }
        ],
    }
    if operation == "create":
        result = create_rule(manager, name, body)
    else:
        manager.create(
            name=name,
            definition_json={
                "event": "turn_start",
                "effects": [{"type": "set_variable", "variable": "ordinary", "value": 1}],
            },
        )
        result = update_rule(manager, name, definition=body)
    assert result["success"] is False
    assert "reserved" in result["error"].lower()


@pytest.mark.parametrize(
    "body",
    [{"not": "a-rule"}, {"event": "turn_start", "effect": {"type": "block", "reason": "legacy"}}],
)
@pytest.mark.parametrize("operation", ["disable", "delete"])
def test_invalid_or_legacy_rule_remains_disableable_and_deletable(
    temp_db: HubDatabase, body: dict[str, Any], operation: str
) -> None:
    manager = RuleDefinitionManager(temp_db)
    row = manager.create(name="synthetic-repairable", definition_json=body, source="custom")
    result = (
        toggle_rule(manager, row.name, False)
        if operation == "disable"
        else delete_rule(manager, row.name)
    )
    assert result["success"] is True
    current = manager.get(row.id, include_deleted=True)
    if operation == "disable":
        assert current.enabled is False
    else:
        assert current.deleted_at is not None
