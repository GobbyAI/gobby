"""Installed definitions select shared-checkout guards independently of their names."""

import json

import pytest

from gobby.hooks.session_activation import (
    _resolve_active_rule_names,
    clear_active_rule_names_cache,
)
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "rule_name",
    ["block-dirty-path-approval-cli", "block-cross-session-foreign-staged-commit"],
)
@pytest.mark.parametrize(
    ("definition_name", "exempt"),
    [("orchestrator", True), ("ordinary-seat", False), ("custom-controller", True)],
)
def test_installed_definition_controls_checkout_guard_exemption(
    temp_db: HubDatabase, rule_name: str, definition_name: str, exempt: bool
) -> None:
    """Exercise declared exclusions on installed fixture definitions."""
    clear_active_rule_names_cache()
    try:
        AgentDefinitionManager(temp_db).create(
            name=definition_name,
            source="installed",
            definition_json=json.dumps(
                {
                    "name": definition_name,
                    "prompts": {"agent": "Handle the assigned work."},
                    "workflows": {
                        "rule_selectors": {
                            "include": ["tag:default"],
                            "exclude": [f"name:{rule_name}"] if exempt else [],
                        }
                    },
                }
            ),
        )
        sync_bundled_rules(temp_db, get_bundled_rules_path())

        active_rules = _resolve_active_rule_names(temp_db, definition_name, None)

        assert active_rules is not None
        assert (rule_name in active_rules) is not exempt
    finally:
        clear_active_rule_names_cache()
