from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "gobby"
MIGRATIONS_SOURCE = SRC_ROOT / "storage" / "migrations.py"
POSTGRES_BASELINE_SCHEMA = SRC_ROOT / "storage" / "postgres_baseline_schema.sql"
RECONCILE_DRIFT_MIGRATION = (
    SRC_ROOT / "storage" / "migrations" / "306_reconcile_live_hub_schema_drift.sql"
)
RECONCILE_DRIFT_V2_MIGRATION = (
    SRC_ROOT / "storage" / "migrations" / "356_reconcile_live_hub_schema_drift_v2.sql"
)
DROP_DEAD_TABLES_MIGRATION = SRC_ROOT / "storage" / "migrations" / "357_drop_dead_tables.sql"
MIGRATION_HELPERS_MODULE = "gobby.storage.migration_helpers"
MEMORY_DREAM_STATUS_INVARIANTS = (
    "'started'",
    "'running'",
    "'completed'",
    "'failed'",
    "'reverted'",
    "'revert_failed'",
    "'partial'",
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
MEMORY_DREAM_PROJECT_FK = "project_id UUID REFERENCES projects(id) ON DELETE CASCADE"
MEMORY_DREAM_PROJECT_COMMENT = (
    "Nullable for global/system dream runs; cron rows are anchored to PERSONAL_PROJECT_ID."
)
MEMORY_DREAM_SNAPSHOT_RUN_INDEX = "ON memory_dream_snapshots(run_id);"
MEMORY_DREAM_LEGACY_SNAPSHOT_RUN_INDEX = "ON memory_dream_snapshots(run_id, id);"
SESSION_CONTEXT_USAGE_RATIO_INDEX = (
    "CREATE INDEX idx_sessions_context_usage_ratio\n"
    "ON sessions(context_usage_ratio DESC)\n"
    "WHERE context_usage_ratio IS NOT NULL;"
)
MEMORY_DREAM_RUNTIME_NORMALIZERS = (
    "UPDATE memory_dream_snapshots\n               SET action = CASE",
    "UPDATE memory_dream_runs\n               SET status = 'failed'",
    "ADD CONSTRAINT memory_dream_runs_status_check",
)
DESTRUCTIVE_MIGRATION_VERSION = 355
DESTRUCTIVE_DIRECTIVE = "-- gobby:destructive"
DESTRUCTIVE_SQL_PATTERNS = (
    re.compile(r"\bDROP\s+(?:TABLE|INDEX|SCHEMA|TYPE)\b", re.IGNORECASE),
    re.compile(
        r"\bALTER\s+TABLE\b[\s\S]*?\bDROP\s+(?:COLUMN|CONSTRAINT)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bTRUNCATE\b", re.IGNORECASE),
    re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE),
)


def test_digest_owned_session_title_migration_contract() -> None:
    migration = (
        SRC_ROOT / "storage" / "migrations" / "341_digest_owned_session_titles.sql"
    ).read_text(encoding="utf-8")
    baseline = POSTGRES_BASELINE_SCHEMA.read_text(encoding="utf-8")

    assert "title_source IN ('heuristic', 'native', 'provisional')" in migration
    assert "title_source = 'provisional'" in migration
    assert "WHEN 'codex' THEN 'Codex'" in migration
    assert "WHEN 'claude' THEN 'Claude'" in migration
    assert "DROP COLUMN IF EXISTS last_title_synthesis_digest_hash" in migration
    assert "last_title_synthesis_digest_hash" not in baseline


def test_transcript_close_checklist_migration_removes_receipt_contract() -> None:
    criteria_migration = (
        SRC_ROOT / "storage" / "migrations" / "342_task_validation_epoch.sql"
    ).read_text(encoding="utf-8")
    cleanup = (
        SRC_ROOT / "storage" / "migrations" / "344_transcript_close_checklist.sql"
    ).read_text(encoding="utf-8")
    validation_constraint = criteria_migration.split(
        "ADD CONSTRAINT tasks_require_validation_criteria",
        maxsplit=1,
    )[1].split("ALTER TABLE verification_receipts", maxsplit=1)[0]

    assert "task_type = 'epic'" in validation_constraint
    assert "NULLIF(BTRIM(validation_criteria), '') IS NOT NULL" in validation_constraint
    assert "NOT VALID" in validation_constraint
    trigger = "DROP TRIGGER sessions_delete_unassigned_verification_receipts ON sessions"
    function = "DROP FUNCTION delete_unassigned_verification_receipts_for_session()"
    table = "DROP TABLE verification_receipts"
    epoch = "DROP COLUMN validation_epoch"
    assert trigger in cleanup
    assert function in cleanup
    assert table in cleanup
    assert epoch in cleanup
    assert cleanup.index(trigger) < cleanup.index(function) < cleanup.index(table)


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


def _contains_destructive_sql(sql: str) -> bool:
    return any(pattern.search(sql) is not None for pattern in DESTRUCTIVE_SQL_PATTERNS)


def _has_destructive_directive(sql: str) -> bool:
    return DESTRUCTIVE_DIRECTIVE in {
        line.strip() for line in sql.splitlines() if line.lstrip().startswith("--")
    }


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE retired;",
        "ALTER TABLE active DROP COLUMN retired;",
        "DROP INDEX retired;",
        "DROP SCHEMA retired;",
        "DROP TYPE retired;",
        "ALTER TABLE active DROP CONSTRAINT old_check;",
        "TRUNCATE active;",
        "DELETE FROM active;",
        "DO $migration$ BEGIN DROP TABLE retired; END; $migration$;",
    ],
)
def test_destructive_sql_marker_audit_recognizes_required_statements(sql: str) -> None:
    assert _contains_destructive_sql(sql)


