"""Database migrations for local storage.

For new databases (version == 0):
    BASELINE_SCHEMA is applied, jumping directly to BASELINE_VERSION.

For existing databases at the launch baseline:
    Any migrations in MIGRATIONS beyond BASELINE_VERSION are applied incrementally.

Older pre-launch SQLite databases are intentionally unsupported. Newer
versions are left untouched so a newer build's schema is never downgraded.

To add a new migration:
    1. Add helper callables to gobby.storage.migration_helpers when needed.
    2. Add the migration to gobby.storage._migration_registry.MIGRATIONS.
    3. Also add the migration to BASELINE_SCHEMA for future fresh installs.
"""

import logging
from collections.abc import Callable
from pathlib import Path

from gobby.storage._migration_registry import MIGRATIONS as _REGISTRY_MIGRATIONS
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
    "_run_migration_list",
    "_setup_code_content_fts",
    "_setup_code_symbols_fts",
    "_setup_memories_fts",
    "_setup_skills_fts",
    "_setup_tasks_fts",
    "get_current_version",
    "run_migrations",
]


class MigrationUnsupportedError(Exception):
    """Raised when database version is too old to migrate."""

    pass


MigrationAction = str | Callable[[LocalDatabase], None]

BASELINE_VERSION = 220
_MIN_MIGRATION_VERSION = 219
BASELINE_SCHEMA = (Path(__file__).parent / "baseline_schema.sql").read_text()


def _table_columns(db: LocalDatabase, table_name: str) -> set[str]:
    return {row["name"] for row in db.fetchall(f"PRAGMA table_info({table_name})")}


def _task_artifacts_create_sql(table_name: str) -> str:
    return f"""
        CREATE TABLE {table_name} (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            plan_file_path TEXT,
            plan_file_hash TEXT,
            worktree_path TEXT,
            worktree_id TEXT,
            clone_path TEXT,
            clone_id TEXT,
            base_commit_sha TEXT,
            target_branch TEXT,
            expansion_run_id TEXT,
            expansion_attempts INTEGER NOT NULL DEFAULT 0,
            pr_url TEXT,
            merge_commit_sha TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (
                (worktree_path IS NULL) = (worktree_id IS NULL)
                AND (clone_path IS NULL) = (clone_id IS NULL)
                AND (worktree_path IS NULL OR clone_path IS NULL)
                AND (
                    base_commit_sha IS NULL
                    OR worktree_path IS NOT NULL
                    OR clone_path IS NOT NULL
                )
            )
        )
        """


def _add_task_artifact_evidence_columns(db: LocalDatabase) -> None:
    row = db.fetchone("SELECT sql FROM sqlite_master WHERE type='table' AND name='task_artifacts'")
    if row is None:
        db.execute(_task_artifacts_create_sql("task_artifacts"))
        return

    existing_columns = _table_columns(db, "task_artifacts")
    table_sql = str(row["sql"] or "")
    if {"base_commit_sha", "plan_file_hash"}.issubset(
        existing_columns
    ) and "base_commit_sha IS NULL" in table_sql:
        return

    columns = [
        "task_id",
        "plan_file_path",
        "plan_file_hash",
        "worktree_path",
        "worktree_id",
        "clone_path",
        "clone_id",
        "base_commit_sha",
        "target_branch",
        "expansion_run_id",
        "expansion_attempts",
        "pr_url",
        "merge_commit_sha",
        "updated_at",
    ]
    select_columns = [
        column if column in existing_columns else _default_task_artifact_column(column)
        for column in columns
    ]

    db.execute("ALTER TABLE task_artifacts RENAME TO task_artifacts_old")
    db.execute(_task_artifacts_create_sql("task_artifacts"))
    db.execute(
        f"""
        INSERT INTO task_artifacts ({", ".join(columns)})
        SELECT {", ".join(select_columns)}
        FROM task_artifacts_old
        """,  # nosec B608 - columns are fixed allowlist values.
    )
    db.execute("DROP TABLE task_artifacts_old")


