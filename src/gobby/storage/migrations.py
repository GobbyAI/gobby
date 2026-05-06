"""Database migrations for local storage.

For new databases (version == 0):
    BASELINE_SCHEMA is applied, jumping directly to BASELINE_VERSION.

For existing databases at or above the current baseline:
    Any future migrations in MIGRATIONS beyond BASELINE_VERSION are applied incrementally.

Existing SQLite databases below the current baseline are intentionally unsupported. They
must be reset or manually recovered; historical migration code is recoverable from Git.

To add a new migration:
    1. Add helper callables to gobby.storage.migration_helpers when needed.
    2. Add the migration to MIGRATIONS below.
    3. Also add the migration to BASELINE_SCHEMA for future fresh installs.
"""

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from gobby.storage.bin_update_state import BIN_UPDATE_STATE_SCHEMA
from gobby.storage.database import LocalDatabase
from gobby.storage.migration_helpers import (
    _setup_code_content_fts,
    _setup_code_symbols_fts,
    _setup_memories_fts,
    _setup_skills_fts,
    _setup_tasks_fts,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BASELINE_VERSION",
    "BASELINE_SCHEMA",
    "MIGRATIONS",
    "MigrationAction",
    "MigrationUnsupportedError",
    "_apply_baseline",
    "_run_migration_list",
    "_setup_code_content_fts",
    "_setup_code_symbols_fts",
    "_setup_memories_fts",
    "_setup_skills_fts",
    "_setup_tasks_fts",
    "get_current_version",
    "latest_known_version",
    "migrations_needed",
    "run_migrations",
]


class MigrationUnsupportedError(Exception):
    """Raised when database version is too old to migrate."""


MigrationAction = str | Callable[[LocalDatabase], None]

BASELINE_VERSION = 239
_MIN_MIGRATION_VERSION = BASELINE_VERSION
BASELINE_SCHEMA = (Path(__file__).parent / "baseline_schema.sql").read_text()
_TASK_STATE_BUCKET_VALUES = (
    "'ready','in_progress','needs_review','review_approved','closed','escalated'"
)

