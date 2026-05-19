"""Database migrations for local storage and hub backends.

The legacy ``run_migrations(LocalDatabase)`` entry point is still used by older
SQLite-only call sites. ``MigrationRunner`` is the backend-neutral runner used by
hub adapters and records applied versions in ``schema_migrations``.
"""

from __future__ import annotations

import importlib.resources
import logging
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
    "BASELINE_SCHEMA",
    "MIGRATIONS",
    "Migration",
    "MigrationAction",
    "MigrationRunner",
    "MigrationUnsupportedError",
    "_apply_baseline",
    "_migrate_bookkeeping_table",
    "_run_migration_list",
    "_split_statements_respecting_dollar_quotes",
    "get_current_version",
    "latest_known_version",
    "migrations_needed",
    "run_migrations",
]


class MigrationUnsupportedError(Exception):
    """Raised when database version is too old or bookkeeping is corrupt."""


MigrationAction = str | Callable[[LocalDatabase], None]

BASELINE_VERSION = 260
# Historical SQLite migration bands through v260 are flattened into the baseline.
# Databases below v260 must use an older Gobby build or manual recovery.
_MIN_MIGRATION_VERSION = 260
BASELINE_SCHEMA = (Path(__file__).parent / "baseline_schema.sql").read_text()


# The current SQLite baseline includes all historical schema changes through v260.
# Keep the generic runner helpers below for future migrations.
MIGRATIONS: list[tuple[int, str, MigrationAction]] = []

_LEGACY_SESSIONS_UNIQUE_COLUMNS = ("external_id", "machine_id", "source", "project_id")
_CURRENT_SESSIONS_UNIQUE_COLUMNS = (*_LEGACY_SESSIONS_UNIQUE_COLUMNS, "session_type")
_MIGRATION_FILE_RE = re.compile(
    r"^(?P<version>\d+)_(?P<name>.+?)(?:\.(?P<dialect>sqlite|postgres))?\.sql$"
)
_SCHEMA_VERSION_CREATE_RE = re.compile(r"^\s*CREATE\s+TABLE\s+schema_version\b", re.I)
_SQLITE_TRIGGER_START_RE = re.compile(
    r"^\s*CREATE\s+(?:TEMP\s+|TEMPORARY\s+)?TRIGGER\b", re.I | re.S
)
_SQLITE_TRIGGER_BEGIN_RE = re.compile(r"\bBEGIN\b", re.I)
_SQLITE_TRIGGER_END_RE = re.compile(r"\bEND\s*$", re.I | re.S)