def test_post_bookkeeping_destructive_migrations_carry_marker() -> None:
    migrations_dir = SRC_ROOT / "storage" / "migrations"
    unmarked: list[str] = []

    for path in migrations_dir.glob("*.sql"):
        version = int(path.name.split("_", 1)[0])
        if version < DESTRUCTIVE_MIGRATION_VERSION:
            continue
        sql = path.read_text(encoding="utf-8")
        if _contains_destructive_sql(sql) and not _has_destructive_directive(sql):
            unmarked.append(path.name)

    assert unmarked == []


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


def _normalize_sql_whitespace(content: str) -> str:
    return " ".join(content.split())


def _table_definition(content: str, table_name: str) -> str:
    marker = f"CREATE TABLE {table_name} ("
    start = content.find(marker)
    assert start != -1, f"{table_name} table missing from baseline"
    end = content.find("\n);\n", start)
    assert end != -1, f"{table_name} table definition is not terminated"
    return content[start:end]


def _tasks_table_definition(content: str) -> str:
    start = content.find("CREATE TABLE tasks (")
    assert start != -1, "tasks table missing from baseline"
    end = content.find("\n\nCREATE INDEX idx_tasks_project", start)
    assert end != -1, "tasks table definition is not terminated before task indexes"
    return content[start:end]


def _assert_column_type(table_sql: str, column_name: str, type_name: str) -> None:
    pattern = rf"(?m)^\s*{re.escape(column_name)}\s+{re.escape(type_name)}\b"
    assert re.search(pattern, table_sql), f"{column_name} is not declared as {type_name}"


def _baseline_text() -> str:
    return POSTGRES_BASELINE_SCHEMA.read_text(encoding="utf-8")


def _reconcile_drift_migration_text() -> str:
    return RECONCILE_DRIFT_MIGRATION.read_text(encoding="utf-8")


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


def test_postgres_migrations_preserve_known_post_baseline_sequence() -> None:
    migrations_dir = SRC_ROOT / "storage" / "migrations"

    # The 0.5.0 pre-release flatten folded every migration (295-305) into the
    # baseline schema. Later fixes remain replayable in numeric order.
    known_prefix = [
        "306_reconcile_live_hub_schema_drift.sql",
        "307_cron_run_scheduler_owner.sql",
        "308_recall_signal_hub.sql",
        "309_github_triage_delivery_leases.sql",
        "310_github_triage_build_dispatches.sql",
        "311_model_costs_provider_key.sql",
        "312_session_digest_pair_index.sql",
        "313_memory_source_session_set_null.sql",
        "314_memory_graph_retry_state.sql",
        "315_session_title_synthesis_digest_hash.sql",
        "316_memory_vector_reindex_state.sql",
        "317_sync_tombstones.sql",
        "318_chat_messages_sequence_unique.sql",
        "319_worktree_last_activity.sql",
        "320_projects_active_name_unique.sql",
        "321_session_variables_session_cascade.sql",
        "322_agent_run_capture_termination.sql",
        "323_recall_usefulness_digest_shadow.sql",
        "324_drop_sync_tombstones.sql",
        "325_recall_usefulness_shadow_index.sql",
        "326_validate_recall_usefulness_label_source.sql",
        "327_failure_category_taxonomy.sql",
        "328_memory_global_visibility.sql",
        "329_memory_type_enum.sql",
        "330_rename_epic_qa.sql",
        "331_external_issue_sync_coordinator.sql",
        "332_attention_states.sql",
        "333_detection_manifests.sql",
        "334_verification_receipts.sql",
        "335_memories_dream_due_version.sql",
        "336_model_metadata_rename.sql",
        "337_verification_receipts_default.sql",
        "338_plan_review_evidence.sql",
        "339_expired_plan_review_round_retry.sql",
        "340_tool_results.sql",
        "341_digest_owned_session_titles.sql",
        "342_task_validation_epoch.sql",
        "343_verification_receipt_worktree_attribution.sql",
        "344_transcript_close_checklist.sql",
        "346_cron_display_name.sql",
        "348_memory_dream_admission.sql",
        "349_task_claim_fk_restrict.sql",
        "352_external_issue_outbound_cursor.sql",
        "353_plan_review_experiment_purge.sql",
    ]
    migration_names = _tracked_migration_names(migrations_dir)

    assert migration_names[: len(known_prefix)] == known_prefix
    versions = [int(name.split("_", 1)[0]) for name in migration_names]
    known_versions = [int(name.split("_", 1)[0]) for name in known_prefix]
    assert versions[: len(known_versions)] == known_versions
    future_versions = versions[len(known_versions) :]
    assert future_versions == list(range(354, 354 + len(future_versions)))


