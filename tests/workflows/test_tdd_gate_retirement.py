"""TDD remains development guidance without transcript-authorized write gates."""

from __future__ import annotations

import ast
from pathlib import Path

from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.safe_evaluator import build_condition_helpers
from gobby.workflows.sync_rules import sync_bundled_rules


def test_retired_tdd_rules_are_not_installed(temp_db: HubDatabase) -> None:
    manager = RuleDefinitionManager(temp_db)
    for name in ("enforce-tdd-block", "enforce-tdd-track-tests"):
        manager.create(
            name=name,
            source="installed",
            tags=["gobby", "tdd-enforcement"],
            definition_json={
                "event": "before_tool",
                "when": "True",
                "effects": [{"type": "block", "reason": "retired"}],
            },
        )
    result = sync_bundled_rules(temp_db)
    assert result["success"] is True, result
    assert manager.get_by_name("enforce-tdd-block") is None
    assert manager.get_by_name("enforce-tdd-track-tests") is None


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
