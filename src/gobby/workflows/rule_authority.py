"""Operator-only authority over protected rules, shared by every rule write path.

A protected rule changes only through bundled template sync or an authenticated
operator call. Seat MCP calls and file imports (user or project YAML) can neither
change, shadow nor author one.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from gobby.storage.definitions.rules import (
    RuleDefinitionManager,
    RuleDefinitionRow,
    rule_definition_protected,
)

_operator_rule_changes: ContextVar[bool] = ContextVar("operator_rule_changes", default=False)


class ProtectedRuleError(ValueError):
    """A non-operator write attempted to change or author a protected rule."""


@contextmanager
def operator_rule_changes() -> Iterator[None]:
    """Used only after HTTP authentication proves an operator principal."""
    token = _operator_rule_changes.set(True)
    try:
        yield
    finally:
        _operator_rule_changes.reset(token)


def is_operator_rule_change() -> bool:
    """Whether the current call runs under an authenticated operator principal."""
    return _operator_rule_changes.get()


def protected_rule_error(name: str, definition_json: Any) -> str | None:
    """Refuse a non-operator change to an existing protected rule."""
    if is_operator_rule_change() or not rule_definition_protected(definition_json):
        return None
    return f"Rule '{name}' is protected; only the operator can change it"


def protected_flag_error(name: str, definition: dict[str, Any]) -> str | None:
    """Refuse a non-operator write that would mark a rule protected."""
    if is_operator_rule_change() or definition.get("protected") is not True:
        return None
    return f"Rule '{name}' cannot be marked protected; only the operator can protect a rule"


def refuse_protected_rule_import(
    manager: RuleDefinitionManager,
    name: str,
    target: RuleDefinitionRow | None,
    definition: dict[str, Any],
) -> None:
    """A rule file import may not protect a rule, change one, or shadow a global one.

    ``target`` is the row the import writes, if any; the global row of the same name
    is checked as well, since a project row would displace it.
    """
    error = protected_flag_error(name, definition)
    for row in (target, manager.get_by_name(name)):
        if error is None and row is not None:
            error = protected_rule_error(name, row.definition_json)
    if error is not None:
        raise ProtectedRuleError(error)