def test_plan_review_experiment_purge_migration_and_baseline_contract() -> None:
    migrations_dir = SRC_ROOT / "storage" / "migrations"
    migration = (migrations_dir / "353_plan_review_experiment_purge.sql").read_text()
    baseline_table = _table_definition(_baseline_text(), "plan_review_evidence")
    removed_columns = (
        "quality_ledger",
        "repair_attestations",
        "prior_round_context",
        "vote_artifact",
        "vote_artifact_digest",
        "vote_receipt",
        "vote_receipt_digest",
    )

    normalized_migration = _normalize_sql_whitespace(migration)
    assert (
        "UPDATE plan_review_evidence SET expired_at = NOW() "
        "WHERE finalized_at IS NULL AND expired_at IS NULL;"
    ) in normalized_migration
    for column in removed_columns:
        assert f"DROP COLUMN IF EXISTS {column}" in migration
        assert column not in baseline_table
    for version in (345, 347, 350, 351):
        assert not list(migrations_dir.glob(f"{version}_*.sql"))


def test_memory_dream_due_version_schema_contract() -> None:
    """Fresh baselines and upgraded hubs expose the same monotonic fence column."""
    baseline = (SRC_ROOT / "storage" / "postgres_baseline_schema.sql").read_text()
    migration = (
        SRC_ROOT / "storage" / "migrations" / "335_memories_dream_due_version.sql"
    ).read_text()

    expected = "dream_due_version BIGINT NOT NULL DEFAULT 0"
    assert expected in baseline
    assert expected in migration


def test_recall_shadow_migration_defines_capture_and_gate_contract() -> None:
    migration_path = SRC_ROOT / "storage" / "migrations" / "323_recall_usefulness_digest_shadow.sql"
    migration = migration_path.read_text(encoding="utf-8")
    normalized = _normalize_sql_whitespace(migration)

    _assert_contains_all(
        "recall shadow migration",
        normalized,
        (
            "ADD COLUMN content_hash TEXT",
            "ADD COLUMN constants_provenance TEXT",
            "label_source IN ('llm_judge', 'ablation', 'digest', 'digest_shadow', 'human')",
            "CREATE TABLE recall_shadow_judge_state",
            "PRIMARY KEY (recall_request_id, label_source, judge_protocol_version)",
            "status IN ('claimed', 'retryable', 'terminal', 'complete')",
            "CREATE TABLE recall_shadow_prompt_snapshot",
            "presented JSONB NOT NULL",
            "judge_config_fingerprint TEXT NOT NULL",
            "prompt_hash TEXT NOT NULL",
            "CREATE TABLE recall_shadow_audit_verdicts",
            "UNIQUE (cohort_digest, request_id, memory_id)",
            "sample_digest TEXT NOT NULL",
            "human_verdict BOOLEAN NOT NULL",
            "CREATE TABLE recall_gate_runs",
            "holdout_consumption_key TEXT PRIMARY KEY",
            "status TEXT NOT NULL CHECK (status IN ('reserved', 'complete'))",
            "fit_settings_digest TEXT NOT NULL",
            "CREATE TABLE recall_holdout_consumed",
            "request_id TEXT UNIQUE NOT NULL",
        ),
    )
    assert "DELETE FROM RECALL_USEFULNESS" not in migration.upper()
    assert "VALIDATE CONSTRAINT recall_usefulness_label_source_check" not in migration


def test_recall_usefulness_constraint_validation_is_non_transactional() -> None:
    migration_path = (
        SRC_ROOT / "storage" / "migrations" / "326_validate_recall_usefulness_label_source.sql"
    )
    migration = migration_path.read_text(encoding="utf-8")

    assert migration.startswith("-- gobby:non-transactional\n")
    assert "VALIDATE CONSTRAINT recall_usefulness_label_source_check" in migration


def test_recall_shadow_index_migration_is_non_transactional() -> None:
    migration_path = SRC_ROOT / "storage" / "migrations" / "325_recall_usefulness_shadow_index.sql"
    migration = migration_path.read_text(encoding="utf-8")
    normalized = _normalize_sql_whitespace(migration)

    assert migration.startswith("-- gobby:non-transactional\n")
    _assert_contains_all(
        "recall shadow index migration",
        normalized,
        (
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_recall_usefulness_request_source_protocol",
            "ON recall_usefulness(recall_request_id, label_source, judge_protocol_version)",
        ),
    )


def test_worktree_last_activity_is_consistent_across_schema_and_migration() -> None:
    baseline = _baseline_text()
    migration = (SRC_ROOT / "storage" / "migrations" / "319_worktree_last_activity.sql").read_text(
        encoding="utf-8"
    )

    worktrees = _table_definition(baseline, "worktrees")
    assert "last_activity_at TIMESTAMPTZ" in worktrees
    assert "ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ" in migration
    assert "SET last_activity_at = updated_at" in migration


def test_uuid_cast_migrations_ship_a_preflight_guard() -> None:
    """Data-dependent uuid casts must preflight-scan for uncastable values.

    Migration tests run against fresh, empty schemas, so a bare
    ``ALTER ... TYPE UUID USING col::UUID`` that chokes on populated data is
    invisible to CI — migration 304 took the daemon down twice this way.
    Any migration performing uuid casts must include a preflight DO block
    that RAISEs with the offending columns before the first cast.
    The directory is empty post-flatten; this contract binds every future
    migration file.
    """
    migrations_dir = SRC_ROOT / "storage" / "migrations"

    for path in sorted(migrations_dir.glob("*.sql")):
        content = path.read_text(encoding="utf-8")
        if "TYPE UUID USING" not in content:
            continue
        assert "RAISE EXCEPTION" in content and "preflight" in content.lower(), (
            f"{path.name} performs uuid casts without a preflight guard; "
            "add a DO block that scans for uncastable values and RAISEs "
            "with the offending column names (see 305_uuid_completion in "
            "git history for the reference pattern)"
        )
        if path.name == "306_reconcile_live_hub_schema_drift.sql":
            normalized = _normalize_sql_whitespace(content)
            regex_fast_path = (
                "value ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"
            )
            assert regex_fast_path in normalized
            assert normalized.index(regex_fast_path) < normalized.index("PERFORM value::UUID")


