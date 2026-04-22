"""Database migrations for local storage.

For new databases (version == 0):
    BASELINE_SCHEMA is applied, jumping directly to BASELINE_VERSION.

For existing databases at or above the minimum supported version:
    Any migrations in MIGRATIONS beyond BASELINE_VERSION are applied incrementally.

To add a new migration:
    1. Add helper callables to gobby.storage.migration_helpers when needed.
    2. Add the migration to gobby.storage._migration_registry.MIGRATIONS.
    3. Also add the migration to BASELINE_SCHEMA for future fresh installs.
"""

import logging
from collections.abc import Callable
from pathlib import Path

from gobby.storage._migration_registry import MIGRATIONS
from gobby.storage.database import LocalDatabase
from gobby.storage.migration_helpers import (
    _add_column_if_missing,
    _add_prune_empty_session_indexes,
    _add_summary_column,
    _column_exists,
    _drop_agent_runs_mode,
    _drop_column_if_exists,
    _drop_summary_column,
    _migrate_add_token_events,
    _migrate_agent_run_claimed_session_id,
    _migrate_agent_run_reasoning_fields,
    _migrate_claimed_by_session_id,
    _migrate_code_graph_target_schema,
    _migrate_expansion_runs,
    _migrate_sessions_sandbox_fields,
    _migrate_task_lifecycle_stage,
    _migrate_tasks_claimed_session_fk_set_null,
    _narrow_memories_fts_update_trigger,
    _remove_usd_columns,
    _setup_code_content_fts,
    _setup_code_symbols_fts,
    _setup_fts_tables,
    _setup_memories_fts,
    _setup_skills_fts,
    _setup_tasks_fts,
    _table_exists,
    _tasks_claimed_session_fk_is_set_null,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BASELINE_VERSION",
    "BASELINE_SCHEMA",
    "MIGRATIONS",
    "MigrationAction",
    "MigrationUnsupportedError",
    "_add_column_if_missing",
    "_add_prune_empty_session_indexes",
    "_add_summary_column",
    "_column_exists",
    "_drop_agent_runs_mode",
    "_drop_column_if_exists",
    "_drop_summary_column",
    "_migrate_add_token_events",
    "_migrate_agent_run_claimed_session_id",
    "_migrate_agent_run_reasoning_fields",
    "_migrate_claimed_by_session_id",
    "_migrate_code_graph_target_schema",
    "_migrate_expansion_runs",
    "_migrate_sessions_sandbox_fields",
    "_migrate_task_lifecycle_stage",
    "_migrate_tasks_claimed_session_fk_set_null",
    "_narrow_memories_fts_update_trigger",
    "_remove_usd_columns",
    "_run_migration_list",
    "_setup_code_content_fts",
    "_setup_code_symbols_fts",
    "_setup_fts_tables",
    "_setup_memories_fts",
    "_setup_skills_fts",
    "_setup_tasks_fts",
    "_table_exists",
    "_tasks_claimed_session_fk_is_set_null",
    "get_current_version",
    "run_migrations",
]


class MigrationUnsupportedError(Exception):
    """Raised when database version is too old to migrate."""

    pass


MigrationAction = str | Callable[[LocalDatabase], None]

BASELINE_VERSION = 217
_MIN_MIGRATION_VERSION = 171
BASELINE_SCHEMA = (Path(__file__).parent / "baseline_schema.sql").read_text()


def get_current_version(db: LocalDatabase) -> int:
    """Get current schema version from database."""
    try:
        row = db.fetchone("SELECT MAX(version) as version FROM schema_version")
        return row["version"] if row and row["version"] else 0
    except Exception:
        return 0


def _apply_baseline(db: LocalDatabase) -> None:
    """Apply baseline schema for new databases (flattened at v171)."""
    logger.info("Applying baseline schema (v171)")

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
        - Runs any new migrations after the recorded schema version.

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
        # Unsupported: Pre-v171 database without legacy migrations
        msg = (
            f"Database version {current_version} is older than minimum "
            f"migration version {_MIN_MIGRATION_VERSION}. "
            f"Upgrade not supported without legacy migrations. "
            f"To recover: 1) Back up ~/.gobby/gobby-hub.db, "
            f"2) Delete the database file, 3) Restart the daemon "
            f"(gobby restart) to reinitialize with a fresh schema."
        )
        logger.error(msg)
        raise MigrationUnsupportedError(msg)

    # Run any new migrations (v172+)
    if MIGRATIONS:
        applied = _run_migration_list(db, current_version, MIGRATIONS)
        total_applied += applied

    # Existing databases may be missing the bootstrapped root session due to
    # prior drift or partial upgrades; restore it idempotently on every startup.
    from gobby.storage.sessions import ensure_system_session

    ensure_system_session(db)

    return total_applied
