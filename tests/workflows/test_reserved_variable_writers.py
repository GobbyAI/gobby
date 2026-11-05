"""Reserved workflow variables: authoring refusals, defaults seeding and writer kinds."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.workflows._variables import create_variable, update_variable
from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.idle_declaration import SET_VARIABLE_TOOL, native_variable_write_error
from gobby.workflows.evaluation_context import freeze_evaluation_value, refresh_evaluation_variables
from gobby.workflows.reserved_variables import (
    RuleVariableAliasError,
    RuleVariableWriter,
    rule_variable_is_writable,
    write_rule_variable,
)
from gobby.workflows.variable_defaults import merge_unloaded_variable_defaults

pytestmark = pytest.mark.unit

RESERVED = "_agent_type"
RESERVED_PREFIX = "enforce_stop_gate"
PROJECT_POLICY = "project_policy"
INTERNAL_RUNTIME_VARIABLE = "unlocked_tools"


def _rule(source: str = "installed", tags: list[str] | None = None) -> RuleDefinitionRow:
    now = datetime(2026, 11, 2, tzinfo=UTC)
    return RuleDefinitionRow(
        id="rule-under-test",
        name="rule-under-test",
        enabled=True,
        enabled_pinned=False,
        priority=100,
        definition_json={},
        source=cast("Any", source),
        created_at=now,
        updated_at=now,
        tags=tags,
    )


def _writable(name: str, row: RuleDefinitionRow, writer: str) -> bool:
    return rule_variable_is_writable(name, row, writer=cast("RuleVariableWriter", writer))


def _definition_manager() -> MagicMock:
    manager = MagicMock()
    manager.get_by_name.return_value = None
    return manager


@pytest.mark.parametrize("name", [RESERVED, RESERVED_PREFIX, PROJECT_POLICY])
def test_create_variable_refuses_reserved_names(name: str) -> None:
    manager = _definition_manager()

    result = create_variable(manager, name, "authored")

    assert result["success"] is False
    assert "reserved" in result["error"]
    manager.create.assert_not_called()


@pytest.mark.parametrize("name", [RESERVED, RESERVED_PREFIX, PROJECT_POLICY])
def test_update_variable_refuses_reserved_names(name: str) -> None:
    manager = _definition_manager()
    manager.get_by_name.return_value = MagicMock(id="existing-row")

    result = update_variable(manager, name, "rewritten")

    assert result["success"] is False
    assert "reserved" in result["error"]
    manager.update.assert_not_called()


def test_defaults_merge_never_seeds_reserved_names(temp_db: HubDatabase) -> None:
    defaults = SessionVariableDefaultManager(temp_db)
    defaults.create(RESERVED, "synthetic-seat")
    defaults.create(RESERVED_PREFIX, True)
    defaults.create("plain_default", "kept")

    merged = merge_unloaded_variable_defaults(temp_db, "no-such-session", {})

    assert merged["plain_default"] == "kept"
    assert RESERVED not in merged
    assert RESERVED_PREFIX not in merged


@pytest.mark.parametrize("writer", ["success_variable", "acknowledge_variable", "runtime"])
def test_installed_rows_cannot_write_project_policy_except_by_set_variable(writer: str) -> None:
    assert _writable(PROJECT_POLICY, _rule(tags=["user"]), writer) is False


def test_installed_set_variable_effect_may_write_project_policy() -> None:
    assert _writable(PROJECT_POLICY, _rule(tags=["user"]), "set_variable") is True


def test_custom_row_cannot_write_project_policy_even_by_set_variable() -> None:
    assert _writable(PROJECT_POLICY, _rule(source="custom", tags=["user"]), "set_variable") is False


def test_refused_project_policy_write_leaves_variables_unchanged() -> None:
    variables: dict[str, Any] = {PROJECT_POLICY: {"security_paths": ["safe"]}}

    written = write_rule_variable(
        variables,
        PROJECT_POLICY,
        {"security_paths": []},
        row=_rule(tags=["user"]),
        writer="success_variable",
    )

    assert written is False
    assert variables == {PROJECT_POLICY: {"security_paths": ["safe"]}}


@pytest.mark.parametrize("writer", ["set_variable", "success_variable", "acknowledge_variable"])
def test_gobby_internal_rules_keep_runtime_write_authority(writer: str) -> None:
    assert _writable(INTERNAL_RUNTIME_VARIABLE, _rule(tags=["gobby"]), writer) is True


def test_native_reserved_refusal_keeps_set_variable_recovery_guidance() -> None:
    error = native_variable_write_error(SET_VARIABLE_TOOL, {"name": RESERVED}, None, {}, None)

    assert error is not None
    assert RESERVED in error
    assert "set_variable remains available for other permitted variables" in error


def _refreshed_context(variables: dict[str, Any], sources: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {"variables": {}}
    refresh_evaluation_variables(context, variables, sources, set())
    return context


def test_rewriting_a_mutated_container_in_place_is_refused() -> None:
    items = ["a"]
    variables: dict[str, Any] = {"items": items}
    sources: dict[str, Any] = {}
    _refreshed_context(variables, sources)
    items.append("b")

    with pytest.raises(RuleVariableAliasError):
        write_rule_variable(
            variables,
            "items",
            items,
            row=_rule(tags=["user"]),
            writer="set_variable",
        )


def test_append_then_replacement_write_refreshes_rule_context() -> None:
    items = ["a"]
    variables: dict[str, Any] = {"items": items}
    sources: dict[str, Any] = {}
    context = _refreshed_context(variables, sources)
    items.append("b")

    written = write_rule_variable(
        variables,
        "items",
        list(items),
        row=_rule(tags=["user"]),
        writer="set_variable",
    )
    refresh_evaluation_variables(context, variables, sources, set())

    assert written is True
    assert context["variables"]["items"] == freeze_evaluation_value(["a", "b"])


def test_rewriting_an_immutable_value_with_itself_is_admitted() -> None:
    variables: dict[str, Any] = {"ready": True}

    written = write_rule_variable(
        variables,
        "ready",
        True,
        row=_rule(tags=["user"]),
        writer="acknowledge_variable",
    )

    assert written is True
    assert variables == {"ready": True}
