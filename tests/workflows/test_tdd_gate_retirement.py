"""TDD remains development guidance without transcript-authorized write gates."""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

from gobby.workflows.safe_evaluator import build_condition_helpers
from gobby.workflows.sync_rules import get_bundled_rules_path


def test_retained_tdd_metadata_defaults() -> None:
    variables_path = get_bundled_rules_path().parent / "variables" / "gobby-default-variables.yaml"
    variables = yaml.safe_load(variables_path.read_text())["variables"]

    assert variables["claimed_task_requires_tdd"]["value"] is False


def test_tdd_proof_functions_are_not_registered() -> None:
    assert not any("tdd" in name for name in build_condition_helpers())


def test_surviving_consumers_do_not_import_tdd_gates() -> None:
    root = Path(__file__).resolve().parents[2] / "src/gobby"
    imports: list[str] = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                if any(part.startswith("tdd_") for part in node.module.split(".")):
                    imports.append(f"{path.relative_to(root)}:{node.lineno} {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if any(part.startswith("tdd_") for part in alias.name.split(".")):
                        imports.append(f"{path.relative_to(root)}:{node.lineno} {alias.name}")
    assert imports == []