def test_postgres_baseline_version_is_flattened_to_305() -> None:
    import gobby.storage.migrations as module

    # The 0.5.0 pre-release flatten folded 295-305 into the baseline. Hubs below
    # 305 take the corrupt_partial backup/recreate path; later migrations replay.
    assert module.BASELINE_VERSION == 305
    assert module.latest_known_version() >= 334


def test_external_issue_sync_migration_preserves_legacy_enablement() -> None:
    sql = (
        SRC_ROOT / "storage" / "migrations" / "331_external_issue_sync_coordinator.sql"
    ).read_text()

    assert "ADD COLUMN IF NOT EXISTS linear_sync_enabled BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "gobby:linear-sync:" in sql
    assert "SET enabled = FALSE" in sql
    assert "ADD COLUMN IF NOT EXISTS sync_enabled BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "ADD COLUMN IF NOT EXISTS triage_enabled BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "sync_enabled = enabled" in sql
    assert "triage_enabled = enabled" in sql
    assert "CREATE TABLE IF NOT EXISTS external_issue_sync_status" in sql


def test_external_issue_outbound_cursor_matches_baseline() -> None:
    baseline = (SRC_ROOT / "storage" / "postgres_baseline_schema.sql").read_text()
    migration = (
        SRC_ROOT / "storage" / "migrations" / "352_external_issue_outbound_cursor.sql"
    ).read_text()

    expected = "last_outbound_success_at TIMESTAMPTZ"
    assert expected in baseline
    assert expected in migration


def test_reconcile_cron_display_name_migration_is_dual_shape_safe() -> None:
    migration = (
        SRC_ROOT / "storage" / "migrations" / "355_reconcile_346_cron_display_name.sql"
    ).read_text(encoding="utf-8")
    normalized = _normalize_sql_whitespace(migration)

    assert normalized.startswith(f"{DESTRUCTIVE_DIRECTIVE} ")
    _assert_contains_all(
        "cron display name reconciliation migration",
        normalized,
        (
            "ALTER TABLE cron_jobs ADD COLUMN IF NOT EXISTS display_name TEXT;",
            "DROP TABLE IF EXISTS tmux_input_requests, tmux_input_pane_states;",
        ),
    )


def test_drop_dead_tables_migration_is_destructive_and_dual_shape_safe() -> None:
    migration = DROP_DEAD_TABLES_MIGRATION.read_text(encoding="utf-8")
    normalized = _normalize_sql_whitespace(migration)
    table_names = (
        "savings_ledger",
        "session_memories",
        "rule_overrides",
        "workflow_states",
        "tool_embeddings",
    )

    assert normalized.startswith(f"{DESTRUCTIVE_DIRECTIVE} ")
    for table_name in table_names:
        assert f"Evidence block: {table_name}" in migration
    _assert_contains_all(
        "dead-table removal migration",
        normalized,
        tuple(f"DROP TABLE IF EXISTS {table_name};" for table_name in table_names),
    )


def test_reconcile_live_hub_schema_drift_v2_matches_canonical_schema() -> None:
    migration = RECONCILE_DRIFT_V2_MIGRATION.read_text(encoding="utf-8")
    normalized = _normalize_sql_whitespace(migration)

    assert normalized.startswith(f"{DESTRUCTIVE_DIRECTIVE} ")
    _assert_contains_all(
        "live hub schema drift v2 reconciliation",
        normalized,
        (
            "CREATE TABLE IF NOT EXISTS memory_dream_truth_state",
            "ALTER COLUMN memory_type SET DEFAULT 'fact'",
            "ALTER COLUMN dream_due_version TYPE BIGINT",
            "ALTER COLUMN total_chars TYPE BIGINT",
            "ADD CONSTRAINT cron_jobs_name_key UNIQUE (name)",
            "ADD CONSTRAINT projects_linear_sync_enabled_check",
            "ADD CONSTRAINT project_github_triage_configs_sync_enabled_check",
            "ADD CONSTRAINT project_github_triage_configs_triage_enabled_check",
            "DROP INDEX IF EXISTS idx_inter_session_messages_unread",
            "DROP COLUMN IF EXISTS read_at",
            "DROP COLUMN IF EXISTS assignee",
        ),
    )


def test_failure_category_taxonomy_is_closed_in_baseline_and_migration() -> None:
    baseline = (SRC_ROOT / "storage" / "postgres_baseline_schema.sql").read_text(encoding="utf-8")
    migration = (
        SRC_ROOT / "storage" / "migrations" / "327_failure_category_taxonomy.sql"
    ).read_text(encoding="utf-8")
    categories = "'environment', 'dependency', 'code', 'test', 'provider', 'timeout'"

    for raw_schema in (baseline, migration):
        schema = _normalize_sql_whitespace(raw_schema)
        assert schema.count("failure_category TEXT CHECK") == 2
        assert schema.count(categories) == 2


