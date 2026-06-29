from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "gobby"
MIGRATIONS_SOURCE = SRC_ROOT / "storage" / "migrations.py"
POSTGRES_BASELINE_SCHEMA = SRC_ROOT / "storage" / "postgres_baseline_schema.sql"
MIGRATION_HELPERS_MODULE = "gobby.storage.migration_helpers"
MEMORY_DREAM_STATUS_INVARIANTS = (
    "'started'",
    "'running'",
    "'completed'",
    "'failed'",
    "'reverted'",
    "'revert_failed'",
)
MEMORY_DREAM_LEGACY_ACTION_INVARIANT = (
    "action IN ('keep', 'delete', 'refresh', 'merge', 'supersede', 'review')"
)
MEMORY_DREAM_PROMOTE_ACTION_INVARIANTS = (
    "'keep'",
    "'delete'",
    "'refresh'",
    "'merge'",
    "'supersede'",
    "'review'",
    "'promote'",
)
MEMORY_DREAM_PROJECT_FK = "project_id TEXT REFERENCES projects(id) ON DELETE CASCADE"
MEMORY_DREAM_PROJECT_COMMENT = (
    "Nullable for global/system dream runs; cron rows are anchored to PERSONAL_PROJECT_ID."
)
MEMORY_DREAM_SNAPSHOT_RUN_INDEX = "ON memory_dream_snapshots(run_id);"
MEMORY_DREAM_LEGACY_SNAPSHOT_RUN_INDEX = "ON memory_dream_snapshots(run_id, id);"
MEMORY_DREAM_RUNTIME_NORMALIZERS = (
    "UPDATE memory_dream_snapshots\n               SET action = CASE",
    "UPDATE memory_dream_runs\n               SET status = 'failed'",
    "ADD CONSTRAINT memory_dream_runs_status_check",
)


