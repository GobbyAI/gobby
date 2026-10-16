"""Narrow collection proof for files declaring operator environment admission."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from gobby.config.validation_outcomes import runner_reported_failures
from gobby.tasks.command_equivalence import pytest_collection_target
from gobby.tasks.transcript_evidence_models import TranscriptValidationRun

_COLLECTED = re.compile(
    r"(?m)^[ \t]*(?:=+[ \t]+|Pytest:[ \t]*)?[1-9]\d* "
    r"(?:tests? collected|collected)(?: in \d+(?:\.\d+)?s)?[ \t=]*$"
)


def collection_target(run: TranscriptValidationRun) -> str | None:
    """Require successful completed collection, never an initial collected count."""
    if (
        run.wrapped
        or run.outcome != "success"
        or run.exit_code != 0
        or not _COLLECTED.search(run.output or "")
        or runner_reported_failures(run.output)
    ):
        return None
    return pytest_collection_target(run.core_command or run.command)


def declares_operator_guard(source: str) -> bool:
    """Recognize a module-wide pytestmark environment-grant skip condition.

    The declaration is ``pytestmark = [..., pytest.mark.skipif(
    os.environ.get("ENV_NAME") != GRANT, ...)]``, or that skipif alone.
    GRANT must be a nonempty literal string or an unambiguous module string
    constant. This matches the existing operator-proof declaration; arbitrary
    truthy skip conditions and observed skips never establish admission.
    """
    try:
        module = ast.parse(source)
    except SyntaxError:
        return False
    assignments: dict[str, list[ast.expr]] = {}
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments.setdefault(target.id, []).append(node.value)
    markers = assignments.get("pytestmark", [])
    if len(markers) != 1:
        return False
    marker = markers[0]
    values = marker.elts if isinstance(marker, (ast.List, ast.Tuple)) else [marker]
    for value in values:
        if not isinstance(value, ast.Call) or ast.unparse(value.func) != "pytest.mark.skipif":
            continue
        condition = (
            value.args[0]
            if value.args
            else next((kw.value for kw in value.keywords if kw.arg == "condition"), None)
        )
        if not (
            isinstance(condition, ast.Compare)
            and len(condition.ops) == len(condition.comparators) == 1
            and isinstance(condition.ops[0], ast.NotEq)
            and isinstance(condition.left, ast.Call)
            and ast.unparse(condition.left.func) == "os.environ.get"
            and len(condition.left.args) == 1
            and not condition.left.keywords
            and isinstance(condition.left.args[0], ast.Constant)
            and isinstance(condition.left.args[0].value, str)
            and condition.left.args[0].value
        ):
            continue
        grant = condition.comparators[0]
        if isinstance(grant, ast.Name):
            bindings = assignments.get(grant.id, [])
            if len(bindings) != 1:
                continue
            grant = bindings[0]
        if isinstance(grant, ast.Constant) and isinstance(grant.value, str) and grant.value:
            return True
    return False


def operator_guard_file(path: Path) -> bool:
    """Read only the declared source; importing a guarded test would execute it."""
    try:
        return declares_operator_guard(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return False
