"""Agent-workflow evaluator call sites build their expressions from the shared allowlist."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = Path(__file__).parents[2]
AGENT_WORKFLOW_EVALUATOR_PATHS = (
    PROJECT_ROOT / "src/gobby/mcp_proxy/tools/spawn_agent/_step_state.py",
    PROJECT_ROOT / "src/gobby/workflows/engine/enforcement_handlers.py",
    PROJECT_ROOT / "src/gobby/workflows/handler_route_lint.py",
)


@pytest.mark.parametrize("path", AGENT_WORKFLOW_EVALUATOR_PATHS, ids=lambda path: path.name)
def test_agent_workflow_evaluator_sites_use_shared_allowlist(path: Path) -> None:
    tree = ast.parse(path.read_text())
    evaluator_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SafeExpressionEvaluator"
    ]

    assert evaluator_calls
    for call in evaluator_calls:
        keyword = next((item for item in call.keywords if item.arg == "allowed_funcs"), None)
        allowed_funcs = keyword.value if keyword is not None else call.args[1]
        assert isinstance(allowed_funcs, ast.Call)
        assert isinstance(allowed_funcs.func, ast.Name)
        assert allowed_funcs.func.id == "build_agent_workflow_allowed_funcs"
