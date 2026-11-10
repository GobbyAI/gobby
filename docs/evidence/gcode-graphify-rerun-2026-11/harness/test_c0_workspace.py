"""The C0 probe reads its registered manifest workspace; the sealed corpus is never a project."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from provision_environment import C0_WORKSPACE, project_entries

HARNESS = Path(__file__).resolve().parent
SEALED_C0 = "corpora/gcode/C0"


def _c0_probe() -> dict[str, Any]:
    (entry,) = [project for project in project_entries() if project["case"] == "C0"]
    return entry


def _spelled_path(node: ast.expr) -> str | None:
    """The literal path an expression spells: a string, or literal parts joined with `/`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left, right = _spelled_path(node.left), _spelled_path(node.right)
        if left is not None and right is not None:
            return f"{left}/{right}"
    return None


def test_the_c0_workspace_constant_is_the_registered_manifest_workspace() -> None:
    assert C0_WORKSPACE == _c0_probe()["workspace"]


def test_the_c0_consumers_name_the_registered_workspace_constant() -> None:
    for module in ("c0_runner.py", "c0.py", "validate_environment.py"):
        tree = ast.parse((HARNESS / module).read_text(), filename=module)
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert "C0_WORKSPACE" in names, module


def test_no_harness_module_names_the_sealed_c0_corpus_as_a_project() -> None:
    offenders = []
    for path in sorted(HARNESS.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if not isinstance(node, ast.expr):
                continue
            spelled = _spelled_path(node)
            if spelled is not None and spelled.startswith(SEALED_C0):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []
