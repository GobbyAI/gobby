"""PostgreSQL hub migrations.

Authoring rules for new migration files (enforced in part by
tests/storage/test_migration_contract.py):

1. Every migration must apply cleanly against BOTH an existing populated
   pre-migration schema AND a fresh baseline that already contains the
   change (fresh installs replay all on-disk migrations on top of the
   current baseline). Statements that only type-check against one shape
   (e.g. a text-expression UPDATE on a column the baseline already made
   uuid) must be wrapped in a column-type-guarded PL/pgSQL ``DO`` block —
   PL/pgSQL only type-checks statements it actually reaches.
2. Any data-dependent cast (``ALTER ... TYPE UUID USING col::UUID`` and
   friends) must be preceded by a preflight ``DO`` block that scans for
   uncastable values and ``RAISE EXCEPTION`` with the offending
   column names and counts. A bare cast failure reports only the value —
   preflights make the failure diagnosable and, critically, they fail on
   POPULATED databases that migration tests (which run against fresh,
   empty schemas) can never exercise. Migration 304's bare
   ``caller_symbol_id::UUID`` cast took the daemon down twice for exactly
   this reason; ``305_uuid_completion.postgres.sql`` (now folded into the
   baseline — see git history) carries the reference preflight pattern.
3. Optional text-uuid columns that used ``''`` sentinels convert with
   ``USING NULLIF(col::TEXT, '')::UUID`` after dropping NOT NULL.
4. Migration versions are append-only. Versions at or below
   ``BASELINE_VERSION`` remain permanently reserved after flattening; new
   files must use a unique version above that high-water mark.
"""

from __future__ import annotations

import importlib.resources
import logging
import re
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from typing import Any, Protocol

from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

__all__ = [
    "BASELINE_VERSION",
    "Migration",
    "MigrationRunner",
    "MigrationUnsupportedError",
    "_split_statements_respecting_dollar_quotes",
    "latest_known_version",
]


class MigrationUnsupportedError(Exception):
    """Raised when database version is too old or bookkeeping is corrupt."""


BASELINE_VERSION = 305


_MIGRATION_FILE_RE = re.compile(r"^(?P<version>\d+)_(?P<name>.+?)(?:\.postgres)?\.sql$")
_SCHEMA_MIGRATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL
)
"""


class _TransactionLike(Protocol):
    def execute(self, sql: str, params: Any = ()) -> Any: ...


class _AutocommitConnection(_TransactionLike, Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Traversable


_NON_TRANSACTIONAL_DIRECTIVE = "-- gobby:non-transactional"
_MIGRATION_LOCK_SQL = "hashtext('postgres_migrations_apply'), hashtext(current_schema())"
_SQL_IDENTIFIER_PATTERN = r'(?:[A-Za-z_][A-Za-z0-9_$]*|"(?:[^"]|"")+")'
_CONCURRENT_INDEX_RE = re.compile(
    rf"CREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY\s+"
    rf"(?:IF\s+NOT\s+EXISTS\s+)?"
    rf"(?P<index_name>{_SQL_IDENTIFIER_PATTERN}(?:\s*\.\s*{_SQL_IDENTIFIER_PATTERN})?)"
    r"\s+ON\b",
    re.IGNORECASE,
)
_INVALID_CONCURRENT_INDEX_SQL = """
SELECT pg_catalog.format('%I.%I', namespace.nspname, relation.relname) AS qualified_name
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
JOIN pg_catalog.pg_index AS index_state ON index_state.indexrelid = relation.oid
WHERE relation.oid = to_regclass(%s)
  AND NOT index_state.indisvalid
