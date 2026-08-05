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

import hashlib
import importlib.resources
import json
import logging
import re
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from typing import Any, Protocol

from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

__all__ = [
    "BASELINE_VERSION",
    "DestructiveMigrationContext",
    "Migration",
    "MigrationRunner",
    "MigrationUnsupportedError",
    "MIGRATION_LOCK_SQL",
    "_split_statements_respecting_dollar_quotes",
    "baseline_checksum",
    "latest_known_version",
]


class MigrationUnsupportedError(Exception):
    """Raised when database version is too old or bookkeeping is corrupt."""


BASELINE_VERSION = 375


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


@dataclass(frozen=True)
class DestructiveMigrationContext:
    """Verified backup and maintenance-epoch facts for one gated batch."""

    epoch_id: str
    batch_id: str
    manifest_sha256: str
    backup_starting_head: int


_NON_TRANSACTIONAL_DIRECTIVE = "-- gobby:non-transactional"
_DESTRUCTIVE_DIRECTIVE = "-- gobby:destructive"
_BOOKKEEPING_VERSION = 354
MIGRATION_LOCK_SQL = "hashtext('postgres_migrations_apply'), hashtext(current_schema())"
_SQL_IDENTIFIER_PATTERN = r'(?:[A-Za-z_][A-Za-z0-9_$]*|"(?:[^"]|"")+")'
_CONCURRENT_INDEX_RE = re.compile(
    rf"CREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY\s+"
    rf"(?:IF\s+NOT\s+EXISTS\s+)?"
    rf"(?P<index_name>{_SQL_IDENTIFIER_PATTERN}(?:\s*\.\s*{_SQL_IDENTIFIER_PATTERN})?)"
    r"\s+ON\b",
    re.IGNORECASE,
)
_INVALID_CONCURRENT_INDEX_SQL = """
SELECT pg_catalog.format('%%I.%%I', namespace.nspname, relation.relname) AS qualified_name
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

    def apply_startup(
        self,
        *,
        baseline_already_applied: Callable[[], bool],
        apply_baseline: Callable[[], None],
    ) -> None:
        """Apply the complete startup schema decision under one session lock."""
        code_head = self._known_schema_version()
        with self._migration_lock():
            baseline_present = baseline_already_applied()
            if baseline_present:
                self._raise_if_schema_is_newer(self._read_current_schema_head(), code_head)
            else:
                apply_baseline()

            self._audit_migration_state()
            self._apply_pending_locked(fresh_schema=not baseline_present)
            database_head = self._read_current_schema_head()
            self._raise_if_schema_is_newer(database_head, code_head)
            logger.info(
                "PostgreSQL schema lockstep verified",
                extra={"code_schema_version": code_head, "database_schema_version": database_head},
            )

    def apply_pending(self, *, fresh_schema: bool = False) -> None:
        with self._migration_lock():
            self._audit_migration_state()
            self._apply_pending_locked(fresh_schema=fresh_schema)

    def _apply_pending_locked(self, *, fresh_schema: bool = False) -> None:
        self._ensure_schema_migrations_table()
        applied = self._read_applied_versions()
        migrations = self._discover_migrations()
        pending = [migration for migration in migrations if migration.version not in applied]
        self._validate_pending_chain(applied, pending)
        for migration in pending:
            if migration.version in applied:
                continue
            if self._is_destructive(migration) and not fresh_schema:
                raise MigrationUnsupportedError(
                    f"Migration {migration.path.name} is destructive and was not applied. "
                    "Run `gobby schema apply --destructive` inside an open maintenance epoch."
                )
            if self._is_non_transactional(migration):
                self._apply_non_transactional(migration)
                applied.add(migration.version)
                continue
            with self._hub.transaction() as txn:
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
                self._record_applied_version(txn, migration)
            applied.add(migration.version)

    @contextmanager
    def _migration_lock(self) -> Iterator[None]:
        if self._autocommit_connection is None:
            raise MigrationUnsupportedError(
                "PostgreSQL migration orchestration requires an autocommit connection."
            )
        with closing(self._autocommit_connection()) as connection:
            connection.execute(f"SELECT pg_advisory_lock({MIGRATION_LOCK_SQL})")
            try:
                yield
            finally:
                connection.execute(f"SELECT pg_advisory_unlock({MIGRATION_LOCK_SQL})")

    def _read_current_schema_head(self) -> int:
        with self._hub.transaction() as txn:
            return self._read_schema_head(txn)

    def _known_schema_version(self) -> int:
        return latest_known_version()

    @staticmethod
    def _raise_if_schema_is_newer(database_head: int, code_head: int) -> None:
        if database_head > code_head:
            raise MigrationUnsupportedError(
                f"hub schema is v{database_head} but this gobby build knows v{code_head} "
                "— update gobby on this machine."
            )

    def apply_destructive(self, context: DestructiveMigrationContext) -> None:
        """Apply or resume one hub-attested destructive migration batch."""
        if self._autocommit_connection is None:
            raise MigrationUnsupportedError(
                "Destructive migration apply requires a session advisory-lock connection."
            )
        with closing(self._autocommit_connection()) as lock_connection:
            lock_connection.execute(f"SELECT pg_advisory_lock({MIGRATION_LOCK_SQL})")
            try:
                self._audit_migration_state()
                migrations = self._discover_migrations()
                applied = self._read_applied_versions()
                pending = [
                    migration for migration in migrations if migration.version not in applied
                ]
                plan = self._load_or_create_batch_plan(
                    context,
                    migrations,
                    pending,
                    applied,
                )
                receipts = self._read_batch_progress(context, plan)
                for migration in self._remaining_batch_migrations(
                    migrations,
                    plan,
                    receipts,
                ):
                    if self._is_non_transactional(migration):
                        raise MigrationUnsupportedError(
                            f"Destructive batch migration {migration.path.name} is "
                            "non-transactional; atomic checksum bookkeeping is required."
                        )
                    self._apply_transactional_locked(migration)
            finally:
                lock_connection.execute(f"SELECT pg_advisory_unlock({MIGRATION_LOCK_SQL})")

    def _load_or_create_batch_plan(
        self,
        context: DestructiveMigrationContext,
        migrations: list[Migration],
        pending: list[Migration],
        applied: set[int],
    ) -> list[dict[str, str]]:
        with self._hub.transaction() as txn:
            row = txn.execute(
                """
                SELECT
                    batch.id,
                    batch.maintenance_epoch_id,
                    batch.campaign,
                    batch.status,
                    batch.backup_manifest_sha256,
                    batch.migration_plan,
                    batch.intent,
                    epoch.released_at,
                    epoch.opened_by,
                    epoch.campaign AS epoch_campaign
                FROM destructive_batches AS batch
                JOIN maintenance_epochs AS epoch
                  ON epoch.id = batch.maintenance_epoch_id
                WHERE batch.id = %s
                  AND batch.maintenance_epoch_id = %s
                FOR UPDATE OF batch
                """,
                (context.batch_id, context.epoch_id),
            ).fetchone()
            self._validate_batch_authority(row, context)
            assert row is not None

            stored_plan = _json_object_list(_row_value(row, "migration_plan", 5))
            intent = _json_object(_row_value(row, "intent", 6))
            if stored_plan:
                starting_head = intent.get("backup_starting_head")
                if starting_head != context.backup_starting_head:
                    raise MigrationUnsupportedError(
                        "Destructive batch backup starting head differs from its immutable intent."
                    )
                self._validate_local_batch_plan(stored_plan, migrations)
                return stored_plan

            self._validate_pending_chain(applied, pending)
            current_head = self._read_schema_head(txn)
            if current_head != context.backup_starting_head:
                raise MigrationUnsupportedError(
                    f"Backup starting head {context.backup_starting_head} does not match "
                    f"current head {current_head}."
                )
            plan = [_migration_plan_item(migration) for migration in pending]
            txn.execute(
                """
                UPDATE destructive_batches
                SET migration_plan = %s::jsonb,
                    intent = intent || jsonb_build_object('backup_starting_head', %s),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (json.dumps(plan, sort_keys=True), context.backup_starting_head, context.batch_id),
            )
            return plan

    @staticmethod
    def _validate_batch_authority(
        row: Any,
        context: DestructiveMigrationContext,
    ) -> None:
        if row is None:
            raise MigrationUnsupportedError(
                "Destructive batch does not belong to the requested maintenance epoch."
            )
        if _row_value(row, "released_at", 7) is not None:
            raise MigrationUnsupportedError("The maintenance epoch is not open.")
        campaign = _row_value(row, "campaign", 2)
        if _row_value(row, "epoch_campaign", 9) != campaign:
            raise MigrationUnsupportedError(
                "The maintenance epoch and destructive batch campaigns do not match."
            )
        if _row_value(row, "opened_by", 8) != f"hub-maintenance:{campaign}":
            raise MigrationUnsupportedError(
                f"The maintenance epoch is not owned by `hub-maintenance:{campaign}`."
            )
        if _row_value(row, "status", 3) != "pending":
            raise MigrationUnsupportedError("The destructive batch is not pending.")
        if _row_value(row, "backup_manifest_sha256", 4) != context.manifest_sha256:
            raise MigrationUnsupportedError(
                "The backup manifest digest does not match the destructive batch."
            )

    @staticmethod
    def _validate_local_batch_plan(
        stored_plan: list[dict[str, str]],
        migrations: list[Migration],
    ) -> None:
        local_by_version = {migration.version: migration for migration in migrations}
        try:
            local_plan = [
                _migration_plan_item(local_by_version[int(item["version"])]) for item in stored_plan
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise MigrationUnsupportedError(
                "Destructive batch references a migration missing from this runner."
            ) from exc
        if local_plan != stored_plan:
            raise MigrationUnsupportedError(
                "Destructive batch has different local migration bytes for the same version."
            )

    def _read_batch_progress(
        self,
        context: DestructiveMigrationContext,
        plan: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        versions = [int(item["version"]) for item in plan]
        with self._hub.transaction() as txn:
            rows = (
                txn.execute(
                    """
                    SELECT version, filename, checksum
                    FROM schema_migrations
                    WHERE version = ANY(%s)
                    ORDER BY version
                    """,
                    (versions,),
                ).fetchall()
                if versions
                else []
            )
            receipts = [
                {
                    "version": str(_row_value(row, "version", 0)),
                    "filename": str(_row_value(row, "filename", 1)),
                    "checksum": str(_row_value(row, "checksum", 2)),
                }
                for row in rows
            ]
            if receipts != plan[: len(receipts)]:
                raise MigrationUnsupportedError(
                    "Applied migration receipts are not an exact prefix of the destructive batch."
                )
            expected_head = (
                context.backup_starting_head if not receipts else int(receipts[-1]["version"])
            )
            current_head = self._read_schema_head(txn)
            if current_head != expected_head:
                raise MigrationUnsupportedError(
                    "Applied migration receipts are not an exact prefix of the destructive batch: "
                    f"expected head {expected_head}, found {current_head}."
                )
            return receipts

    @staticmethod
    def _remaining_batch_migrations(
        migrations: list[Migration],
        plan: list[dict[str, str]],
        receipts: list[dict[str, str]],
    ) -> list[Migration]:
        by_version = {migration.version: migration for migration in migrations}
        return [by_version[int(item["version"])] for item in plan[len(receipts) :]]

    def _apply_transactional_locked(self, migration: Migration) -> None:
        with self._hub.transaction() as txn:
            row = txn.execute(
                """
                SELECT version, filename, checksum
                FROM schema_migrations
                WHERE version = %s
                """,
                (migration.version,),
            ).fetchone()
            if row is not None:
                receipt = {
                    "version": str(_row_value(row, "version", 0)),
                    "filename": str(_row_value(row, "filename", 1)),
                    "checksum": str(_row_value(row, "checksum", 2)),
                }
                if receipt != _migration_plan_item(migration):
                    raise MigrationUnsupportedError(
                        f"Migration v{migration.version} is recorded with different bytes."
                    )
                return
            logger.info(
                "Applying gated PostgreSQL migration",
                extra={
                    "migration_name": migration.name,
                    "migration_version": migration.version,
                },
            )
            self._run_migration(txn, migration)
            self._record_applied_version(txn, migration)

    @staticmethod
    def _read_schema_head(txn: _TransactionLike) -> int:
        row = txn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
        if row is None or _row_value(row, "version") is None:
            raise MigrationUnsupportedError("schema_migrations has no applied head.")
        return int(_row_value(row, "version"))

    def _apply_non_transactional(self, migration: Migration) -> None:
        if self._autocommit_connection is None:
            raise MigrationUnsupportedError(
                "Non-transactional migration requires an autocommit connection."
            )
        with closing(self._autocommit_connection()) as connection:
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
            self._record_applied_version(connection, migration)

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

    def _audit_migration_state(self) -> None:
        self._ensure_schema_migrations_table()
        applied = self._read_applied_versions()
        if max(applied, default=0) < _BOOKKEEPING_VERSION:
            return
        migrations = self._discover_migrations()
        self._validate_contiguous_chain(applied, migrations)
        self._verify_applied_migrations(migrations)

    def _verify_applied_migrations(self, migrations: list[Migration]) -> None:
        local_by_version = {migration.version: migration for migration in migrations}
        with self._hub.transaction() as txn:
            rows = txn.execute(
                """
                SELECT version, filename, checksum
                FROM schema_migrations
                ORDER BY version
                """
            ).fetchall()

        for row in rows:
            version = int(_row_value(row, "version", 0))
            filename = _row_value(row, "filename", 1)
            checksum = _row_value(row, "checksum", 2)
            if filename is None and checksum is None and version < _BOOKKEEPING_VERSION:
                continue
            if not isinstance(filename, str) or not isinstance(checksum, str):
                raise MigrationUnsupportedError(
                    f"Migration v{version} has incomplete filename/checksum bookkeeping."
                )

            if version == BASELINE_VERSION:
                expected_filename = f"baseline@{BASELINE_VERSION}"
                expected_checksum = baseline_checksum()
            else:
                migration = local_by_version.get(version)
                if migration is None:
                    raise MigrationUnsupportedError(
                        f"Applied migration v{version} has no matching on-disk file."
                    )
                expected_filename = migration.path.name
                expected_checksum = _migration_checksum(migration)

            if filename != expected_filename:
                raise MigrationUnsupportedError(
                    f"Migration filename mismatch for v{version}: "
                    f"recorded {filename!r}, found {expected_filename!r}."
                )
            if checksum != expected_checksum:
                raise MigrationUnsupportedError(
                    f"Migration checksum mismatch for v{version} ({expected_filename})."
                )

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
                if path.name.startswith("."):
                    continue
                raise MigrationUnsupportedError(f"Invalid migration filename: {path.name}")

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
        return _has_directive(migration, _NON_TRANSACTIONAL_DIRECTIVE)

    @staticmethod
    def _is_destructive(migration: Migration) -> bool:
        return _has_directive(migration, _DESTRUCTIVE_DIRECTIVE)

    @staticmethod
    def _validate_contiguous_chain(
        applied: set[int],
        migrations: list[Migration],
    ) -> None:
        database_head = max(applied, default=BASELINE_VERSION)
        for version in range(BASELINE_VERSION + 1, database_head + 1):
            if version not in applied:
                raise MigrationUnsupportedError(
                    f"Migration chain is not contiguous: missing applied migration v{version}."
                )

        expected = database_head + 1
        for migration in migrations:
            if migration.version <= database_head:
                continue
            if migration.version != expected:
                raise MigrationUnsupportedError(
                    f"Migration chain is not contiguous: missing migration v{expected} "
                    f"before {migration.path.name}."
                )
            expected += 1

    @staticmethod
    def _validate_pending_chain(
        applied: set[int],
        pending: list[Migration],
    ) -> None:
        applied_chain = [version for version in applied if version >= _BOOKKEEPING_VERSION]
        expected = max(applied_chain, default=_BOOKKEEPING_VERSION - 1) + 1
        for migration in pending:
            if migration.version < _BOOKKEEPING_VERSION:
                continue
            if migration.version != expected:
                raise MigrationUnsupportedError(
                    f"Migration chain is not contiguous: missing migration v{expected} "
                    f"before {migration.path.name}."
                )
            expected += 1

    def _run_migration(self, txn: _TransactionLike, migration: Migration) -> None:
        _execute_sql_script(txn, migration.path.read_text())

    def _record_applied_version(
        self,
        txn: _TransactionLike,
        migration: Migration,
    ) -> None:
        if migration.version >= _BOOKKEEPING_VERSION:
            txn.execute(
                """
            INSERT INTO schema_migrations(version, filename, checksum, applied_at)
            VALUES (%s, %s, %s, NOW())
            """,
                (
                    migration.version,
                    migration.path.name,
                    _migration_checksum(migration),
                ),
            )
            return
        txn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (%s, NOW())",
            (migration.version,),
        )


def _has_directive(migration: Migration, directive: str) -> bool:
    return directive in {
        line.strip()
        for line in migration.path.read_text().splitlines()
        if line.lstrip().startswith("-- gobby:")
    }


def _migration_checksum(migration: Migration) -> str:
    return hashlib.sha256(migration.path.read_bytes()).hexdigest()


def baseline_checksum(sql: str | None = None) -> str:
    if sql is None:
        baseline = importlib.resources.files("gobby.storage").joinpath(
            "postgres_baseline_schema.sql"
        )
        return hashlib.sha256(baseline.read_bytes()).hexdigest()
    return hashlib.sha256(sql.encode()).hexdigest()


def _migration_plan_item(migration: Migration) -> dict[str, str]:
    return {
        "version": str(migration.version),
        "filename": migration.path.name,
        "checksum": _migration_checksum(migration),
    }


def _json_object(value: Any) -> dict[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise MigrationUnsupportedError("Destructive batch intent is not a JSON object.")
    return parsed


def _json_object_list(value: Any) -> list[dict[str, str]]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise MigrationUnsupportedError("Destructive batch migration plan is not a JSON array.")
    return [{str(key): str(item_value) for key, item_value in item.items()} for item in parsed]


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
