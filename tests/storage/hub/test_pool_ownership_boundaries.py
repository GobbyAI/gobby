"""Architecture boundary for PostgreSQL pool ownership."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_ROOT = REPO_ROOT / "src" / "gobby" / "cli"
CLI_COMPOSITION_ROOTS = {
    CLI_ROOT / "daemon.py",
    CLI_ROOT / "datastores.py",
    CLI_ROOT / "install.py",
    CLI_ROOT / "install_setup.py",
    CLI_ROOT / "runtime.py",
    CLI_ROOT / "utils_config.py",
}
INJECTED_MANAGER_FILES = {
    REPO_ROOT / "src" / "gobby" / "prompts" / "loader.py",
    REPO_ROOT / "src" / "gobby" / "storage" / "workflow_audit.py",
    REPO_ROOT / "src" / "gobby" / "utils" / "deps.py",
}
POOL_CONSTRUCTORS = {
    "PostgresHubDatabase",
    "open_runtime_hub_database",
    "runtime_hub_database",
}
pytestmark = pytest.mark.unit


def _attribute_parts(node: ast.expr) -> list[str]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return list(reversed(parts))


def _forbidden_pool_calls(tree: ast.AST) -> list[tuple[int, str]]:
    direct_aliases: dict[str, str] = {}
    module_aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound_name = alias.asname or alias.name
                if alias.name in POOL_CONSTRUCTORS:
                    direct_aliases[bound_name] = alias.name
                elif alias.name in {"postgres", "runtime"}:
                    module_aliases.add(bound_name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                module_aliases.add(alias.asname or alias.name.split(".", maxsplit=1)[0])

    calls: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        canonical_name: str | None = None
        if isinstance(node.func, ast.Name):
            canonical_name = direct_aliases.get(node.func.id)
            if canonical_name is None and node.func.id in POOL_CONSTRUCTORS:
                canonical_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            parts = _attribute_parts(node.func)
            if len(parts) >= 2 and parts[-1] in POOL_CONSTRUCTORS and parts[0] in module_aliases:
                canonical_name = parts[-1]

        if canonical_name is not None:
            calls.append((node.lineno, canonical_name))

    return calls


def _operational_files() -> list[Path]:
    files = []
    for path in CLI_ROOT.rglob("*.py"):
        if path in CLI_COMPOSITION_ROOTS:
            continue
        if path.name.startswith("_install_") or "installers" in path.parts:
            continue
        files.append(path)
    return [*files, *INJECTED_MANAGER_FILES]


def test_operational_layers_do_not_acquire_postgres_pools() -> None:
    violations: list[str] = []
    for path in _operational_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line_number, canonical_name in _forbidden_pool_calls(tree):
            violations.append(f"{path.relative_to(REPO_ROOT)}:{line_number}: {canonical_name}")

    assert not violations, "Operational layers acquired PostgreSQL pools:\n" + "\n".join(violations)


def test_forbidden_pool_calls_recognizes_alias_and_qualified_calls() -> None:
    tree = ast.parse(
        """
runtime_hub_database()
from gobby.storage.hub.postgres import PostgresHubDatabase as Pool
Pool("postgresql://example")
import gobby.storage.hub.runtime as hub_runtime
hub_runtime.runtime_hub_database()
import gobby.storage.hub.postgres
gobby.storage.hub.postgres.PostgresHubDatabase("postgresql://example")
"""
    )

    assert [name for _, name in _forbidden_pool_calls(tree)] == [
        "runtime_hub_database",
        "PostgresHubDatabase",
        "runtime_hub_database",
        "PostgresHubDatabase",
    ]


def test_removed_raw_runtime_database_api_has_no_callers() -> None:
    violations: list[str] = []
    for path in (REPO_ROOT / "src" / "gobby").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "open_runtime_hub_database":
                violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")

    assert not violations, "Removed raw database API remains in use:\n" + "\n".join(violations)
