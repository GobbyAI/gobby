"""Architecture boundary for PostgreSQL pool ownership."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_ROOT = REPO_ROOT / "src" / "gobby" / "cli"
CLI_COMPOSITION_ROOTS = {
    CLI_ROOT / "daemon.py",
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
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported = {alias.name for alias in node.names}
                forbidden = imported & POOL_CONSTRUCTORS
                if forbidden:
                    names = ", ".join(sorted(forbidden))
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {names}")

    assert not violations, "Operational layers acquired PostgreSQL pools:\n" + "\n".join(violations)


def test_removed_raw_runtime_database_api_has_no_callers() -> None:
    violations: list[str] = []
    for path in (REPO_ROOT / "src" / "gobby").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "open_runtime_hub_database":
                violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")

    assert not violations, "Removed raw database API remains in use:\n" + "\n".join(violations)