_DELIVERY_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_delivery_campaigns (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'pending',
    merge_strategy TEXT NOT NULL DEFAULT 'squash'
        CHECK (merge_strategy IN ('merge', 'squash', 'rebase')),
    structured_pr_verdict TEXT,
    pr_report_ref TEXT,
    merge_sha TEXT,
    merge_report_ref TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_delivery_units (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    unit_key TEXT NOT NULL,
    worktree_id TEXT,
    repo TEXT,
    source_branch TEXT,
    target_branch TEXT NOT NULL DEFAULT 'main',
    pr_required INTEGER CHECK (pr_required IN (0, 1)),
    protection_json TEXT,
    pr_url TEXT,
    github_pr_number INTEGER,
    gate_snapshot_json TEXT,
    pr_state TEXT,
    local_update_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(task_id, unit_key)
);

CREATE INDEX IF NOT EXISTS idx_task_delivery_units_task_id
    ON task_delivery_units(task_id);

CREATE INDEX IF NOT EXISTS idx_task_delivery_units_pr_url
    ON task_delivery_units(pr_url);
"""

_GITHUB_TRIAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_github_triage_configs (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    webhook_enabled INTEGER NOT NULL DEFAULT 0 CHECK (webhook_enabled IN (0, 1)),
    repositories_json TEXT NOT NULL DEFAULT '[]',
    reconcile_interval_seconds INTEGER NOT NULL DEFAULT 3600
        CHECK (reconcile_interval_seconds > 0),
    webhook_secret_ref TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS gh_triage_deliveries (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    delivery_id TEXT NOT NULL,
    event TEXT NOT NULL,
    action TEXT,
    repository TEXT,
    issue_number INTEGER,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'processed', 'ignored', 'duplicate', 'error')),
    payload_hash TEXT NOT NULL,
    headers_json TEXT NOT NULL DEFAULT '{}',
    raw_body TEXT NOT NULL DEFAULT '',
    error TEXT,
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    processed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, delivery_id)
);

CREATE INDEX IF NOT EXISTS idx_gh_triage_deliveries_project_status
    ON gh_triage_deliveries(project_id, status);

CREATE INDEX IF NOT EXISTS idx_gh_triage_deliveries_issue
    ON gh_triage_deliveries(project_id, repository, issue_number);

CREATE TABLE IF NOT EXISTS gh_issues_triaged (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    issue_url TEXT,
    issue_state TEXT,
    labels_json TEXT NOT NULL DEFAULT '[]',
    issue_updated_at TEXT,
    content_hash TEXT NOT NULL,
    verdict TEXT NOT NULL
        CHECK (verdict IN ('implement', 'skip', 'escalate', 'dedup')),
    decision_json TEXT NOT NULL DEFAULT '{}',
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    vector_point_id TEXT,
    dedup_issue_key TEXT,
    source TEXT NOT NULL,
    last_triaged_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, repo, issue_number)
);

CREATE INDEX IF NOT EXISTS idx_gh_issues_triaged_project_hash
    ON gh_issues_triaged(project_id, content_hash);

CREATE INDEX IF NOT EXISTS idx_gh_issues_triaged_task
    ON gh_issues_triaged(task_id);
"""

_GITHUB_ISSUE_TASK_LINK_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_github_issue_link
    ON tasks(project_id, github_repo, github_issue_number)
    WHERE github_repo IS NOT NULL
      AND github_issue_number IS NOT NULL;
"""

_REVIEW_ANCHOR_DEFAULT_STAGE_SCHEMA = """
INSERT OR IGNORE INTO task_type_default_stages (task_type, stage_name, position)
VALUES ('review_anchor', 'planning', 0);
"""

_STALE_CONFIG_STORE_EXACT_KEYS = frozenset(
    {
        "_meta.yaml_imported",
        "conductor.daily_budget_usd",
        "conductor.throttle_threshold",
        "conductor.tracking_window_days",
        "conductor.warning_threshold",
        "embeddings.provider",
        "gobby_tasks.expansion.max_subtasks",
        "gobby_tasks.validation.external_validator_mode",
        "gobby_tasks.validation.use_external_validator",
        "llm_providers.api_keys.openai_api_key",
        "logging.watchdog",
        "memory.mem0_url",
        "ui_settings.defaultchatmode",
        "ui_settings.fontsize",
        "ui_settings.selectedprojectid",
        "workflow.protected_tools",
        "workflow.require_task_before_edit",
    }
)

_STALE_CONFIG_STORE_PREFIXES = (
    "gobby_tasks.enrichment.",
    "review.",
    "task_description.",
    "title_synthesis.",
    "hook_extensions.plugins.",
    "llm_providers.litellm.",
    "memory_extraction.",
    "watchdog.",
)

_LEGACY_DELIVERY_ARTIFACT_COLUMNS = frozenset(
    {
        "pr_url",
        "merge_commit_sha",
        "pr_review_report",
        "structured_pr_verdict",
        "merge_campaign_report",
    }
)


# State bucket precedence is canonical: closed -> escalated -> first non-done stage -> ready.
def _task_state_bucket_case_sql(task_id_expr: str) -> str:
    return f"""
    CASE
        WHEN closed_at IS NOT NULL THEN 'closed'
        WHEN escalated_at IS NOT NULL OR COALESCE(is_escalated, 0) = 1 THEN 'escalated'
        ELSE COALESCE(
            (
                SELECT CASE
                    WHEN stage_scan.state IN (
                        'ready', 'in_progress', 'needs_review', 'review_approved'
                    )
                    THEN stage_scan.state
                    ELSE 'ready'
                END
                  FROM task_stage_states stage_scan
                 WHERE stage_scan.task_id = {task_id_expr}
                   AND stage_scan.state != 'done'
                 ORDER BY stage_scan.position
                 LIMIT 1
            ),
            'ready'
        )
    END
    """


def _task_state_bucket_trigger_update_sql(task_id_expr: str) -> str:
    return f"""
    UPDATE tasks
       SET state_bucket = {_task_state_bucket_case_sql(task_id_expr)}
     WHERE id = {task_id_expr};
    """


def _task_state_bucket_trigger_sql() -> tuple[str, ...]:
    return (
        f"""
        CREATE TRIGGER IF NOT EXISTS tasks_state_bucket_ai
        AFTER INSERT ON tasks
        BEGIN
            {_task_state_bucket_trigger_update_sql("NEW.id")}
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS tasks_state_bucket_au
        AFTER UPDATE OF closed_at, escalated_at, is_escalated ON tasks
        BEGIN
            {_task_state_bucket_trigger_update_sql("NEW.id")}
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS task_stage_states_state_bucket_ai
        AFTER INSERT ON task_stage_states
        BEGIN
            {_task_state_bucket_trigger_update_sql("NEW.task_id")}
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS task_stage_states_state_bucket_au
        AFTER UPDATE OF state, position ON task_stage_states
        BEGIN
            {_task_state_bucket_trigger_update_sql("NEW.task_id")}
            UPDATE tasks
               SET state_bucket = {_task_state_bucket_case_sql("OLD.task_id")}
             WHERE id = OLD.task_id
               AND OLD.task_id != NEW.task_id;
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS task_stage_states_state_bucket_ad
        AFTER DELETE ON task_stage_states
        BEGIN
            {_task_state_bucket_trigger_update_sql("OLD.task_id")}
        END
        """,
    )


def _apply_task_state_bucket_schema(db: LocalDatabase) -> None:
    task_columns = {row["name"] for row in db.fetchall("PRAGMA table_info(tasks)")}
    if "state_bucket" not in task_columns:
        db.execute(
            "ALTER TABLE tasks ADD COLUMN state_bucket TEXT NOT NULL DEFAULT 'ready' "
            f"CHECK(state_bucket IN ({_TASK_STATE_BUCKET_VALUES}))"
        )

    db.execute(f"UPDATE tasks SET state_bucket = {_task_state_bucket_case_sql('tasks.id')}")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_state_bucket ON tasks(state_bucket)")
    for statement in _task_state_bucket_trigger_sql():
        db.execute(statement)


def _require_sqlite_drop_column_support() -> None:
    if sqlite3.sqlite_version_info >= (3, 35, 0):
        return
    raise MigrationUnsupportedError(
        "Task delivery state migration requires SQLite 3.35.0 or newer for "
        "ALTER TABLE DROP COLUMN support. Upgrade SQLite or recreate the local "
        "Gobby database from the flattened baseline before deploying this build."
    )


def _apply_delivery_state_schema(db: LocalDatabase) -> None:
    for statement in _DELIVERY_STATE_SCHEMA.strip().split(";"):
        statement = statement.strip()
        if statement:
            db.execute(statement)

    artifact_columns = {row["name"] for row in db.fetchall("PRAGMA table_info(task_artifacts)")}
    legacy_columns = _LEGACY_DELIVERY_ARTIFACT_COLUMNS & artifact_columns
    if legacy_columns:
        _require_sqlite_drop_column_support()
    for column in sorted(legacy_columns):
        db.execute(f"ALTER TABLE task_artifacts DROP COLUMN {column}")  # nosec B608


def _apply_github_triage_schema(db: LocalDatabase) -> None:
    for statement in _GITHUB_TRIAGE_SCHEMA.strip().split(";"):
        statement = statement.strip()
        if statement:
            db.execute(statement)


def _apply_github_issue_task_link_index(db: LocalDatabase) -> None:
    db.execute(_GITHUB_ISSUE_TASK_LINK_INDEX)


def _apply_stage_dispatch_schema(db: LocalDatabase) -> None:
    columns = {row["name"] for row in db.fetchall("PRAGMA table_info(task_stages_registry)")}
    additions = {
        "dispatch_type": (
            "ALTER TABLE task_stages_registry ADD COLUMN dispatch_type TEXT "
            "CHECK (dispatch_type IS NULL OR dispatch_type IN ('agent','pipeline'))"
        ),
        "dispatch_target": "ALTER TABLE task_stages_registry ADD COLUMN dispatch_target TEXT",
        "dispatch_inputs_json": (
            "ALTER TABLE task_stages_registry ADD COLUMN dispatch_inputs_json TEXT"
        ),
    }
    for column, sql in additions.items():
        if column not in columns:
            db.execute(sql)


def _apply_bin_update_state_schema(db: LocalDatabase) -> None:
    db.execute(BIN_UPDATE_STATE_SCHEMA)


def _apply_config_store_cleanup(db: LocalDatabase) -> None:
    row = db.fetchone(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'config_store'"
    )
    if row is None:
        return

    from gobby.config.app import _LOGGING_TO_TELEMETRY_FIELDS

    legacy_logging_keys = set()
    for old_field, new_field in _LOGGING_TO_TELEMETRY_FIELDS.items():
        old_key = f"logging.{old_field}"
        new_key = f"telemetry.{new_field}"
        legacy_logging_keys.add(old_key)
        db.execute(
            """
            INSERT INTO config_store (key, value, source, is_secret, updated_at)
            SELECT ?, value, source, is_secret, datetime('now')
              FROM config_store
             WHERE key = ?
               AND NOT EXISTS (SELECT 1 FROM config_store WHERE key = ?)
            """,
            (new_key, old_key, new_key),
        )

    delete_keys = _STALE_CONFIG_STORE_EXACT_KEYS | legacy_logging_keys
    db.executemany(
        "DELETE FROM config_store WHERE key = ?",
        [(key,) for key in sorted(delete_keys)],
    )

    for prefix in _STALE_CONFIG_STORE_PREFIXES:
        db.execute(
            "DELETE FROM config_store WHERE substr(key, 1, ?) = ?",
            (len(prefix), prefix),
        )


def _apply_dispatch_mutex_run_id_index(db: LocalDatabase) -> None:
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dispatch_mutex_run_id
            ON task_dispatch_mutex(run_id)
        """
    )


