"""Condition analysis shared by workflow dry-run checks."""

from __future__ import annotations

import ast
import json
from typing import Any

from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

_CONSTANT_NAMES = {"True", "true", "False", "false", "None", "none"}
_HELPER_NAMES = set(build_condition_helpers())


def analyze_condition(condition: str, runtime_names: set[str]) -> tuple[str | None, list[str]]:
    """Return a syntax error and names unavailable in the runtime context."""
    try:
        normalized = SafeExpressionEvaluator._normalize_expr(condition)
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as exc:
        return exc.msg, []

    bound_names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    loaded_names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    unknown_names = loaded_names - runtime_names - _HELPER_NAMES - _CONSTANT_NAMES - bound_names
    return None, sorted(unknown_names)


def runtime_resolution_mismatches(
    name: str, workflow_loader: Any, project_ref: str | None
) -> list[str]:
    """Describe loader features that the runtime definition lookup does not apply."""
    manager = getattr(workflow_loader, "def_manager", None)
    scope_resolver = getattr(workflow_loader, "_db_project_scope", None)
    if manager is None or not callable(scope_resolver):
        return []
    scope = scope_resolver(project_ref, label="workflow")
    row = manager.get_by_name(name, project_id=scope)
    raw_definition = getattr(row, "definition_json", None)
    if not isinstance(raw_definition, str):
        return []

    mismatches = []
    if getattr(row, "project_id", None) is not None:
        mismatches.append(
            "project-scoped override; runtime enforcement reads the global definition"
        )
    try:
        data = json.loads(raw_definition)
    except json.JSONDecodeError:
        return mismatches
    if isinstance(data, dict) and data.get("extends"):
        mismatches.append(
            "resolved extends inheritance; runtime enforcement reads the stored definition"
        )
    return mismatches
