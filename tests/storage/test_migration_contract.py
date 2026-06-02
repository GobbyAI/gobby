from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "gobby"
MIGRATIONS_SOURCE = SRC_ROOT / "storage" / "migrations.py"
MIGRATION_HELPERS_MODULE = "gobby.storage.migration_helpers"


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


def test_legacy_migration_api_is_absent_from_source_and_runtime() -> None:
    import gobby.storage.migrations as module

    tree = ast.parse(MIGRATIONS_SOURCE.read_text(encoding="utf-8"))
    names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.ClassDef | ast.AsyncFunctionDef)
    }
    assignments = {
        node.target.id
        for node in tree.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }

    for removed in {
        "MIGRATIONS",
        "MigrationAction",
        "get_current_version",
        "migrations_needed",
        "run_migrations",
        "_run_migration_list",
        "_migrate_bookkeeping_table",
        "migrate_neo4j_config_to_falkordb",
    }:
        assert not hasattr(module, removed)
        assert removed not in names
        assert removed not in assignments


def test_only_current_postgres_sql_migrations_exist_after_flattening() -> None:
    migrations_dir = SRC_ROOT / "storage" / "migrations"

    assert sorted(path.name for path in migrations_dir.glob("*.sql")) == [
        "261_implementation_domain.sql",
        "262_neo4j_config_to_falkordb.sql",
        "264_drop_migration_state.sql",
        "265_build_runs_project_root_started.sql",
        "266_agent_run_resume_metadata.sql",
        "267_context_usage_snapshot.sql",
        "268_prevent_self_parent_sessions.sql",
        "269_context_usage_ratio_range.sql",
        "270_context_usage_value_constraints.sql",
        "271_embeddings_namespace_to_ai_embeddings.sql",
        "272_drop_embedding_provider_config.sql",
        "273_task_merge_status.sql",
    ]


def test_implementation_domain_migration_adds_column_and_backfills_open_code_tasks() -> None:
    migration = (SRC_ROOT / "storage" / "migrations" / "261_implementation_domain.sql").read_text(
        encoding="utf-8"
    )

    assert "ADD COLUMN IF NOT EXISTS implementation_domain" in migration
    assert "category = 'code'" in migration
    assert "closed_at IS NULL" in migration
    assert "assigned_agent = 'frontend-developer'" in migration
    assert "assigned_agent = 'fullstack-developer'" in migration
    assert "ELSE 'backend'" in migration


def test_neo4j_config_migration_preserves_tunables_and_uses_json_secret_guard() -> None:
    migration = (
        SRC_ROOT / "storage" / "migrations" / "262_neo4j_config_to_falkordb.sql"
    ).read_text(encoding="utf-8")

    assert "databases.neo4j.graph_search" in migration
    assert "databases.neo4j.graph_min_score" in migration
    assert "databases.neo4j.rrf_k" in migration
    assert "databases.neo4j.graph_name" in migration
    assert "databases.falkordb." in migration
    assert "ON CONFLICT (key) DO NOTHING" in migration
    assert "DELETE FROM config_store" in migration
    assert "WHERE key LIKE 'databases.neo4j.%'" in migration
    assert "to_json('$secret:auth'::text)::text" in migration


def test_embedding_namespace_migration_uses_namespaced_secret_reference() -> None:
    migration = (
        SRC_ROOT / "storage" / "migrations" / "271_embeddings_namespace_to_ai_embeddings.sql"
    ).read_text(encoding="utf-8")

    assert "ai.embeddings.api_key" in migration
    assert "embeddings_api_key" in migration
    assert """'"$secret:embeddings_api_key"'""" in migration
    assert "to_json('$secret:embeddings_api_key'::text)::text" not in migration
    assert "'secret-' || md5(source_secret.name || ':embeddings_api_key')" in migration
    assert "WHERE key = 'embeddings.api_key'\n      AND EXISTS" in migration
    assert "DELETE FROM config_store" in migration


def test_embedding_provider_cleanup_migration_removes_dead_keys() -> None:
    migration = (
        SRC_ROOT / "storage" / "migrations" / "272_drop_embedding_provider_config.sql"
    ).read_text(encoding="utf-8")

    assert "'ai.embeddings.provider'" in migration
    assert "'embeddings.provider'" in migration
    assert "DELETE FROM config_store" in migration