def _apply_chat_message_content_blocks_schema(db: LocalDatabase) -> None:
    columns = {row["name"] for row in db.fetchall("PRAGMA table_info(chat_messages)")}
    if "content_blocks_json" not in columns:
        db.execute("ALTER TABLE chat_messages ADD COLUMN content_blocks_json TEXT")


def _apply_linear_project_binding_schema(db: LocalDatabase) -> None:
    columns = {row["name"] for row in db.fetchall("PRAGMA table_info(projects)")}
    if "linear_project_id" not in columns:
        db.execute("ALTER TABLE projects ADD COLUMN linear_project_id TEXT")


def _apply_integration_workspace_metadata_schema(db: LocalDatabase) -> None:
    worktree_columns = {row["name"] for row in db.fetchall("PRAGMA table_info(worktrees)")}
    if "workspace_role" not in worktree_columns:
        db.execute("ALTER TABLE worktrees ADD COLUMN workspace_role TEXT NOT NULL DEFAULT 'task'")

    clone_columns = {row["name"] for row in db.fetchall("PRAGMA table_info(clones)")}
    if "workspace_role" not in clone_columns:
        db.execute("ALTER TABLE clones ADD COLUMN workspace_role TEXT NOT NULL DEFAULT 'task'")

    artifact_columns = {row["name"] for row in db.fetchall("PRAGMA table_info(task_artifacts)")}
    if "integration_branch" not in artifact_columns:
        db.execute("ALTER TABLE task_artifacts ADD COLUMN integration_branch TEXT")
    if "integration_workspace_id" not in artifact_columns:
        db.execute("ALTER TABLE task_artifacts ADD COLUMN integration_workspace_id TEXT")
    if "integration_clone_id" not in artifact_columns:
        db.execute("ALTER TABLE task_artifacts ADD COLUMN integration_clone_id TEXT")

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS integration_workspace_mutex (
            integration_key TEXT PRIMARY KEY,
            lease_until TEXT,
            lease_holder TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


MIGRATIONS: list[tuple[int, str, MigrationAction]] = [
    (
        240,
        "Add task delivery state tables (requires SQLite >= 3.35 for legacy column drops)",
        _apply_delivery_state_schema,
    ),
    (241, "Add GitHub issue triage tables", _apply_github_triage_schema),
    (242, "Add review anchor default planning stage", _REVIEW_ANCHOR_DEFAULT_STAGE_SCHEMA),
    (243, "Clean stale config store keys", _apply_config_store_cleanup),
    (244, "Add persisted task state bucket", _apply_task_state_bucket_schema),
    (245, "Add unique linked GitHub issue task index", _apply_github_issue_task_link_index),
    (246, "Add generic stage dispatch target columns", _apply_stage_dispatch_schema),
    (247, "Add managed native binary update state", _apply_bin_update_state_schema),
    (248, "Add dispatch mutex run id lookup index", _apply_dispatch_mutex_run_id_index),
    (249, "Persist rich web chat content blocks", _apply_chat_message_content_blocks_schema),
    (250, "Add Linear project binding to projects", _apply_linear_project_binding_schema),
    (251, "Add integration workspace metadata", _apply_integration_workspace_metadata_schema),
]


def get_current_version(db: LocalDatabase) -> int:
    """Get current schema version from database."""
    try:
        row = db.fetchone("SELECT MAX(version) as version FROM schema_version")
        return row["version"] if row and row["version"] else 0
    except Exception:
        return 0


def latest_known_version() -> int:
    """Return the newest schema version known to this build."""
    return max(
        BASELINE_VERSION,
        max((version for version, _description, _action in MIGRATIONS), default=BASELINE_VERSION),
    )


def migrations_needed(db: LocalDatabase) -> bool:
    """Return whether schema migrations should run for this database.

    This is intentionally a schema-version check only. Startup repair work that lives in
    run_migrations should still be executed by normal daemon startup.
    """
    current_version = get_current_version(db)
    if current_version == 0 or current_version < _MIN_MIGRATION_VERSION:
        return True
    return current_version < latest_known_version()


def _apply_baseline(db: LocalDatabase) -> None:
    """Apply baseline schema for new databases."""
    logger.info("Applying baseline schema (v%s)", BASELINE_VERSION)

    with db.transaction() as conn:
        conn.executescript(BASELINE_SCHEMA)
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (BASELINE_VERSION,),
        )

    _setup_code_symbols_fts(db, include_summary=True)
    _setup_code_content_fts(db)
    _setup_tasks_fts(db)
    _setup_skills_fts(db)
    _setup_memories_fts(db)

    logger.info("Baseline schema applied, now at version %s", BASELINE_VERSION)