"""


class MigrationRunner:
    """PostgreSQL file-based migration runner."""

    def __init__(
        self,
        hub: HubDatabase,
        *,
        autocommit_connection: Callable[[], _AutocommitConnection] | None = None,
    ) -> None:
        if hub.dialect != "postgres":
            raise MigrationUnsupportedError(
                "MigrationRunner only supports PostgreSQL hub databases."
            )
        self._hub = hub
        self._autocommit_connection = autocommit_connection

    def apply_pending(self) -> None:
        self._ensure_schema_migrations_table()
        applied = self._read_applied_versions()
        for migration in self._discover_migrations():
            if migration.version in applied:
                continue
            if self._is_non_transactional(migration):
                self._apply_non_transactional(migration)
                applied.add(migration.version)
                continue
            with self._hub.transaction() as txn:
                txn.execute(f"SELECT pg_advisory_xact_lock({_MIGRATION_LOCK_SQL})")
                row = txn.execute(
                    "SELECT version FROM schema_migrations WHERE version = %s",
                    (migration.version,),
                ).fetchone()
                if row is not None:
                    applied.add(migration.version)
                    continue
                logger.info(
                    "Applying PostgreSQL migration",
                    extra={
                        "migration_name": migration.name,
                        "migration_version": migration.version,
                    },
                )
                self._run_migration(txn, migration)
                self._record_applied_version(txn, migration.version)
            applied.add(migration.version)

    def _apply_non_transactional(self, migration: Migration) -> None:
        if self._autocommit_connection is None:
            raise MigrationUnsupportedError(
                "Non-transactional migration requires an autocommit connection."
            )
        with closing(self._autocommit_connection()) as connection:
            connection.execute(f"SELECT pg_advisory_lock({_MIGRATION_LOCK_SQL})")
            try:
                row = connection.execute(
                    "SELECT version FROM schema_migrations WHERE version = %s",
                    (migration.version,),
                ).fetchone()
                if row is not None:
                    return
                logger.info(
                    "Applying non-transactional PostgreSQL migration",
                    extra={
                        "migration_name": migration.name,
                        "migration_version": migration.version,
                    },
                )
                self._repair_invalid_concurrent_indexes(connection, migration)
                self._run_migration(connection, migration)
                self._record_applied_version(connection, migration.version)
            finally:
                connection.execute(f"SELECT pg_advisory_unlock({_MIGRATION_LOCK_SQL})")

    def _repair_invalid_concurrent_indexes(
        self,
        connection: _AutocommitConnection,
        migration: Migration,
    ) -> None:
        for index_name in _concurrent_index_names(migration.path.read_text()):
            row = connection.execute(
                _INVALID_CONCURRENT_INDEX_SQL,
                (index_name,),
            ).fetchone()
            if row is None:
                continue
            qualified_name = _row_value(row, "qualified_name")
            if not isinstance(qualified_name, str) or not qualified_name:
                raise MigrationUnsupportedError(
                    f"Invalid catalog identifier returned while repairing index {index_name!r}"
                )
            logger.warning(
                "Dropping invalid PostgreSQL index before migration retry",
                extra={
                    "migration_name": migration.name,
                    "migration_version": migration.version,
                    "index_name": index_name,
                },
            )
            # PostgreSQL format('%I.%I', ...) produced this catalog identifier.
            connection.execute(  # noqa: S608
                f"DROP INDEX CONCURRENTLY IF EXISTS {qualified_name}"
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

        grouped: dict[int, Migration] = {}
        for path in migrations_dir.iterdir():
            if not path.is_file():
                continue
            match = _MIGRATION_FILE_RE.match(path.name)
            if match is None:
                if not path.name.startswith("."):
                    logger.warning(
                        "Ignoring invalid migration filename",
                        extra={"migration_filename": path.name},
                    )
                continue

            version = int(match.group("version"))
            name = match.group("name")
            if version <= BASELINE_VERSION:
                raise RuntimeError(
                    f"Migration v{version} reuses a version reserved by "
                    f"baseline v{BASELINE_VERSION}"
                )
            existing = grouped.get(version)
            if existing is not None:
                raise RuntimeError(f"Duplicate migration file for v{version}")
            grouped[version] = Migration(version=version, name=name, path=path)

        return [migration for _version, migration in sorted(grouped.items())]

    @staticmethod
    def _is_non_transactional(migration: Migration) -> bool:
        first_line = migration.path.read_text().splitlines()[0].strip()
        return first_line == _NON_TRANSACTIONAL_DIRECTIVE

    def _run_migration(self, txn: _TransactionLike, migration: Migration) -> None:
        _execute_sql_script(txn, migration.path.read_text())

    def _record_applied_version(self, txn: _TransactionLike, version: int) -> None:
        txn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (%s, NOW())",
            (version,),
        )


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
            prefix = sql[i - 1] if i > 0 else ""
            escape_backslashes = (
                bool(prefix)
                and prefix in "eE"
                and (i < 2 or not _is_identifier_continuation(sql[i - 2]))
            )
            i = _skip_single_quoted_string(sql, i, escape_backslashes=escape_backslashes)
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


def _statement_body(statement: str) -> str:
    position = 0
    while position < len(statement):
        while position < len(statement) and statement[position].isspace():
            position += 1
        if statement.startswith("--", position):
            position = _skip_line_comment(statement, position)
            continue
        if statement.startswith("/*", position):
            position = _skip_block_comment(statement, position)
            continue
        break
    return statement[position:]


def _concurrent_index_names(sql: str) -> tuple[str, ...]:
    names: list[str] = []
    for statement in _split_statements_respecting_dollar_quotes(sql):
        match = _CONCURRENT_INDEX_RE.match(_statement_body(statement))
        if match is None:
            continue
        index_name = re.sub(r"\s*\.\s*", ".", match.group("index_name"))
        if index_name not in names:
            names.append(index_name)
    return tuple(names)


def _skip_single_quoted_string(
    sql: str,
    start: int,
    *,
    escape_backslashes: bool = False,
) -> int:
    i = start + 1
    n = len(sql)
    while i < n:
        if escape_backslashes and sql[i] == "\\":
            i += 2
            continue
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


def latest_known_version() -> int:
    """Return the newest schema version known to this build."""
    versions = [BASELINE_VERSION]
    migrations_dir = importlib.resources.files("gobby.storage").joinpath("migrations")
    if migrations_dir.is_dir():
        for path in migrations_dir.iterdir():
            if not path.is_file():
                continue
            match = _MIGRATION_FILE_RE.match(path.name)
            if match is not None:
                versions.append(int(match.group("version")))
    return max(versions)