def _default_task_artifact_column(column: str) -> str:
    if column == "expansion_attempts":
        return "0 AS expansion_attempts"
    if column == "updated_at":
        return "CURRENT_TIMESTAMP AS updated_at"
    return f"NULL AS {column}"


MIGRATIONS: list[tuple[int, str, MigrationAction]] = [
    *_REGISTRY_MIGRATIONS,
    (
        223,
        "Add evidence metadata to task_artifacts",
        _add_task_artifact_evidence_columns,
    ),
]


def get_current_version(db: LocalDatabase) -> int:
    """Get current schema version from database."""
    try:
        row = db.fetchone("SELECT MAX(version) as version FROM schema_version")
        return row["version"] if row and row["version"] else 0
    except Exception:
        return 0


def _apply_baseline(db: LocalDatabase) -> None:
    """Apply baseline schema for new databases (flattened at v220)."""
    logger.info("Applying baseline schema (v220)")

    with db.transaction() as conn:
        # Execute baseline schema
        for statement in BASELINE_SCHEMA.strip().split(";"):
            statement = statement.strip()
            if statement:
                conn.execute(statement)

        # Record baseline version
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (BASELINE_VERSION,),
        )

    # FTS5 triggers use semicolons in BEGIN...END — can't go through the split
    _setup_code_symbols_fts(db, include_summary=True)
    _setup_code_content_fts(db)
    _setup_tasks_fts(db)
    _setup_skills_fts(db)
    _setup_memories_fts(db)

    logger.info(f"Baseline schema applied, now at version {BASELINE_VERSION}")


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
            logger.debug(f"Applying migration {version}: {description}")
            try:
                if callable(action):
                    # Python data migration
                    with db.transaction():
                        action(db)
                        db.execute(
                            "INSERT INTO schema_version (version) VALUES (?)",
                            (version,),
                        )
                else:
                    # SQL migration (may contain multiple statements)
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
                logger.error(f"Migration {version} failed: {e}")
                raise

    if applied > 0:
        logger.debug(f"Applied {applied} migration(s), now at version {last_version}")

    return applied


def run_migrations(db: LocalDatabase) -> int:
    """
    Run pending migrations.

    For new databases (version == 0):
        - Applies the current baseline schema directly.

    For existing databases:
        - Versions 219+ run any future SQLite migrations and repair the system session.
        - Versions below 219 raise MigrationUnsupportedError.
        - Versions above the latest known migration are left untouched.

    Args:
        db: LocalDatabase instance

    Returns:
        Number of migrations applied
    """
    current_version = get_current_version(db)
    total_applied = 0

    if current_version == 0:
        # New database with flattened baseline: apply schema directly
        logger.info("Using flattened baseline for new database")
        _apply_baseline(db)
        total_applied = 1
        current_version = BASELINE_VERSION
    elif current_version < _MIN_MIGRATION_VERSION:
        # Unsupported: pre-launch SQLite database without legacy migrations.
        msg = (
            f"Database version {current_version} predates the SQLite launch "
            f"baseline {_MIN_MIGRATION_VERSION}. Direct upgrade is unsupported. "
            f"To recover: stop the daemon, remove ~/.gobby/gobby-hub.db or "
            f"restore your pre-cutover backup, then restart Gobby to initialize "
            f"schema {BASELINE_VERSION}."
        )
        logger.error(msg)
        raise MigrationUnsupportedError(msg)

    latest_known_version = max(
        BASELINE_VERSION,
        max((version for version, _description, _action in MIGRATIONS), default=BASELINE_VERSION),
    )
    if current_version > latest_known_version:
        logger.info(
            "Database version %s is newer than this build's latest known SQLite "
            "schema %s; leaving it untouched.",
            current_version,
            latest_known_version,
        )
        return 0

    # Run any new migrations after the flattened launch baseline.
    if MIGRATIONS:
        applied = _run_migration_list(db, current_version, MIGRATIONS)
        total_applied += applied

    # Existing databases may be missing the bootstrapped root session due to
    # prior drift or partial upgrades; restore it idempotently on every startup.
    from gobby.storage.sessions import ensure_system_session

    ensure_system_session(db)

    return total_applied
