"""Regression tests for generic HTTP 500 response details."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parents[3]
ROUTE_PATHS = (
    Path("src/gobby/servers/routes/agent_spawn.py"),
    Path("src/gobby/servers/routes/cron.py"),
    Path("src/gobby/servers/routes/mcp/endpoints"),
    Path("src/gobby/servers/routes/pipelines.py"),
    Path("src/gobby/servers/routes/sessions"),
    Path("src/gobby/servers/routes/tasks.py"),
)


def _route_files() -> list[Path]:
    files: list[Path] = []
    for relative_path in ROUTE_PATHS:
        path = REPO_ROOT / relative_path
        files.extend(path.rglob("*.py") if path.is_dir() else [path])
    return files


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((keyword.value for keyword in call.keywords if keyword.arg == name), None)


def test_in_scope_500_responses_use_generic_detail() -> None:
    violations: list[str] = []

    for path in _route_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if function_name != "HTTPException":
                continue
            status_code = _keyword(node, "status_code")
            if not isinstance(status_code, ast.Constant) or status_code.value != 500:
                continue
            detail = _keyword(node, "detail")
            if not isinstance(detail, ast.Constant) or detail.value != "Internal server error":
                violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")

    assert violations == []
