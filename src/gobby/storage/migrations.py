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

from gobby.storage._migration_registry import MIGRATIONS
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
