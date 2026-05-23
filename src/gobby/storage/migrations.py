"""PostgreSQL hub migrations plus DEPRECATED_SQLITE_IMPORT_TEST_ONLY fixtures."""

from __future__ import annotations

import importlib.resources
import logging
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Protocol

from gobby.storage.database import LocalDatabase
from gobby.storage.hub.protocol import HubDatabase, Transaction

logger = logging.getLogger(__name__)

__all__ = [
    "BASELINE_VERSION",
    "MIGRATIONS",
    "Migration",
    "MigrationAction",
    "MigrationRunner",
    "MigrationUnsupportedError",
    "_migrate_bookkeeping_table",
    "_run_migration_list",
    "_split_statements_respecting_dollar_quotes",
    "get_current_version",
    "latest_known_version",
    "migrate_neo4j_config_to_falkordb",
    "migrations_needed",
    "run_migrations",
]


class MigrationUnsupportedError(Exception):
    """Raised when database version is too old or bookkeeping is corrupt."""


MigrationAction = str | Callable[[LocalDatabase], None]

BASELINE_VERSION = 261
# Historical SQLite migration bands through v260 are accepted only as import sources.
_MIN_MIGRATION_VERSION = 260


# Runtime migrations are file-based PostgreSQL migrations only.
MIGRATIONS: list[tuple[int, str, MigrationAction]] = []

_NEO4J_BACKEND_NEUTRAL_KEYS = ("graph_search", "graph_min_score", "rrf_k", "graph_name")


def migrate_neo4j_config_to_falkordb(db: LocalDatabase) -> None:
    """Migrate legacy Neo4j config-store rows to FalkorDB-compatible rows.

    The runtime PostgreSQL path uses the matching file-based migration under
    ``gobby.storage.migrations``. This SQLite-compatible helper preserves the
    same behavior for legacy import and migration regression tests.
    """
    with db.transaction():
        for key in _NEO4J_BACKEND_NEUTRAL_KEYS:
            db.execute(
                """
                INSERT OR IGNORE INTO config_store (key, value, source, is_secret, updated_at)
                SELECT REPLACE(key, 'databases.neo4j.', 'databases.falkordb.'),
                       value,
                       source,
                       is_secret,
                       updated_at
                  FROM config_store
                 WHERE key = ?
                """,
                (f"databases.neo4j.{key}",),
            )

        db.execute("DELETE FROM config_store WHERE key LIKE 'databases.neo4j.%'")
        db.execute(
            """
            DELETE FROM secrets
             WHERE name = 'auth'
               AND NOT EXISTS (
                   SELECT 1
                     FROM config_store
                    WHERE value = json_quote('$secret:auth')
               )
            """
        )


def _describe_legacy_migration_entry(entry: object) -> str:
    if not isinstance(entry, tuple) or len(entry) != 3:
        return repr(entry)

    version, description, action = entry
    action_kind = "callable" if callable(action) else type(action).__name__
    return f"v{version} {description!r} action={action_kind}"


def _ensure_no_legacy_migration_entries() -> None:
    """Fail fast if Python or SQL-string migration entries are reintroduced."""
    if not MIGRATIONS:
        return

    entries = "; ".join(_describe_legacy_migration_entry(entry) for entry in MIGRATIONS)
    raise MigrationUnsupportedError(
        "MIGRATIONS must remain empty after the file-based migration cutover. "
        "Add declarative SQL files under src/gobby/storage/migrations/NNN_name.sql "
        f"instead of legacy Python or SQL-string migration entries. Found: {entries}"
    )


