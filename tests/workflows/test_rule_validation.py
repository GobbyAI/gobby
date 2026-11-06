"""Definition-independent rule validation at the persistence boundary."""

from pathlib import Path
from typing import Any

import pytest

from gobby.storage.definitions import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


def _definition(condition: str) -> dict[str, Any]:
    return {
        "event": "before_tool",
        "when": condition,
        "effects": [{"type": "block", "reason": "Synthetic decision"}],
    }


def test_invalid_condition_cannot_create_rule(temp_db: HubDatabase) -> None:
    manager = RuleDefinitionManager(temp_db)

    with pytest.raises(ValueError, match="when"):
        manager.create("synthetic-invalid", _definition("variables.get("))

    assert manager.get_by_name("synthetic-invalid") is None


def test_invalid_update_preserves_existing_rule(temp_db: HubDatabase) -> None:
    manager = RuleDefinitionManager(temp_db)
    original = _definition("variables.get('custom_toggle')")
    row = manager.create("synthetic-update", original)

    with pytest.raises(ValueError, match="when"):
        manager.update(row.id, definition_json=_definition("True and ("))

    assert manager.get(row.id).definition_json == original


def test_sync_repairs_deleted_invalid_definition_atomically(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    import json

    import yaml

    from gobby.workflows.sync_rules import sync_rule_file

    manager = RuleDefinitionManager(temp_db)
    row = manager.create(
        "synthetic-repair", _definition("True"), source="installed", tags=["gobby"]
    )
    manager.delete(row.id)
    temp_db.execute(
        "UPDATE rule_definitions SET definition_json = %s WHERE id = %s",
        (json.dumps(_definition("unsupported()")), row.id),
    )
    path = tmp_path / "repair.yaml"
    path.write_text(yaml.safe_dump({"rules": {"synthetic-repair": _definition("True")}}))
    result = sync_rule_file(temp_db, path, tag="gobby")
    assert result["errors"] == []
    repaired = manager.get(row.id)
    assert repaired.deleted_at is None
    assert repaired.definition_json["when"] == "True"


def test_unavailable_runtime_context_does_not_block_creation(temp_db: HubDatabase) -> None:
    manager = RuleDefinitionManager(temp_db)
    definition = _definition("custom_runtime_flag")

    row = manager.create("synthetic-runtime", definition)

    assert manager.get(row.id).definition_json == definition


@pytest.mark.parametrize("mutation", ["metadata", "sync", "toggle", "restore", "duplicate"])
def test_invalid_stored_rule_cannot_bypass_activation_validation(
    temp_db: HubDatabase, mutation: str
) -> None:
    import json

    manager = RuleDefinitionManager(temp_db)
    row = manager.create("synthetic-activation", _definition("True"), enabled=False)
    if mutation == "restore":
        manager.delete(row.id)
    broken = _definition("True or missing_function()")
    temp_db.execute(
        "UPDATE rule_definitions SET definition_json = %s WHERE id = %s",
        (json.dumps(broken), row.id),
    )
    before = manager.get(row.id, include_deleted=True)
    with pytest.raises(ValueError, match="when"):
        if mutation == "metadata":
            manager.update(row.id, description="Changed description")
        elif mutation == "sync":
            manager.update_from_sync(row.id, priority=123)
        elif mutation == "toggle":
            manager.toggle_enabled(row.id)
        elif mutation == "restore":
            manager.restore(row.id)
        else:
            manager.duplicate(row.id, "synthetic-copy")
    assert manager.get(row.id, include_deleted=True) == before
    assert manager.get_by_name("synthetic-copy") is None


@pytest.mark.parametrize(
    "condition",
    ["False and (lambda: True)()", "True or invented_helper()", "2 ** 3", "{}.pop('x')"],
)
def test_all_expression_branches_use_engine_grammar(condition: str) -> None:
    from gobby.workflows.rule_validation import validate_rule

    report = validate_rule(_definition(condition))
    assert not report.valid
    assert any(item.field == "when" for item in report.errors)


@pytest.mark.parametrize(
    "condition", ["has_durable_stop_wait(1)", "has_durable_stop_wait(unexpected=True)"]
)
def test_invalid_helper_arguments_are_errors(condition: str) -> None:
    from gobby.workflows.rule_validation import validate_rule

    report = validate_rule(_definition(condition))
    assert not report.valid
    assert report.errors[0].code == "evaluation"


def test_missing_context_is_distinct_from_evaluation_failure() -> None:
    from gobby.workflows.rule_validation import validate_rule

    missing = validate_rule(_definition("custom_runtime_flag"))
    failed = validate_rule(_definition("1 // 0"))
    assert missing.valid
    assert missing.warnings[0].code == "unresolved"
    assert not failed.valid
    assert failed.errors[0].code == "evaluation"


def test_dry_run_copies_state_and_prepares_external_effects() -> None:
    from gobby.workflows.rule_validation import validate_rule

    context: dict[str, Any] = {"variables": {"payload": "safe"}}
    definition = {
        "event": "before_tool",
        "effects": [
            {"type": "set_variable", "variable": "payload", "value": "changed"},
            {"type": "run_command", "command": ["must-never-execute"]},
            {"type": "mcp_call", "server": "unavailable", "tool": "mutate"},
            {"type": "proxy_hook", "handler": "unavailable"},
        ],
    }
    report = validate_rule(definition, context=context)
    assert report.valid
    assert context == {"variables": {"payload": "safe"}}
    assert [item.effect_type for item in report.trace] == [
        "set_variable",
        "run_command",
        "mcp_call",
        "proxy_hook",
    ]
    assert all(item.executed is False for item in report.trace)


def test_dry_run_never_evaluates_live_lazy_predicates() -> None:
    from unittest.mock import Mock

    from gobby.workflows.rule_validation import validate_rule
    from gobby.workflows.safe_evaluator import LazyBool

    query = Mock(side_effect=AssertionError("Live database query"))
    report = validate_rule(_definition("live_flag"), context={"live_flag": LazyBool(query)})
    query.assert_not_called()
    assert report.valid
    assert report.warnings


@pytest.mark.parametrize(
    "effect",
    [
        {"type": "block", "reason": "{{ broken( }}"},
        {"type": "block", "reason": "x", "recovery_when": "False or invented()"},
        {"type": "block", "reason": "x", "command_pattern": "["},
        {"type": "inject_context", "template": "{{ 'x' | invented_filter }}"},
    ],
)
def test_unmatched_effects_are_still_validated(effect: dict[str, Any]) -> None:
    from gobby.workflows.rule_validation import validate_rule

    report = validate_rule({"event": "before_tool", "when": "False", "effects": [effect]})
    assert not report.valid
    assert report.errors[0].field.startswith("effects.0.")
