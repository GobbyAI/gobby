"""PostgreSQL hub migrations."""

from __future__ import annotations

import importlib.resources
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from typing import Any, Protocol

from gobby.storage.hub.protocol import HubDatabase, Transaction

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


BASELINE_VERSION = 261


_MIGRATION_FILE_RE = re.compile(r"^(?P<version>\d+)_(?P<name>.+?)(?:\.postgres)?\.sql$")
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
    path: Traversable


class MigrationRunner:
    """PostgreSQL file-based migration runner."""

    def __init__(self, hub: HubDatabase) -> None:
        if hub.dialect != "postgres":
            raise MigrationUnsupportedError(
                "MigrationRunner only supports PostgreSQL hub databases."
            )
        self._hub = hub

    def apply_pending(self) -> None:
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

        grouped: dict[int, Migration] = {}
        for path in migrations_dir.iterdir():
            if not path.is_file():
                continue
            match = _MIGRATION_FILE_RE.match(path.name)
            if match is None:
                continue

            version = int(match.group("version"))
            name = match.group("name")
            existing = grouped.get(version)
            if existing is not None:
                raise RuntimeError(f"Duplicate migration file for v{version}")
            grouped[version] = Migration(version=version, name=name, path=path)

        return [migration for _version, migration in sorted(grouped.items())]

    def _run_migration(self, txn: Transaction, migration: Migration) -> None:
        _execute_sql_script(txn, migration.path.read_text())

    def _record_applied_version(self, txn: Transaction, version: int) -> None:
        txn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES ($1, NOW())",
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


def latest_known_version() -> int:
    """Return the newest schema version known to this build."""
    return BASELINE_VERSION
