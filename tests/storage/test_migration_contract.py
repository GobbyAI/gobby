from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import MigrationUnsupportedError

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "gobby"
MIGRATIONS_SOURCE = SRC_ROOT / "storage" / "migrations.py"
MIGRATION_HELPERS_MODULE = "gobby.storage.migration_helpers"
ALLOWED_FTS5_HELPER_IMPORTS = {
    Path("code_index/storage.py"),
    Path("memory/fts_search.py"),
    Path("search/fts5.py"),
    Path("skills/search.py"),
}


def _storage_module_name(path: Path) -> str:
    relative = path.relative_to(SRC_ROOT).with_suffix("")
    return "gobby." + ".".join(relative.parts)


def _resolved_import_from_module(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""

    package_parts = _storage_module_name(path).split(".")[:-1]
    base_parts = package_parts[: len(package_parts) - node.level + 1]
    if node.module:
        base_parts.extend(node.module.split("."))
    return ".".join(base_parts)


def _imports_migration_helpers(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == MIGRATION_HELPERS_MODULE:
                    lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            module = _resolved_import_from_module(path, node)
            if module == MIGRATION_HELPERS_MODULE:
                lines.append(node.lineno)
            elif module == "gobby.storage" and any(
                alias.name == "migration_helpers" for alias in node.names
            ):
                lines.append(node.lineno)

    return lines


def _function_line_span(path: Path, name: str) -> range:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            assert node.end_lineno is not None
            return range(node.lineno, node.end_lineno + 1)
    raise AssertionError(f"{name} not found in {path}")


def test_legacy_migrations_list_is_empty_in_source_and_runtime() -> None:
    import gobby.storage.migrations as module

    tree = ast.parse(MIGRATIONS_SOURCE.read_text(encoding="utf-8"))
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "MIGRATIONS"
    ]

    assert module.MIGRATIONS == []
    assert len(assignments) == 1
    assert isinstance(assignments[0].value, ast.List)
    assert assignments[0].value.elts == []


def test_legacy_migrations_guard_rejects_callable_and_string_entries(monkeypatch) -> None:
    import gobby.storage.migrations as module

    def legacy_callable(_db: LocalDatabase) -> None:
        raise AssertionError("legacy callable should not run")

    monkeypatch.setattr(
        module,
        "MIGRATIONS",
        [
            (261, "python callable", legacy_callable),
            (262, "sql string", "SELECT 1"),
        ],
    )

    with pytest.raises(MigrationUnsupportedError) as exc_info:
        module.latest_known_version()

    message = str(exc_info.value)
    assert "file-based migration cutover" in message
    assert "action=callable" in message
    assert "action=str" in message


def test_postgres_runner_rejects_legacy_migrations_before_opening_transaction(
    monkeypatch,
) -> None:
    import gobby.storage.migrations as module

    def legacy_callable(_db: LocalDatabase) -> None:
        raise AssertionError("legacy callable should not run")

    class PostgresHub:
        dialect = "postgres"

        def transaction(self) -> Any:
            raise AssertionError("legacy MIGRATIONS must fail before Postgres transactions")

    monkeypatch.setattr(module, "MIGRATIONS", [(261, "python callable", legacy_callable)])

    with pytest.raises(MigrationUnsupportedError, match="MIGRATIONS must remain empty"):
        module.MigrationRunner(PostgresHub()).apply_pending()


def test_migration_helpers_are_not_imported_by_postgres_storage_paths() -> None:
    baseline_span = _function_line_span(MIGRATIONS_SOURCE, "_apply_baseline")
    violations: list[str] = []

    for path in SRC_ROOT.rglob("*.py"):
        relative = path.relative_to(SRC_ROOT)
        if relative == Path("storage/migration_helpers.py"):
            continue

        import_lines = _imports_migration_helpers(path)
        if not import_lines:
            continue

        if relative == Path("storage/migrations.py"):
            disallowed_lines = [line for line in import_lines if line not in baseline_span]
        elif relative in ALLOWED_FTS5_HELPER_IMPORTS:
            disallowed_lines = []
        else:
            disallowed_lines = import_lines

        violations.extend(f"{relative}:{line}" for line in disallowed_lines)

    assert violations == []