def _tracked_migration_names(migrations_dir: Path) -> list[str]:
    relative_dir = migrations_dir.relative_to(REPO_ROOT)
    try:
        result = subprocess.run(
            [
                "git",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                str(relative_dir),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return sorted(path.name for path in migrations_dir.glob("*.sql"))
    if result.returncode == 0 and result.stdout.strip():
        return sorted(
            Path(line).name
            for line in result.stdout.splitlines()
            if line.endswith(".sql") and (REPO_ROOT / line).exists()
        )
    return sorted(path.name for path in migrations_dir.glob("*.sql"))


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


def _assert_contains_all(label: str, content: str, snippets: tuple[str, ...]) -> None:
    missing = [snippet for snippet in snippets if snippet not in content]
    assert missing == [], f"{label} missing expected snippets: {missing}"


def _assert_absent_all(label: str, content: str, snippets: tuple[str, ...]) -> None:
    present = [snippet for snippet in snippets if snippet in content]
    assert present == [], f"{label} contained forbidden snippets: {present}"


def _table_definition(content: str, table_name: str) -> str:
    marker = f"CREATE TABLE {table_name} ("
    start = content.find(marker)
    assert start != -1, f"{table_name} table missing from baseline"
    end = content.find("\n);\n", start)
    assert end != -1, f"{table_name} table definition is not terminated"
    return content[start:end]


def _baseline_text() -> str:
    return POSTGRES_BASELINE_SCHEMA.read_text(encoding="utf-8")


def _assert_memory_dream_project_scope(label: str, content: str) -> None:
    _assert_contains_all(label, content, (MEMORY_DREAM_PROJECT_FK, MEMORY_DREAM_PROJECT_COMMENT))


def _assert_memory_dream_snapshot_run_index(label: str, content: str) -> None:
    _assert_contains_all(label, content, (MEMORY_DREAM_SNAPSHOT_RUN_INDEX,))
    _assert_absent_all(label, content, (MEMORY_DREAM_LEGACY_SNAPSHOT_RUN_INDEX,))


def _assert_memory_dream_constraints(
    label: str, content: str, *, promote_supported: bool = False
) -> None:
    action_invariants: tuple[str, ...] = MEMORY_DREAM_PROMOTE_ACTION_INVARIANTS
    if not promote_supported:
        action_invariants = (MEMORY_DREAM_LEGACY_ACTION_INVARIANT,)
    _assert_contains_all(
        label,
        content,
        (
            "memory_dream_runs_status_check",
            *MEMORY_DREAM_STATUS_INVARIANTS,
            "memory_dream_snapshots_action_check",
            *action_invariants,
        ),
    )


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


def test_postgres_migrations_limited_to_known_post_baseline() -> None:
    migrations_dir = SRC_ROOT / "storage" / "migrations"

    # Post-flatten the baseline schema carries the squashed state; only these
    # incremental migrations remain on disk for already-provisioned hubs. A
    # stray .sql file here would silently run against every existing DB.
    assert _tracked_migration_names(migrations_dir) == [
        "295_relabel_gemini_sessions.postgres.sql",
        "298_drop_session_wiki_schema.postgres.sql",
        "299_unmodeled_observations.postgres.sql",
        "300_purge_unmodeled_observations_for_hash_v2.postgres.sql",
    ]


def test_postgres_baseline_version_is_flattened_to_297() -> None:
    import gobby.storage.migrations as module

    # Baseline stays 297: bumping it would reclassify existing 297 hubs as
    # corrupt_partial (recreation-required) instead of upgrading in place. The
    # post-baseline migrations ship above 297, so
    # latest_known_version reflects the migration file.
    assert module.BASELINE_VERSION == 297
    assert module.latest_known_version() == 300


def test_unmodeled_observation_hash_v2_purge_migration() -> None:
    migration = (
        SRC_ROOT
        / "storage"
        / "migrations"
        / "300_purge_unmodeled_observations_for_hash_v2.postgres.sql"
    ).read_text(encoding="utf-8")

    _assert_contains_all(
        "unmodeled observation hash v2 purge",
        migration,
        (
            "to_regclass('public.unmodeled_observation_events')",
            "DELETE FROM unmodeled_observation_events",
            "to_regclass('public.unmodeled_observations')",
            "DELETE FROM unmodeled_observations",
        ),
    )


def test_postgres_baseline_defines_implementation_domain_and_current_config_state() -> None:
    baseline = _baseline_text()

    _assert_contains_all(
        "implementation domain baseline",
        baseline,
        (
            "implementation_domain TEXT CHECK(",
            "implementation_domain IN ('backend', 'frontend', 'fullstack')",
        ),
    )
    _assert_absent_all(
        "baseline config seed",
        baseline,
        (
            'INSERT INTO "config_store"',
            "databases.neo4j.",
            "embeddings.provider",
            "ai.embeddings.provider",
            "llm_providers",
        ),
    )


def test_session_summary_revisions_baseline_defines_schema() -> None:
    baseline = _baseline_text()
    session_columns = (
        "summary_revision_id",
        "summary_source_context_hash",
        "summary_digest_turn_count",
        "summary_generation_mode",
        "summary_generated_at",
    )
    revision_snippets = (
        "CREATE TABLE session_summary_revisions",
        "summary_markdown TEXT NOT NULL",
        "generation_mode TEXT NOT NULL",
        "source_context_hash TEXT",
        "source_digest_turn_count INTEGER",
        "previous_revision_id TEXT",
        "metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb",
        "session_summary_revisions_digest_turn_count_nonnegative",
        "session_summary_revisions_generation_mode_valid",
        "sessions_summary_revision_fk",
        "idx_session_summary_revisions_session_created",
        "idx_sessions_summary_revision",
    )

    for column in session_columns:
        assert column in baseline

    integrity_snippets = (
        "sessions_summary_digest_turn_count_nonnegative",
        "session_summary_revisions_id_session_id_unique",
        "session_summary_revisions_previous_same_session_fk",
        "FOREIGN KEY (previous_revision_id, session_id)",
        "FOREIGN KEY (summary_revision_id, id)",
        "ON DELETE SET NULL (summary_revision_id)",
    )
    _assert_contains_all("session summary revision baseline", baseline, revision_snippets)
    _assert_contains_all("summary revision integrity baseline", baseline, integrity_snippets)


def test_session_wiki_schema_removed_from_baseline_and_dropped_by_migration() -> None:
    # The session wiki page is now the session summary; the second wiki
    # narrative, its 11 sessions.wiki_* columns, and session_wiki_revisions are
    # gone. Fresh DBs (baseline at 298) must never create any of it, and the
    # 298 migration must drop it from already-provisioned 297 hubs.
    baseline = _baseline_text()
    removed_objects = (
        "wiki_path",
        "wiki_markdown",
        "wiki_revision_id",
        "wiki_source_context_hash",
        "wiki_digest_turn_count",
        "wiki_generation_mode",
        "wiki_generated_at",
        "wiki_synthesis_consecutive_failures",
        "wiki_synthesis_last_failure_reason",
        "wiki_synthesis_last_error",
        "wiki_synthesis_last_failed_at",
        "session_wiki_revisions",
        "sessions_wiki_revision_fk",
        "idx_sessions_wiki_revision",
        "idx_sessions_wiki_synthesis_failures_source",
        "sessions_wiki_digest_turn_count_nonnegative",
        "sessions_wiki_synthesis_consecutive_failures_nonnegative",
    )
    _assert_absent_all("session wiki baseline removal", baseline, removed_objects)

    migration = (
        SRC_ROOT / "storage" / "migrations" / "298_drop_session_wiki_schema.postgres.sql"
    ).read_text()
    for removed_object in removed_objects:
        assert removed_object in migration
    _assert_contains_all(
        "session wiki drop migration",
        migration,
        (
            "DROP CONSTRAINT IF EXISTS sessions_wiki_revision_fk",
            "DROP INDEX IF EXISTS idx_sessions_wiki_revision",
            "DROP INDEX IF EXISTS idx_sessions_wiki_synthesis_failures_source",
            "DROP CONSTRAINT IF EXISTS sessions_wiki_digest_turn_count_nonnegative",
            "DROP COLUMN IF EXISTS wiki_path",
            "DROP COLUMN IF EXISTS wiki_markdown",
            "DROP COLUMN IF EXISTS wiki_synthesis_consecutive_failures",
            "DROP COLUMN IF EXISTS wiki_synthesis_last_failed_at",
            "DROP TABLE IF EXISTS session_wiki_revisions CASCADE",
        ),
    )


def test_code_index_baseline_defines_projection_and_failure_tables() -> None:
    baseline = _baseline_text()
    indexed_files_table = _table_definition(baseline, "code_indexed_files")

    projection_snippets = (
        "CREATE TABLE code_index_projection_cleanup_pending",
        "PRIMARY KEY(project_id, store)",
        "code_index_projection_cleanup_store",
        "CHECK (store IN ('graph', 'vector'))",
        "CREATE INDEX idx_cipcp_updated",
    )
    indexed_file_sync_snippets = (
        "graph_synced BOOLEAN NOT NULL DEFAULT FALSE",
        "vectors_synced BOOLEAN NOT NULL DEFAULT FALSE",
        "graph_sync_attempted_at TIMESTAMPTZ",
        "vector_sync_attempted_at TIMESTAMPTZ",
    )
    failure_snippets = ("summary_attempted_at TIMESTAMPTZ",)
    prune_snippets = (
        "CREATE TABLE code_index_prune_dirty_projects",
        "project_id TEXT PRIMARY KEY",
        "root_path TEXT NOT NULL",
        "reason TEXT NOT NULL",
        "attempts INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX idx_cipdp_updated",
    )

    _assert_contains_all("projection cleanup baseline", baseline, projection_snippets)
    _assert_contains_all(
        "code_indexed_files sync attempts baseline",
        indexed_files_table,
        indexed_file_sync_snippets,
    )
    _assert_contains_all("code index failure attempts baseline", baseline, failure_snippets)
    _assert_contains_all("code index prune dirty baseline", baseline, prune_snippets)


def test_plan_enhancement_baseline_defines_artifacts_and_build_profile_columns() -> None:
    baseline = _baseline_text()

    _assert_contains_all(
        "plan enhancement artifacts baseline",
        baseline,
        (
            "plan_enhancement_rounds INTEGER NOT NULL DEFAULT 0",
            "plan_enhancement_rounds_completed INTEGER NOT NULL DEFAULT 0",
            "plan_enhancement_converged BOOLEAN NOT NULL DEFAULT FALSE",
            "task_artifacts_plan_enhancement_rounds_nonnegative",
            "task_artifacts_plan_enhancement_rounds_completed_nonnegative",
        ),
    )
    _assert_contains_all(
        "build profile plan enhancement baseline",
        baseline,
        (
            "CREATE TABLE build_profiles",
            "plan_enhancement_rounds INTEGER NOT NULL DEFAULT 0",
            "CHECK (plan_enhancement_rounds >= 0)",
        ),
    )


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
    baseline = _baseline_text()
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
        assert column in baseline

    assert "context_usage_ratio DOUBLE PRECISION" in baseline
    assert "idx_sessions_context_usage_ratio" in baseline


def test_self_parent_sessions_migration_and_baseline_define_invariant() -> None:
    baseline = _baseline_text()
    invariant = "CHECK (parent_session_id IS NULL OR parent_session_id <> id)"

    assert "sessions_parent_session_not_self" in baseline
    assert invariant in baseline


def test_context_usage_ratio_range_migration_and_baseline_define_invariant() -> None:
    baseline = _baseline_text()
    invariant = "context_usage_ratio >= 0 AND context_usage_ratio <= 1"

    assert "sessions_context_usage_ratio_range" in baseline
    assert invariant in baseline


def test_context_usage_value_constraints_migration_and_baseline_define_invariants() -> None:
    baseline = _baseline_text()
    token_invariant = "context_used_tokens IS NULL OR context_used_tokens >= 0"
    confidence_invariant = "context_usage_confidence IN ('reported', 'estimated', 'unknown')"

    assert "sessions_context_usage_tokens_nonnegative" in baseline
    assert token_invariant in baseline
    assert "sessions_context_usage_confidence_valid" in baseline
    assert confidence_invariant in baseline


def test_memory_dream_baseline_and_runtime_define_invariants() -> None:
    baseline = _baseline_text()
    runtime_storage = (SRC_ROOT / "memory" / "dream" / "storage.py").read_text(encoding="utf-8")

    _assert_contains_all("memory dream baseline interrupted status", baseline, ("'interrupted'",))
    _assert_contains_all(
        "memory dream runtime storage interrupted status",
        runtime_storage,
        ("'interrupted'",),
    )
    _assert_memory_dream_project_scope("memory dream baseline", baseline)
    _assert_memory_dream_snapshot_run_index("memory dream baseline", baseline)
    _assert_memory_dream_constraints("memory dream baseline", baseline, promote_supported=True)
    _assert_memory_dream_constraints(
        "memory dream runtime storage", runtime_storage, promote_supported=True
    )
    _assert_absent_all(
        "memory dream runtime storage",
        runtime_storage,
        MEMORY_DREAM_RUNTIME_NORMALIZERS,
    )
    _assert_absent_all(
        "baseline retired memory cleanup cron seed",
        baseline,
        (
            "nightly-memory-cleanup",
            "gobby:nightly-memory-cleanup",
            "gobby:memory-cleanup",
        ),
    )


def test_migration_helpers_are_not_imported_by_runtime_storage_paths() -> None:
    violations: list[str] = []

    for path in SRC_ROOT.rglob("*.py"):
        relative = path.relative_to(SRC_ROOT)
        import_lines = _imports_migration_helpers(path)
        if not import_lines:
            continue

        violations.extend(f"{relative}:{line}" for line in import_lines)

    assert violations == []