_MIGRATION_FILE_RE = re.compile(
    r"^(?P<version>\d+)_(?P<name>.+?)(?:\.(?P<dialect>sqlite|postgres))?\.sql$"
)
_SCHEMA_MIGRATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL
)
"""


class _TransactionLike(Protocol):
    def execute(self, sql: str, params: Any = ()) -> Any: ...


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    shared_path: Traversable | None
    sqlite_path: Traversable | None
    postgres_path: Traversable | None

    def path_for_dialect(self, dialect: str) -> Traversable:
        if dialect == "sqlite" and self.sqlite_path is not None:
            return self.sqlite_path
        if dialect == "postgres" and self.postgres_path is not None:
            return self.postgres_path
        if self.shared_path is not None:
            return self.shared_path
        raise RuntimeError(f"No {dialect} migration file for v{self.version}")


class MigrationRunner:
    """Backend-neutral file-based migration runner."""

    def __init__(self, hub: HubDatabase) -> None:
        self._hub = hub

    def apply_pending(self) -> None:
        _ensure_no_legacy_migration_entries()
        if self._hub.dialect == "sqlite":
            raise MigrationUnsupportedError(
                "SQLite hub migrations were removed. Use `gobby postgres migrate-from-sqlite` "
                "to import a legacy SQLite database into PostgreSQL."
            )
        _migrate_bookkeeping_table(self._hub)
        self._ensure_schema_migrations_table()
        applied = self._read_applied_versions()
        for migration in self._discover_migrations():
            if migration.version in applied:
                continue
            logger.warning("Applying PostgreSQL migration %s_%s", migration.version, migration.name)
            with self._hub.transaction() as txn:
                self._run_migration(txn, migration)
                self._record_applied_version(txn, migration.version)
            applied.add(migration.version)

    def _ensure_schema_migrations_table(self) -> None:
        with self._hub.transaction() as txn:
            txn.execute(_SCHEMA_MIGRATIONS_TABLE_SQL)

    def _read_applied_versions(self) -> set[int]:
        with self._hub.transaction() as txn:
            rows = txn.execute("SELECT version FROM schema_migrations").fetchall()
        return {int(_row_value(row, "version")) for row in rows}

    def _discover_migrations(self) -> list[Migration]:
        migrations_dir = importlib.resources.files("gobby.storage").joinpath("migrations")
        if not migrations_dir.is_dir():
            return []

        grouped: dict[int, dict[str, Traversable | str | None]] = {}
        for path in migrations_dir.iterdir():
            if not path.is_file():
                continue
            match = _MIGRATION_FILE_RE.match(path.name)
            if match is None:
                continue

            version = int(match.group("version"))
            name = match.group("name")
            dialect = match.group("dialect")
            slot = "shared_path" if dialect is None else f"{dialect}_path"
            entry = grouped.setdefault(
                version,
                {"name": name, "shared_path": None, "sqlite_path": None, "postgres_path": None},
            )
            if entry[slot] is not None:
                raise RuntimeError(f"Duplicate migration file for v{version} ({slot})")
            if entry["name"] != name:
                raise RuntimeError(f"Conflicting migration names for v{version}")
            entry[slot] = path

        return [
            Migration(
                version=version,
                name=str(entry["name"]),
                shared_path=_as_traversable(entry["shared_path"]),
                sqlite_path=_as_traversable(entry["sqlite_path"]),
                postgres_path=_as_traversable(entry["postgres_path"]),
            )
            for version, entry in sorted(grouped.items())
        ]

    def _run_migration(self, txn: Transaction, migration: Migration) -> None:
        path = migration.path_for_dialect(self._hub.dialect)
        _execute_sql_script(txn, path.read_text())

    def _record_applied_version(self, txn: Transaction, version: int) -> None:
        if self._hub.dialect == "postgres":
            applied_at_sql = "NOW()"
        else:
            applied_at_sql = "CURRENT_TIMESTAMP"
        txn.execute(
            f"INSERT INTO schema_migrations(version, applied_at) VALUES ($1, {applied_at_sql})",
            (version,),
        )


def _as_traversable(value: Traversable | str | None) -> Traversable | None:
    if value is None:
        return None
    if isinstance(value, str):
        raise TypeError(f"expected Traversable, got {value!r}")
    return value


def _split_statements_respecting_dollar_quotes(sql: str) -> Iterator[str]:
    """Split SQL statements while preserving strings, comments, and dollar bodies."""
    statement_start = 0
    i = 0
    n = len(sql)

    while i < n:
        char = sql[i]

        if char == "-" and i + 1 < n and sql[i + 1] == "-":
            i = _skip_line_comment(sql, i)
            continue

        if char == "/" and i + 1 < n and sql[i + 1] == "*":
            i = _skip_block_comment(sql, i)
            continue

        if char == "'":
            i = _skip_single_quoted_string(sql, i)
            continue

        if char == '"':
            i = _skip_double_quoted_identifier(sql, i)
            continue

        if char == "$":
            tag = _dollar_quote_tag_at(sql, i)
            if tag is not None:
                close = sql.find(tag, i + len(tag))
                if close < 0:
                    raise ValueError(f"unterminated dollar-quote tag {tag!r}")
                i = close + len(tag)
                continue

        if char == ";":
            statement = sql[statement_start:i]
            yield statement
            statement_start = i + 1

        i += 1

    tail = sql[statement_start:]
    if tail:
        yield tail


def _skip_line_comment(sql: str, start: int) -> int:
    end = sql.find("\n", start + 2)
    return len(sql) if end < 0 else end + 1


def _skip_block_comment(sql: str, start: int) -> int:
    i = start + 2
    depth = 1
    n = len(sql)
    while i < n and depth:
        if i + 1 < n and sql[i] == "/" and sql[i + 1] == "*":
            depth += 1
            i += 2
            continue
        if i + 1 < n and sql[i] == "*" and sql[i + 1] == "/":
            depth -= 1
            i += 2
            continue
        i += 1
    return i


def _skip_single_quoted_string(sql: str, start: int) -> int:
    i = start + 1
    n = len(sql)
    while i < n:
        if sql[i] == "'":
            if i + 1 < n and sql[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return i


def _skip_double_quoted_identifier(sql: str, start: int) -> int:
    i = start + 1
    n = len(sql)
    while i < n:
        if sql[i] == '"':
            if i + 1 < n and sql[i + 1] == '"':
                i += 2
                continue
            return i + 1
        i += 1
    return i


def _dollar_quote_tag_at(sql: str, start: int) -> str | None:
    if start > 0 and _is_identifier_continuation(sql[start - 1]):
        return None
    if start + 1 >= len(sql):
        return None
    if sql[start + 1] == "$":
        return "$$"
    if not _is_identifier_start(sql[start + 1]):
        return None

    tag_end = start + 2
    while tag_end < len(sql) and _is_identifier_continuation(sql[tag_end]):
        tag_end += 1
    if tag_end < len(sql) and sql[tag_end] == "$":
        return sql[start : tag_end + 1]
    return None


def _is_identifier_start(char: str) -> bool:
    return char.isalpha() or char == "_"


def _is_identifier_continuation(char: str) -> bool:
    return char.isalnum() or char == "_"


def _execute_sql_script(executor: _TransactionLike, sql: str) -> None:
    for statement in _split_statements_respecting_dollar_quotes(sql):
        if statement.strip():
            executor.execute(statement)


def _row_value(row: Any, key: str, index: int = 0) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return row[index]


def _table_exists(txn: _TransactionLike, dialect: str, table: str) -> bool:
    if dialect == "postgres":
        row = txn.execute("SELECT to_regclass($1) IS NOT NULL AS table_exists", (table,)).fetchone()
        return bool(row is not None and _row_value(row, "table_exists"))

    row = txn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = $table",
        {"table": table},
    ).fetchone()
    return row is not None


def _migrate_bookkeeping_table(db: Any) -> None:
    """Idempotently rename ``schema_version`` to ``schema_migrations``."""
    dialect = getattr(db, "dialect", "sqlite")
    with _transaction(db) as txn:
        has_old = _table_exists(txn, dialect, "schema_version")
        has_new = _table_exists(txn, dialect, "schema_migrations")

        if has_new and not has_old:
            return

        if has_old and not has_new:
            txn.execute("ALTER TABLE schema_version RENAME TO schema_migrations")
            return

        if has_old and has_new:
            old_versions = _version_set(txn, "schema_version")
            new_versions = _version_set(txn, "schema_migrations")
            if old_versions == new_versions:
                txn.execute("DROP TABLE schema_version")
                return
            raise MigrationUnsupportedError(
                "Both schema_version and schema_migrations exist with divergent rows. "
                "This indicates a corrupted PostgreSQL migration bookkeeping state; restore "
                "the PostgreSQL hub database from a known-good backup before continuing."
            )


@contextmanager
def _transaction(db: Any) -> Iterator[_TransactionLike]:
    with db.transaction() as txn:
        yield txn


def _version_set(txn: _TransactionLike, table: str) -> set[int]:
    rows = txn.execute(f"SELECT version FROM {table}").fetchall()
    return {int(_row_value(row, "version")) for row in rows}


def get_current_version(db: LocalDatabase) -> int:
    """Get current schema version from either bookkeeping table."""
    for table in ("schema_migrations", "schema_version"):
        try:
            row = db.fetchone(f"SELECT MAX(version) as version FROM {table}")
            return row["version"] if row and row["version"] else 0
        except Exception:
            continue
    return 0


def latest_known_version() -> int:
    """Return the newest schema version known to this build."""
    _ensure_no_legacy_migration_entries()
    return BASELINE_VERSION


def migrations_needed(db: LocalDatabase) -> bool:
    """Return whether a legacy SQLite database predates the import-supported version."""
    current_version = get_current_version(db)
    if current_version == 0 or current_version < _MIN_MIGRATION_VERSION:
        return True
    return current_version < latest_known_version()


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
                        _execute_sql_script(db, action)
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


def _run_test_sqlite_baseline(db: LocalDatabase, schema_path: Path) -> int:
    """Apply the fixture-only SQLite baseline used by legacy LocalDatabase tests."""
    current_version = get_current_version(db)
    if current_version >= BASELINE_VERSION:
        _run_test_sqlite_startup_repairs(db)
        return 0
    if current_version != 0:
        raise MigrationUnsupportedError(
            "SQLite test baseline can only initialize an empty test database."
        )

    schema_sql = schema_path.read_text(encoding="utf-8")
    with db.transaction() as conn:
        conn.executescript(schema_sql)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (BASELINE_VERSION,))
    return 1


def _run_test_sqlite_startup_repairs(db: LocalDatabase) -> None:
    """Run lightweight repair steps expected by legacy SQLite tests."""
    from gobby.storage.auth import repair_legacy_sqlite_auth_sessions

    repair_legacy_sqlite_auth_sessions(db)

    table = db.fetchone(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("task_dispatch_mutex",),
    )
    if table is None:
        return

    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

    try:
        TaskDispatchMutexManager(db).sweep_expired()
    except Exception:
        logger.exception("Failed to sweep expired task dispatch mutex rows during startup repair")


def run_migrations(db: LocalDatabase) -> int:
    """Reject runtime SQLite migrations after the PostgreSQL cutover."""
    if os.environ.get("GOBBY_TEST_PROTECT") == "1":
        test_schema = os.environ.get("GOBBY_SQLITE_TEST_SCHEMA_PATH")
        if test_schema:
            return _run_test_sqlite_baseline(db, Path(test_schema))

    _ = db
    raise MigrationUnsupportedError(
        "SQLite runtime migrations were removed. Use `gobby postgres migrate-from-sqlite` "
        "to import a legacy SQLite database into PostgreSQL."
    )