_SCHEMA_MIGRATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL
)
"""

_SQLITE_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS code_symbols_fts USING fts5(
    name, qualified_name, signature, docstring, summary,
    content='code_symbols', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS code_symbols_ai AFTER INSERT ON code_symbols BEGIN
    INSERT INTO code_symbols_fts(rowid, name, qualified_name, signature, docstring, summary)
    VALUES (new.rowid, new.name, new.qualified_name, new.signature, new.docstring, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS code_symbols_ad AFTER DELETE ON code_symbols BEGIN
    INSERT INTO code_symbols_fts(
        code_symbols_fts, rowid, name, qualified_name, signature, docstring, summary
    )
    VALUES (
        'delete', old.rowid, old.name, old.qualified_name, old.signature,
        old.docstring, old.summary
    );
END;

CREATE TRIGGER IF NOT EXISTS code_symbols_au AFTER UPDATE ON code_symbols BEGIN
    INSERT INTO code_symbols_fts(
        code_symbols_fts, rowid, name, qualified_name, signature, docstring, summary
    )
    VALUES (
        'delete', old.rowid, old.name, old.qualified_name, old.signature,
        old.docstring, old.summary
    );
    INSERT INTO code_symbols_fts(rowid, name, qualified_name, signature, docstring, summary)
    VALUES (new.rowid, new.name, new.qualified_name, new.signature, new.docstring, new.summary);
END;

INSERT OR IGNORE INTO code_symbols_fts(rowid, name, qualified_name, signature, docstring, summary)
SELECT rowid, name, qualified_name, signature, docstring, summary FROM code_symbols;

CREATE VIRTUAL TABLE IF NOT EXISTS code_content_fts USING fts5(
    content, file_path, language,
    content='code_content_chunks', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS code_content_ai AFTER INSERT ON code_content_chunks BEGIN
    INSERT INTO code_content_fts(rowid, content, file_path, language)
    VALUES (new.rowid, new.content, new.file_path, new.language);
END;

CREATE TRIGGER IF NOT EXISTS code_content_ad AFTER DELETE ON code_content_chunks BEGIN
    INSERT INTO code_content_fts(code_content_fts, rowid, content, file_path, language)
    VALUES ('delete', old.rowid, old.content, old.file_path, old.language);
END;

CREATE TRIGGER IF NOT EXISTS code_content_au AFTER UPDATE ON code_content_chunks BEGIN
    INSERT INTO code_content_fts(code_content_fts, rowid, content, file_path, language)
    VALUES ('delete', old.rowid, old.content, old.file_path, old.language);
    INSERT INTO code_content_fts(rowid, content, file_path, language)
    VALUES (new.rowid, new.content, new.file_path, new.language);
END;

INSERT OR IGNORE INTO code_content_fts(rowid, content, file_path, language)
SELECT rowid, content, file_path, language FROM code_content_chunks;

CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
    title, description, labels, task_type, category,
    content='tasks', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS tasks_fts_ai AFTER INSERT ON tasks BEGIN
    INSERT INTO tasks_fts(rowid, title, description, labels, task_type, category)
    VALUES (new.rowid, new.title, new.description, new.labels, new.task_type, new.category);
END;

CREATE TRIGGER IF NOT EXISTS tasks_fts_ad AFTER DELETE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, description, labels, task_type, category)
    VALUES ('delete', old.rowid, old.title, old.description, old.labels, old.task_type, old.category);
END;

CREATE TRIGGER IF NOT EXISTS tasks_fts_au AFTER UPDATE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, description, labels, task_type, category)
    VALUES ('delete', old.rowid, old.title, old.description, old.labels, old.task_type, old.category);
    INSERT INTO tasks_fts(rowid, title, description, labels, task_type, category)
    VALUES (new.rowid, new.title, new.description, new.labels, new.task_type, new.category);
END;

INSERT OR IGNORE INTO tasks_fts(rowid, title, description, labels, task_type, category)
SELECT rowid, title, description, labels, task_type, category FROM tasks;

CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
    name, description, tags_text, category,
    content='', content_rowid='rowid'
);

INSERT OR IGNORE INTO skills_fts(rowid, name, description, tags_text, category)
SELECT rowid, name, description,
       COALESCE(json_extract(metadata, '$.skillport.tags'), ''),
       COALESCE(
           json_extract(metadata, '$.skillport.category'),
           json_extract(metadata, '$.category'),
           ''
       )
FROM skills WHERE deleted_at IS NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, tags, memory_type, source_type,
    content='memories', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, tags, memory_type, source_type)
    VALUES (
        new.rowid, new.content,
        REPLACE(REPLACE(REPLACE(COALESCE(new.tags, ''), '"', ''), '[', ''), ']', ''),
        new.memory_type, COALESCE(new.source_type, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags, memory_type, source_type)
    VALUES (
        'delete', old.rowid, old.content,
        REPLACE(REPLACE(REPLACE(COALESCE(old.tags, ''), '"', ''), '[', ''), ']', ''),
        old.memory_type, COALESCE(old.source_type, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS memories_fts_au
AFTER UPDATE OF content, tags, memory_type, source_type ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags, memory_type, source_type)
    VALUES (
        'delete', old.rowid, old.content,
        REPLACE(REPLACE(REPLACE(COALESCE(old.tags, ''), '"', ''), '[', ''), ']', ''),
        old.memory_type, COALESCE(old.source_type, '')
    );
    INSERT INTO memories_fts(rowid, content, tags, memory_type, source_type)
    VALUES (
        new.rowid, new.content,
        REPLACE(REPLACE(REPLACE(COALESCE(new.tags, ''), '"', ''), '[', ''), ']', ''),
        new.memory_type, COALESCE(new.source_type, '')
    );
END;

INSERT OR IGNORE INTO memories_fts(rowid, content, tags, memory_type, source_type)
SELECT rowid, content,
       REPLACE(REPLACE(REPLACE(COALESCE(tags, ''), '"', ''), '[', ''), ']', ''),
       memory_type, COALESCE(source_type, '')
FROM memories;
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
        _migrate_bookkeeping_table(self._hub)
        if self._hub.dialect == "sqlite":
            self._apply_sqlite_baseline_if_needed()
        self._ensure_schema_migrations_table()
        applied = self._read_applied_versions()
        for migration in self._discover_migrations():
            if migration.version in applied:
                continue
            with self._hub.transaction() as txn:
                self._run_migration(txn, migration)
                self._record_applied_version(txn, migration.version)
            applied.add(migration.version)

    def _apply_sqlite_baseline_if_needed(self) -> None:
        with self._hub.transaction() as txn:
            if _table_exists(txn, "sqlite", "schema_migrations"):
                return
            if _table_exists(txn, "sqlite", "schema_version"):
                return
            if _has_sqlite_application_tables(txn):
                raise MigrationUnsupportedError(
                    "SQLite database has application tables but no schema_migrations "
                    "or schema_version bookkeeping table; restore ~/.gobby/gobby-hub.db "
                    "from a backup before continuing."
                )

            logger.info("Applying SQLite hub baseline (v%s)", BASELINE_VERSION)
            _execute_sql_script(txn, _sqlite_baseline_sql("schema_migrations"))
            txn.execute(
                "INSERT INTO schema_migrations (version, applied_at) "
                "VALUES ($1, CURRENT_TIMESTAMP)",
                (BASELINE_VERSION,),
            )

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
            if _is_incomplete_sqlite_trigger(statement):
                i += 1
                continue
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


def _is_incomplete_sqlite_trigger(statement: str) -> bool:
    body = _strip_leading_sql_comments(statement)
    if not _SQLITE_TRIGGER_START_RE.match(body):
        return False
    if not _SQLITE_TRIGGER_BEGIN_RE.search(body):
        return False
    return not bool(_SQLITE_TRIGGER_END_RE.search(body.rstrip()))


def _strip_leading_sql_comments(sql: str) -> str:
    i = 0
    n = len(sql)
    while i < n:
        while i < n and sql[i].isspace():
            i += 1
        if i + 1 < n and sql[i] == "-" and sql[i + 1] == "-":
            i = _skip_line_comment(sql, i)
            continue
        if i + 1 < n and sql[i] == "/" and sql[i + 1] == "*":
            i = _skip_block_comment(sql, i)
            continue
        break
    return sql[i:]


def _execute_sql_script(executor: _TransactionLike, sql: str) -> None:
    for statement in _split_statements_respecting_dollar_quotes(sql):
        if statement.strip():
            executor.execute(statement)


def _sqlite_baseline_sql(version_table: str) -> str:
    statements: list[str] = []
    for statement in _split_statements_respecting_dollar_quotes(BASELINE_SCHEMA):
        if _SCHEMA_VERSION_CREATE_RE.match(statement):
            if version_table == "schema_migrations":
                statements.append(_SCHEMA_MIGRATIONS_TABLE_SQL)
            else:
                statements.append(statement)
        else:
            statements.append(statement)
    statements.extend(_split_statements_respecting_dollar_quotes(_SQLITE_FTS_SCHEMA))
    return ";\n".join(statement for statement in statements if statement.strip())


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


def _has_sqlite_application_tables(txn: _TransactionLike) -> bool:
    row = txn.execute(
        """
        SELECT name
          FROM sqlite_master
         WHERE type = 'table'
           AND name NOT LIKE 'sqlite_%'
           AND name NOT IN ('schema_version', 'schema_migrations')
         LIMIT 1
        """
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
                "This indicates a corrupted bookkeeping state; restore ~/.gobby/gobby-hub.db "
                "from a backup before continuing."
            )


@contextmanager
def _transaction(db: Any) -> Iterator[_TransactionLike]:
    with db.transaction() as txn:
        yield txn


def _version_set(txn: _TransactionLike, table: str) -> set[int]:
    rows = txn.execute(f"SELECT version FROM {table}").fetchall()
    return {int(_row_value(row, "version")) for row in rows}


def _sqlite_index_columns(db: LocalDatabase, index_name: str) -> tuple[str, ...]:
    rows = db.fetchall("SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index_name,))
    return tuple(row["name"] for row in rows)


def _quote_sqlite_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _repair_sessions_unique_index(db: LocalDatabase) -> None:
    """Drop the pre-session_type uniqueness constraint if an older DB still has it."""
    indexes = db.fetchall("PRAGMA index_list(sessions)")
    legacy_index_names: list[str] = []
    has_current_unique = False

    for row in indexes:
        if not row["unique"]:
            continue
        index_name = row["name"]
        columns = _sqlite_index_columns(db, index_name)
        if columns == _CURRENT_SESSIONS_UNIQUE_COLUMNS:
            has_current_unique = True
        elif columns == _LEGACY_SESSIONS_UNIQUE_COLUMNS:
            origin = row["origin"] if "origin" in row.keys() else "c"
            if origin == "c":
                legacy_index_names.append(index_name)
            else:
                logger.warning(
                    "Legacy sessions uniqueness is backed by non-droppable SQLite index %s",
                    index_name,
                )

    if not legacy_index_names and has_current_unique:
        return

    with db.transaction():
        for index_name in legacy_index_names:
            db.execute(f"DROP INDEX IF EXISTS {_quote_sqlite_identifier(index_name)}")
            logger.info("Dropped legacy sessions unique index %s", index_name)

        if not has_current_unique:
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_unique
                ON sessions(external_id, machine_id, source, project_id, session_type)
                """
            )
            logger.info("Ensured sessions unique index includes session_type")


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
    """Apply baseline schema for new legacy SQLite databases."""
    logger.info("Applying baseline schema (v%s)", BASELINE_VERSION)

    with db.transaction() as conn:
        _execute_sql_script(conn, _sqlite_baseline_sql("schema_version"))
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (BASELINE_VERSION,),
        )

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


def _run_sqlite_startup_repairs(db: LocalDatabase) -> None:
    _repair_sessions_unique_index(db)

    from gobby.storage.sessions import ensure_system_session
    from gobby.storage.tasks import TaskDispatchMutexManager

    ensure_system_session(db)
    TaskDispatchMutexManager(db).sweep_expired()


def run_migrations(db: LocalDatabase) -> int:
    """
    Run pending legacy SQLite migrations.

    For new databases:
        - Applies the current baseline schema directly.

    For existing databases:
        - Versions below _MIN_MIGRATION_VERSION raise MigrationUnsupportedError.
        - Versions at or above _MIN_MIGRATION_VERSION run future SQLite migrations.
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

    _run_sqlite_startup_repairs(db)

    return total_applied