def test_tombstone_cleanup_migration_and_baseline_remove_sync_objects() -> None:
    migration = (SRC_ROOT / "storage" / "migrations" / "324_drop_sync_tombstones.sql").read_text(
        encoding="utf-8"
    )
    baseline = _baseline_text()

    _assert_contains_all(
        "tombstone cleanup migration",
        migration,
        (
            "DROP TRIGGER IF EXISTS tasks_capture_sync_tombstone ON tasks",
            "DROP TRIGGER IF EXISTS memories_capture_sync_tombstone ON memories",
            "DROP FUNCTION IF EXISTS capture_sync_tombstone()",
            "DROP TABLE IF EXISTS sync_tombstones",
        ),
    )
    _assert_absent_all(
        "postgres baseline",
        baseline,
        (
            "tasks_capture_sync_tombstone",
            "memories_capture_sync_tombstone",
            "capture_sync_tombstone",
            "sync_tombstones",
        ),
    )


def test_postgres_baseline_uses_uuid_for_internal_identity_columns() -> None:
    baseline = _baseline_text()

    for table_name, columns in {
        "projects": ("id",),
        "sessions": (
            "id",
            "project_id",
            "parent_session_id",
            "summary_revision_id",
            "agent_run_id",
        ),
        "tasks": ("id", "project_id", "parent_task_id", "created_in_session_id"),
        "memories": ("id", "project_id", "source_session_id"),
        "memory_crossrefs": ("source_id", "target_id"),
        "code_symbols": ("id", "project_id", "parent_symbol_id"),
        "workflow_instances": ("id", "session_id"),
        "agent_runs": ("id",),
        "tool_metrics": ("id",),
        "build_runs": ("id",),
        "build_history_events": ("run_id",),
        "expansion_runs": ("id",),
        "worktrees": ("id",),
        "clones": ("id",),
        "merge_resolutions": ("id", "worktree_id"),
        "merge_conflicts": ("id", "resolution_id"),
        "skills": ("id",),
        "skill_files": ("id", "skill_id"),
        "cron_jobs": ("id",),
        "cron_runs": ("id", "cron_job_id", "agent_run_id", "pipeline_execution_id"),
        "pipeline_executions": ("id", "parent_execution_id"),
        "step_executions": ("execution_id",),
        "prompts": ("id",),
        "checkpoints": ("run_id",),
        "completion_subscribers": ("completion_id",),
        "task_dispatch_mutex": ("run_id",),
        "task_delivery_units": ("worktree_id",),
        "comms_channels": ("id",),
        "comms_identities": ("id", "channel_id", "session_id", "project_id"),
        "comms_messages": ("id", "channel_id", "identity_id", "session_id"),
        "comms_routing_rules": ("id", "channel_id", "project_id", "session_id"),
        "comms_attachments": ("id", "message_id"),
        "session_variables": ("session_id",),
        "unmodeled_observation_events": ("id", "session_id"),
        "unmodeled_observations": ("example_session_id",),
    }.items():
        table_sql = _table_definition(baseline, table_name)
        for column_name in columns:
            _assert_column_type(table_sql, column_name, "UUID")

    _assert_contains_all(
        "agent_runs internal references",
        baseline,
        (
            "ADD COLUMN parent_session_id UUID NOT NULL REFERENCES sessions(id)",
            "ADD COLUMN child_session_id UUID REFERENCES sessions(id)",
            "ADD COLUMN claimed_session_id UUID REFERENCES sessions(id)",
            "ADD COLUMN task_id UUID REFERENCES tasks(id)",
            "ADD COLUMN worktree_id UUID",
            "ADD COLUMN clone_id UUID",
        ),
    )

    # task_artifacts is declared with indentation the _table_definition helper
    # cannot terminate; assert its converted id columns by containment.
    _assert_contains_all(
        "task_artifacts isolation/expansion id columns",
        baseline,
        (
            "worktree_id UUID",
            "clone_id UUID",
            "integration_workspace_id UUID",
            "integration_clone_id UUID",
            "expansion_run_id UUID",
        ),
    )


def test_postgres_baseline_keeps_allowlisted_textual_ids() -> None:
    baseline = _baseline_text()

    for table_name, columns in {
        "comms_messages": ("platform_message_id", "platform_thread_id"),
        "comms_identities": ("external_user_id",),
        "sessions": ("external_id", "spawned_by_agent_id"),
        "secret_key_material": ("id",),
        "spans": ("trace_id", "span_id", "parent_span_id"),
    }.items():
        table_sql = _table_definition(baseline, table_name)
        for column_name in columns:
            _assert_column_type(table_sql, column_name, "TEXT")

    # task_stage_states is declared with indentation the _table_definition
    # helper cannot terminate; assert its actor columns by containment.
    _assert_contains_all(
        "task_stage_states actor columns",
        baseline,
        ("entered_by_actor TEXT", "completed_by_actor TEXT"),
    )


def test_unmodeled_observation_baseline_uses_uuid_session_columns() -> None:
    # Session ids in the observation telemetry tables are native uuid; the
    # event dedup key must be NULLS NOT DISTINCT so unknown-session events
    # (NULL session_id) still collapse per occurrence.
    baseline = _baseline_text()

    events_sql = _table_definition(baseline, "unmodeled_observation_events")
    _assert_column_type(events_sql, "session_id", "UUID")
    assert "UNIQUE NULLS NOT DISTINCT" in events_sql

    aggregate_sql = _table_definition(baseline, "unmodeled_observations")
    _assert_column_type(aggregate_sql, "example_session_id", "UUID")


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
        "previous_revision_id UUID",
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