def test_removed_migration_baseline_and_import_files_are_absent() -> None:
    removed_paths = [
        SRC_ROOT / "storage" / "baseline_schema.sql",
        SRC_ROOT / "storage" / "migration",
        SRC_ROOT / "storage" / "migration_helpers.py",
        SRC_ROOT / "search" / "fts5.py",
        SRC_ROOT / "memory" / "fts_search.py",
        REPO_ROOT / "tests" / "fixtures" / ("sql" + "ite_test_schema.sql"),
        REPO_ROOT / "tests" / "storage" / "migration",
    ]

    assert [path for path in removed_paths if path.exists()] == []


def test_context_usage_snapshot_migration_and_baseline_define_session_snapshot_fields() -> None:
    migration = (SRC_ROOT / "storage" / "migrations" / "267_context_usage_snapshot.sql").read_text(
        encoding="utf-8"
    )
    baseline = (SRC_ROOT / "storage" / "postgres_baseline_schema.sql").read_text(encoding="utf-8")
    expected_columns = [
        "context_used_tokens",
        "context_usage_ratio",
        "context_usage_source",
        "context_usage_confidence",
        "context_usage_updated_at",
        "last_prompt_input_tokens",
        "last_prompt_uncached_input_tokens",
        "last_prompt_cache_read_tokens",
        "last_prompt_cache_creation_tokens",
        "last_completion_output_tokens",
    ]

    for column in expected_columns:
        assert f"ADD COLUMN IF NOT EXISTS {column}" in migration
        assert column in baseline

    assert "idx_sessions_context_usage_ratio" in migration
    assert "idx_sessions_context_usage_ratio" in baseline


def test_self_parent_sessions_migration_and_baseline_define_invariant() -> None:
    migration = (
        SRC_ROOT / "storage" / "migrations" / "268_prevent_self_parent_sessions.sql"
    ).read_text(encoding="utf-8")
    baseline = (SRC_ROOT / "storage" / "postgres_baseline_schema.sql").read_text(encoding="utf-8")
    invariant = "CHECK (parent_session_id IS NULL OR parent_session_id <> id)"

    assert "UPDATE sessions" in migration
    assert "SET parent_session_id = NULL" in migration
    assert "WHERE parent_session_id = id" in migration
    assert "sessions_parent_session_not_self" in migration
    assert invariant in migration
    assert "sessions_parent_session_not_self" in baseline
    assert invariant in baseline


def test_context_usage_ratio_range_migration_and_baseline_define_invariant() -> None:
    migration = (
        SRC_ROOT / "storage" / "migrations" / "269_context_usage_ratio_range.sql"
    ).read_text(encoding="utf-8")
    baseline = (SRC_ROOT / "storage" / "postgres_baseline_schema.sql").read_text(encoding="utf-8")
    invariant = "context_usage_ratio >= 0 AND context_usage_ratio <= 1"

    assert "sessions_context_usage_ratio_range" in migration
    assert invariant in migration
    assert "sessions_context_usage_ratio_range" in baseline
    assert invariant in baseline


def test_context_usage_value_constraints_migration_and_baseline_define_invariants() -> None:
    migration = (
        SRC_ROOT / "storage" / "migrations" / "270_context_usage_value_constraints.sql"
    ).read_text(encoding="utf-8")
    baseline = (SRC_ROOT / "storage" / "postgres_baseline_schema.sql").read_text(encoding="utf-8")
    token_invariant = "context_used_tokens IS NULL OR context_used_tokens >= 0"
    confidence_invariant = "context_usage_confidence IN ('reported', 'estimated', 'unknown')"

    assert "sessions_context_usage_tokens_nonnegative" in migration
    assert "UPDATE sessions" in migration
    assert "WHEN context_window < 0 THEN 0" in migration
    assert "ELSE 'unknown'" in migration
    assert token_invariant in migration
    assert "sessions_context_usage_confidence_valid" in migration
    assert confidence_invariant in migration
    assert "sessions_context_usage_tokens_nonnegative" in baseline
    assert token_invariant in baseline
    assert "sessions_context_usage_confidence_valid" in baseline
    assert confidence_invariant in baseline


def test_migration_helpers_are_not_imported_by_runtime_storage_paths() -> None:
    violations: list[str] = []

    for path in SRC_ROOT.rglob("*.py"):
        relative = path.relative_to(SRC_ROOT)
        import_lines = _imports_migration_helpers(path)
        if not import_lines:
            continue

        violations.extend(f"{relative}:{line}" for line in import_lines)

    assert violations == []
