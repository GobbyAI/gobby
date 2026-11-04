"""Protected rules admit only the operator principal, never a seat."""

import pytest

from gobby.mcp_proxy.tools.workflows._rules import (
    create_rule,
    delete_rule,
    toggle_rule,
    update_rule,
)
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.rule_authority import operator_rule_changes
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

PROTECTED_RULE = "synthetic-protected-rule"
_GATE = {
    "event": "task_close",
    "protected": True,
    "when": "reason == 'done'",
    "effects": [{"type": "block", "reason": "Synthetic protected gate."}],
}


@pytest.fixture
def manager(temp_db: HubDatabase) -> RuleDefinitionManager:
    manager = RuleDefinitionManager(temp_db)
    with operator_rule_changes():
        created = create_rule(manager, PROTECTED_RULE, _GATE)
    assert created["success"] is True, created
    return manager


def test_seat_cannot_author_a_protected_rule(temp_db: HubDatabase) -> None:
    manager = RuleDefinitionManager(temp_db)

    created = create_rule(manager, "seat-authored-rule", _GATE)

    assert created["success"] is False
    assert "cannot be marked protected" in created["error"]
    assert manager.get_by_name("seat-authored-rule") is None


def test_seat_cannot_protect_an_existing_rule(manager: RuleDefinitionManager) -> None:
    unprotected = {key: value for key, value in _GATE.items() if key != "protected"}
    assert create_rule(manager, "seat-ordinary-rule", unprotected)["success"] is True

    updated = update_rule(manager, "seat-ordinary-rule", definition=_GATE)

    assert updated["success"] is False
    assert "cannot be marked protected" in updated["error"]
    row = manager.get_by_name("seat-ordinary-rule")
    assert row is not None
    assert "protected" not in row.definition_json


def test_bundled_task_security_rules_declare_protection(temp_db: HubDatabase) -> None:
    assert sync_bundled_rules(temp_db, get_bundled_rules_path())["errors"] == []
    security_rules = [
        row
        for row in RuleDefinitionManager(temp_db).list_all()
        if "task-security" in (row.tags or [])
    ]

    assert security_rules
    for row in security_rules:
        assert row.definition_json.get("protected") is True, row.name


def test_seat_cannot_toggle_update_or_delete_protected_rule(
    manager: RuleDefinitionManager,
) -> None:
    toggled = toggle_rule(manager, PROTECTED_RULE, False)
    updated = update_rule(manager, PROTECTED_RULE, enabled=False)
    deleted = delete_rule(manager, PROTECTED_RULE, force=True)

    assert toggled["success"] is False
    assert "protected" in toggled["error"]
    assert updated["success"] is False
    assert "protected" in updated["error"]
    assert deleted["success"] is False
    assert "protected" in deleted["error"]
    row = manager.get_by_name(PROTECTED_RULE)
    assert row is not None
    assert row.enabled is True
    assert row.deleted_at is None
    assert row.definition_json["protected"] is True


def test_operator_principal_still_changes_protected_rule(manager: RuleDefinitionManager) -> None:
    with operator_rule_changes():
        assert toggle_rule(manager, PROTECTED_RULE, False)["success"] is True

    row = manager.get_by_name(PROTECTED_RULE)
    assert row is not None
    assert row.enabled is False


def test_unprotected_rule_remains_seat_toggleable(manager: RuleDefinitionManager) -> None:
    created = create_rule(
        manager,
        "ordinary-rule",
        {
            "event": "turn_start",
            "effects": [{"type": "set_variable", "variable": "ordinary", "value": 1}],
        },
    )
    assert created["success"] is True, created

    result = toggle_rule(manager, "ordinary-rule", False)

    assert result["success"] is True, result
