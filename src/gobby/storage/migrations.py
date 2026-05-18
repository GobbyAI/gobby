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
from collections.abc import Callable
from pathlib import Path

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

BASELINE_VERSION = 259
_MIN_MIGRATION_VERSION = 258
BASELINE_SCHEMA = (Path(__file__).parent / "baseline_schema.sql").read_text()

MIGRATIONS: list[tuple[int, str, MigrationAction]] = [
    (
        259,
        "Add inter-session completion notification lookup index",
        """
        CREATE INDEX IF NOT EXISTS idx_ism_completion_lookup
            ON inter_session_messages(to_session, message_type)
            WHERE metadata_json IS NOT NULL;
        """,
    ),
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