def test_session_wiki_schema_removed_from_baseline() -> None:
    # The session wiki page is now the session summary; the second wiki
    # narrative, its 11 sessions.wiki_* columns, and session_wiki_revisions are
    # gone. Fresh DBs must never create any of it (the 298 drop migration was
    # folded into the baseline flatten).
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


def test_code_index_baseline_defines_projection_and_failure_tables() -> None:
    baseline = _baseline_text()
    indexed_files_table = _table_definition(baseline, "code_indexed_files")
    symbols_table = _table_definition(baseline, "code_symbols")

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
    symbol_retry_snippets = ("summary_attempted_at TIMESTAMPTZ",)
    prune_snippets = (
        "CREATE TABLE code_index_prune_dirty_projects",
        "project_id UUID PRIMARY KEY",
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
    _assert_contains_all(
        "code_symbols retry attempts baseline", symbols_table, symbol_retry_snippets
    )
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
    assert SESSION_CONTEXT_USAGE_RATIO_INDEX in baseline


def test_session_context_usage_ratio_index_baseline_and_migration_are_partial_desc() -> None:
    baseline = _baseline_text()
    migration = _reconcile_drift_migration_text()
    plain_index = "CREATE INDEX idx_sessions_context_usage_ratio ON sessions(context_usage_ratio);"
    normalized_plain_index = _normalize_sql_whitespace(plain_index)

    assert SESSION_CONTEXT_USAGE_RATIO_INDEX in baseline
    assert SESSION_CONTEXT_USAGE_RATIO_INDEX in migration
    assert normalized_plain_index not in _normalize_sql_whitespace(baseline)
    assert normalized_plain_index not in _normalize_sql_whitespace(migration)


def test_self_parent_sessions_migration_and_baseline_define_invariant() -> None:
    baseline = _baseline_text()
    invariant = "CHECK (parent_session_id IS NULL OR parent_session_id <> id)"

    assert "sessions_parent_session_not_self" in baseline
    assert invariant in baseline


def test_context_usage_ratio_range_migration_and_baseline_define_invariant() -> None:
    baseline = _baseline_text()
    migration = _reconcile_drift_migration_text()
    invariant = "context_usage_ratio >= 0 AND context_usage_ratio <= 1"

    assert "sessions_context_usage_ratio_range" in baseline
    assert invariant in baseline
    _assert_contains_all(
        "context usage ratio reconcile migration",
        migration,
        (
            "DROP CONSTRAINT IF EXISTS sessions_context_usage_ratio_range",
            "ALTER COLUMN context_usage_ratio TYPE DOUBLE PRECISION",
            "ADD CONSTRAINT sessions_context_usage_ratio_range",
            invariant,
        ),
    )
    _assert_absent_all("context usage ratio invariant", baseline + migration, ("::numeric",))


def test_context_usage_value_constraints_migration_and_baseline_define_invariants() -> None:
    baseline = _baseline_text()
    token_invariant = "context_used_tokens IS NULL OR context_used_tokens >= 0"
    confidence_invariant = "context_usage_confidence IN ('reported', 'estimated', 'unknown')"

    assert "sessions_context_usage_tokens_nonnegative" in baseline
    assert token_invariant in baseline
    assert "sessions_context_usage_confidence_valid" in baseline
    assert confidence_invariant in baseline


def test_tasks_baseline_and_migration_define_merge_flags_without_dead_columns() -> None:
    baseline = _baseline_text()
    migration = _reconcile_drift_migration_text()
    tasks_sql = _tasks_table_definition(baseline)

    _assert_contains_all(
        "tasks merge flag baseline",
        tasks_sql,
        (
            "merge_in_progress BOOLEAN NOT NULL DEFAULT FALSE",
            "blocked_by_merge BOOLEAN NOT NULL DEFAULT FALSE",
        ),
    )
    _assert_absent_all("tasks dead isolation columns", tasks_sql, ("worktree_id", "clone_id"))
    _assert_contains_all(
        "tasks merge flag migration",
        migration,
        (
            "SET merge_in_progress = FALSE",
            "SET blocked_by_merge = FALSE",
            "ALTER COLUMN merge_in_progress SET DEFAULT FALSE",
            "ALTER COLUMN merge_in_progress SET NOT NULL",
            "ALTER COLUMN blocked_by_merge SET DEFAULT FALSE",
            "ALTER COLUMN blocked_by_merge SET NOT NULL",
        ),
    )


def test_memory_global_visibility_schema_and_migration_contract() -> None:
    baseline = _normalize_sql_whitespace(_baseline_text())
    migration = _normalize_sql_whitespace(
        (SRC_ROOT / "storage" / "migrations" / "328_memory_global_visibility.sql").read_text(
            encoding="utf-8"
        )
    )

    _assert_contains_all(
        "memory global visibility baseline",
        baseline,
        (
            "project_id UUID NOT NULL REFERENCES projects(id) ON DELETE RESTRICT "
            "DEFERRABLE INITIALLY IMMEDIATE",
            "is_global BOOLEAN NOT NULL DEFAULT FALSE",
            "CREATE INDEX idx_memories_project_live ON memories(project_id, updated_at DESC) "
            "WHERE deleted_at IS NULL",
            "CREATE INDEX idx_memories_global_live ON memories(updated_at DESC) "
            "WHERE is_global IS TRUE AND deleted_at IS NULL",
        ),
    )
    _assert_contains_all(
        "memory global visibility migration",
        migration,
        (
            "ADD COLUMN IF NOT EXISTS is_global BOOLEAN NOT NULL DEFAULT FALSE",
            "SET project_id = '00000000-0000-0000-0000-000000060887'::uuid, "
            "is_global = TRUE WHERE project_id IS NULL",
            "ALTER COLUMN project_id SET NOT NULL",
            "FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT "
            "DEFERRABLE INITIALLY IMMEDIATE",
            "SET vector_needs_reindex = TRUE",
            "graph_status = 'pending'",
            "CREATE INDEX idx_memories_project_live ON memories(project_id, updated_at DESC) "
            "WHERE deleted_at IS NULL",
            "CREATE INDEX idx_memories_global_live ON memories(updated_at DESC) "
            "WHERE is_global IS TRUE AND deleted_at IS NULL",
        ),
    )


def test_memory_type_schema_and_migration_close_free_text_contract() -> None:
    baseline = _normalize_sql_whitespace(_baseline_text())
    migration = _normalize_sql_whitespace(
        (SRC_ROOT / "storage" / "migrations" / "329_memory_type_enum.sql").read_text(
            encoding="utf-8"
        )
    )

    constraint = (
        "CONSTRAINT memories_memory_type_check CHECK "
        "(memory_type IN ('fact', 'preference', 'pattern', 'context'))"
    )
    assert constraint in baseline
    assert constraint in migration
    for legacy_type, canonical_type in (
        ("debugging_pattern", "pattern"),
        ("user_preference", "preference"),
        ("project_context", "context"),
        ("implementation_note", "fact"),
    ):
        assert f"('{legacy_type}', '{canonical_type}')" in migration
    assert "COALESCE(memory_type_mapping.canonical_type, 'fact')" in migration
    assert "UPDATE memories SET vector_needs_reindex = TRUE" in migration


def test_epic_qa_schema_and_migration_contract() -> None:
    baseline = _normalize_sql_whitespace(_baseline_text())
    migration = _normalize_sql_whitespace(
        (SRC_ROOT / "storage" / "migrations" / "330_rename_epic_qa.sql").read_text(encoding="utf-8")
    )

    _assert_contains_all(
        "epic QA baseline",
        baseline,
        (
            "epic_qa_attempts INTEGER NOT NULL DEFAULT 0",
            "VALUES ('epic_qa', 'Epic QA', 'Whole-epic review after every leaf is parked.', "
            "'verification', 'epic-reviewer', 'epic-reviewer'",
            "VALUES ('epic', 'epic_qa', 7)",
        ),
    )
    _assert_contains_all(
        "epic QA migration",
        migration,
        (
            "SET CONSTRAINTS ALL DEFERRED",
            "RENAME COLUMN holistic_attempts TO epic_qa_attempts",
            "UPDATE task_stage_states SET stage_name = 'epic_qa'",
            "UPDATE task_type_default_stages SET stage_name = 'epic_qa'",
            "UPDATE task_stages_registry SET name = 'epic_qa'",
            "default_agent = 'epic-reviewer'",
            "reviewer_agent = 'epic-reviewer'",
            "UPDATE build_profiles AS profiles SET skip_stages_json = normalized.stages",
        ),
    )


def test_memory_dream_baseline_and_runtime_define_invariants() -> None:
    baseline = _baseline_text()
    migration = _reconcile_drift_migration_text()
    runtime_storage = (SRC_ROOT / "memory" / "dream" / "storage_schema.py").read_text(
        encoding="utf-8"
    )

    _assert_contains_all("memory dream baseline interrupted status", baseline, ("'interrupted'",))
    _assert_contains_all(
        "memory dream runtime storage interrupted status",
        runtime_storage,
        ("'interrupted'",),
    )
    _assert_memory_dream_project_scope("memory dream baseline", baseline)
    _assert_memory_dream_snapshot_run_index("memory dream baseline", baseline)
    _assert_memory_dream_snapshot_run_index("memory dream reconcile migration", migration)
    _assert_contains_all(
        "memory dream reconcile migration",
        migration,
        (
            "memory_dream_runs.id UUID preflight failed",
            "memory_dream_runs.project_id UUID preflight failed",
            "memory_dream_snapshots.run_id UUID preflight failed",
            "memory_dream_snapshots.memory_id UUID preflight failed",
            "pg_temp.gobby_is_uuid_castable",
            "ALTER COLUMN id TYPE UUID USING id::UUID",
            "ALTER COLUMN project_id TYPE UUID USING project_id::UUID",
            "ALTER COLUMN run_id TYPE UUID USING run_id::UUID",
            "ALTER COLUMN memory_id TYPE UUID USING memory_id::UUID",
            "memory_dream_runs_project_id_fkey",
            "LEFT JOIN projects projects",
            "FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE",
        ),
    )
    _assert_contains_all(
        "memory dream runtime storage UUID/FK shape",
        runtime_storage,
        (
            "id UUID PRIMARY KEY",
            "project_id UUID REFERENCES projects(id) ON DELETE CASCADE",
            "run_id UUID NOT NULL REFERENCES memory_dream_runs(id)",
            "memory_id UUID NOT NULL",
            "project_id TEXT PRIMARY KEY",
        ),
    )
    _assert_absent_all(
        "memory dream runtime storage legacy text IDs",
        runtime_storage,
        (
            "                id TEXT PRIMARY KEY",
            "run_id TEXT NOT NULL REFERENCES memory_dream_runs(id)",
            "memory_id TEXT NOT NULL",
        ),
    )
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


def test_memory_graph_retry_state_is_consistent_across_schema_and_runtime() -> None:
    """Baseline, replayable migration, model, and queue storage share one state contract."""
    baseline = _baseline_text()
    migration = (
        SRC_ROOT / "storage" / "migrations" / "314_memory_graph_retry_state.sql"
    ).read_text(encoding="utf-8")
    model = (SRC_ROOT / "storage" / "memories_models.py").read_text(encoding="utf-8")
    storage = (SRC_ROOT / "storage" / "memories_graph.py").read_text(encoding="utf-8")

    for label, content in (("baseline", baseline), ("migration", migration)):
        _assert_contains_all(
            f"memory graph retry {label}",
            content,
            (
                "graph_attempts",
                "graph_status",
                "'pending'",
                "'completed'",
                "'failed'",
            ),
        )
    _assert_contains_all(
        "memory graph retry model",
        model,
        ("graph_processed", "graph_attempts", "graph_status"),
    )
    _assert_contains_all(
        "memory graph retry storage",
        storage,
        ("record_graph_failure", "graph_attempts + 1", "graph_status = 'pending'"),
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


def test_model_metadata_uses_provider_scoped_primary_key_in_baseline_and_migrations() -> None:
    baseline = _baseline_text()
    provider_key_migration = (
        SRC_ROOT / "storage" / "migrations" / "311_model_costs_provider_key.sql"
    ).read_text(encoding="utf-8")
    rename_migration = (
        SRC_ROOT / "storage" / "migrations" / "336_model_metadata_rename.sql"
    ).read_text(encoding="utf-8")

    model_metadata = _table_definition(baseline, "model_metadata")
    assert "provider TEXT NOT NULL" in model_metadata
    assert "CONSTRAINT model_metadata_pkey PRIMARY KEY (provider, model)" in model_metadata
    assert "DROP CONSTRAINT IF EXISTS model_costs_pkey" in provider_key_migration
    assert "PRIMARY KEY (provider, model)" in provider_key_migration
    assert "ALTER TABLE model_costs RENAME TO model_metadata" in rename_migration
    assert "RENAME CONSTRAINT model_costs_pkey TO model_metadata_pkey" in rename_migration
    assert "pg_constraint" in rename_migration


def test_memory_source_session_fk_sets_null_in_baseline_and_upgrade_migration() -> None:
    memories = _normalize_sql_whitespace(_table_definition(_baseline_text(), "memories"))
    migration = _normalize_sql_whitespace(
        (SRC_ROOT / "storage" / "migrations" / "313_memory_source_session_set_null.sql").read_text(
            encoding="utf-8"
        )
    )

    assert (
        "source_session_id UUID REFERENCES sessions(id) ON DELETE SET NULL "
        "DEFERRABLE INITIALLY IMMEDIATE"
    ) in memories
    assert "DROP CONSTRAINT IF EXISTS memories_source_session_id_fkey" in migration
    assert "FOREIGN KEY (source_session_id) REFERENCES sessions(id) ON DELETE SET NULL" in migration


def test_session_variables_fk_cascades_in_baseline_and_upgrade_migration() -> None:
    session_variables = _normalize_sql_whitespace(
        _table_definition(_baseline_text(), "session_variables")
    )
    migration = _normalize_sql_whitespace(
        (
            SRC_ROOT / "storage" / "migrations" / "321_session_variables_session_cascade.sql"
        ).read_text(encoding="utf-8")
    )

    assert (
        "session_id UUID PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE "
        "DEFERRABLE INITIALLY IMMEDIATE"
    ) in session_variables
    assert "DELETE FROM session_variables" in migration
    assert "FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE" in migration


def test_memory_vector_reindex_state_is_consistent_across_schema_and_runtime() -> None:
    baseline = _baseline_text()
    migration = (
        SRC_ROOT / "storage" / "migrations" / "316_memory_vector_reindex_state.sql"
    ).read_text(encoding="utf-8")
    model = (SRC_ROOT / "storage" / "memories_models.py").read_text(encoding="utf-8")
    storage = (SRC_ROOT / "storage" / "memories_crud.py").read_text(encoding="utf-8")

    for label, content in (("baseline", baseline), ("migration", migration)):
        _assert_contains_all(
            f"memory vector reindex {label}",
            content,
            ("vector_needs_reindex", "DEFAULT FALSE", "WHERE vector_needs_reindex IS TRUE"),
        )
    _assert_contains_all(
        "memory vector reindex model",
        model,
        ("vector_needs_reindex",),
    )
    _assert_contains_all(
        "memory vector reindex storage",
        storage,
        ("list_vector_reindex_ids", "mark_vectors_reindexed", "content = %s"),
    )


def test_chat_message_sequence_constraint_is_consistent_across_schema_and_migration() -> None:
    baseline = _baseline_text()
    migration = (
        SRC_ROOT / "storage" / "migrations" / "318_chat_messages_sequence_unique.sql"
    ).read_text(encoding="utf-8")

    for label, content in (("baseline", baseline), ("migration", migration)):
        _assert_contains_all(
            f"chat message sequence {label}",
            content,
            (
                "chat_messages_conversation_seq_unique",
                "UNIQUE (conversation_id, seq)",
            ),
        )
    _assert_contains_all(
        "chat message sequence migration",
        migration,
        ("duplicate_rank > 1", "replacement_seq", "DROP INDEX IF EXISTS"),
    )
