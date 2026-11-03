"""Protected rules admit only the operator principal, never a seat."""

import pytest

from gobby.mcp_proxy.tools.workflows._rules import (
    create_rule,
    delete_rule,
    operator_rule_changes,
    toggle_rule,
    update_rule,
)
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

SECURITY_CLOSURE_RULES = (
    "closure-requires-security-policy-provenance",
    "landing-requires-security-policy-provenance",
    "closure-requires-security-approval",
    "landing-requires-security-approval",
)


@pytest.fixture
def bundled(temp_db: HubDatabase) -> RuleDefinitionManager:
    assert sync_bundled_rules(temp_db, get_bundled_rules_path())["errors"] == []
    return RuleDefinitionManager(temp_db)


def test_bundled_security_closure_rules_declare_protection(bundled: RuleDefinitionManager) -> None:
    for name in SECURITY_CLOSURE_RULES:
        row = bundled.get_by_name(name)
        assert row is not None, name
        assert row.definition_json["protected"] is True, name


@pytest.mark.parametrize("name", SECURITY_CLOSURE_RULES)
def test_seat_cannot_toggle_update_or_delete_protected_rule(
    bundled: RuleDefinitionManager, name: str
) -> None:
    toggled = toggle_rule(bundled, name, False)
    updated = update_rule(bundled, name, enabled=False)
    deleted = delete_rule(bundled, name, force=True)

    assert toggled["success"] is False
    assert "protected" in toggled["error"]
    assert updated["success"] is False
    assert "protected" in updated["error"]
    assert deleted["success"] is False
    assert "protected" in deleted["error"]
    row = bundled.get_by_name(name)
    assert row is not None
    assert row.enabled is True
    assert row.deleted_at is None


def test_operator_principal_still_changes_protected_rule(bundled: RuleDefinitionManager) -> None:
    name = "closure-requires-security-approval"

    with operator_rule_changes():
        assert toggle_rule(bundled, name, False)["success"] is True

    row = bundled.get_by_name(name)
    assert row is not None
    assert row.enabled is False


def test_unprotected_rule_remains_seat_toggleable(bundled: RuleDefinitionManager) -> None:
    created = create_rule(
        bundled,
        "ordinary-rule",
        {
            "event": "turn_start",
            "effects": [{"type": "set_variable", "variable": "ordinary", "value": 1}],
        },
    )
    assert created["success"] is True, created

    result = toggle_rule(bundled, "ordinary-rule", False)

    assert result["success"] is True, result