def _run_migration_list(
    db: LocalDatabase,
    current_version: int,
    migrations: list[tuple[int, str, MigrationAction]],
) -> int:
    """
    Run migrations from a list.

    Args:
        db: LocalDatabase instance
        current_version: Current schema version
        migrations: List of (version, description, action) tuples

    Returns:
        Number of migrations applied
    """
    applied = 0
    last_version = current_version

    for version, description, action in migrations:
        if version > current_version:
            logger.debug("Applying migration %s: %s", version, description)
            try:
                if callable(action):
                    with db.transaction():
                        action(db)
                        db.execute(
                            "INSERT INTO schema_version (version) VALUES (?)",
                            (version,),
                        )
                else:
                    with db.transaction():
                        for statement in action.strip().split(";"):
                            statement = statement.strip()
                            if statement:
                                db.execute(statement)
                        db.execute(
                            "INSERT INTO schema_version (version) VALUES (?)",
                            (version,),
                        )
                applied += 1
                last_version = version
            except Exception as e:
                logger.error("Migration %s failed: %s", version, e)
                raise

    if applied > 0:
        logger.debug("Applied %s migration(s), now at version %s", applied, last_version)

    return applied


def run_migrations(db: LocalDatabase) -> int:
    """
    Run pending migrations.

    For new databases:
        - Applies the current baseline schema directly.

    For existing databases:
        - Versions below BASELINE_VERSION raise MigrationUnsupportedError.
        - Versions at or above BASELINE_VERSION run future SQLite migrations.
        - Versions above the latest known migration are left untouched.

    Args:
        db: LocalDatabase instance

    Returns:
        Number of migrations applied
    """
    current_version = get_current_version(db)
    total_applied = 0

    if current_version == 0:
        logger.info("Using flattened baseline for new database")
        _apply_baseline(db)
        total_applied = 1
        current_version = BASELINE_VERSION
    elif current_version < _MIN_MIGRATION_VERSION:
        msg = (
            f"Database version {current_version} is below the current SQLite baseline "
            f"{BASELINE_VERSION}. Direct upgrade is unsupported; reset "
            "~/.gobby/gobby-hub.db or manually recover the data from a backup before "
            "starting this Gobby build."
        )
        logger.error(msg)
        raise MigrationUnsupportedError(msg)

    latest_version = latest_known_version()
    if current_version > latest_version:
        logger.info(
            "Database version %s is newer than this build's latest known SQLite "
            "schema %s; leaving it untouched.",
            current_version,
            latest_version,
        )
        return 0

    if MIGRATIONS:
        total_applied += _run_migration_list(db, current_version, MIGRATIONS)

    from gobby.storage.sessions import ensure_system_session
    from gobby.storage.tasks import TaskDispatchMutexManager

    ensure_system_session(db)
    TaskDispatchMutexManager(db).sweep_expired()

    return total_applied
