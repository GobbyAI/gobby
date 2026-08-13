"""Schema-per-worker pytest fixtures for PostgresHubDatabase tests.

Per-worker isolation without per-session container churn: each xdist worker
gets its own Postgres schema inside the shared test container; per-test
isolation is achieved by resetting mutable rows back to the canonical seed
captured once when the worker's schema was first migrated.

The reset-based approach is intentional. A single outer savepoint is not
sufficient once the runtime uses pooled connections — work can commit on a
different connection and bypass that savepoint entirely. Resetting the worker
schema gives real isolation without constraining production code to a
single-connection model.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from gobby.runner_maintenance.storage_hygiene import sweep_orphaned_test_schemas
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.schema_contract import apply_schema
from gobby.utils.env import is_test_protect_enabled
from gobby.utils.machine_id import get_machine_id

_POSTGRES_IDENTIFIER_MAX_BYTES = 63
_DEFAULT_POSTGRES_PORT = "5432"
_TEST_SCHEMA_PREFIX = "gobby_test_"


def _test_schema_created_epoch(schema_name: str) -> int | None:
    parts = schema_name.split("_")
    if len(parts) != 6 or parts[:2] != ["gobby", "test"]:
        return None
    created_epoch, process_id, worker_label, nonce = parts[2:]
    if not created_epoch.isascii() or not created_epoch.isdigit():
        return None
    if not process_id.isascii() or not process_id.isdigit():
        return None
    if not worker_label or not nonce:
        return None
    return int(created_epoch)


# Reserved machine-id namespace seeded into every worker schema's canonical
# snapshot; fixtures attribute sessions to these ids to satisfy the
# sessions.machine_id -> machines(id) FK. Tests asserting machines-table
# contents must exclude this prefix.
TEST_MACHINE_ID_PREFIX = "21000000-0000-4000-8000-"
TEST_USER_ID = "20000000-0000-4000-8000-000000000001"
TEST_USER_NAME = "Gobby Test User"
TEST_USER_EMAIL = "test-user@gobby.local"
TEST_USER_PASSWORD = "test-password"
TEST_USER_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$Z29iYnktdGVzdC1zYWx0IQ$"
    "a0t+5elAO+H9L/acbECK+32i6gZTZeKfT221qJhLLjM"
)
_LOOPBACK_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1"})
_TEST_DSN_REQUIRED = (
    "DATABASE_URL must point at an isolated PostgreSQL test database; start one with "
    "`docker compose -f docker-compose.test.yml up -d postgres-test`."
)
_ISOLATED_SCHEMA_APPLICATION_PREFIX = "gobby-isolated-schema-"
_SCHEMA_BOOKKEEPING_TABLES = frozenset({"schema_migrations"})


def _schema_looks_test_only(schema: str) -> bool:
    return schema.startswith(_TEST_SCHEMA_PREFIX)


def _enforce_safe_test_schema(schema: str) -> None:
    if os.environ.get("GOBBY_TEST_PROTECT") != "1":
        return
    if _schema_looks_test_only(schema):
        return
    pytest.fail(
        "Refusing to run PostgreSQL tests outside a gobby_test_* schema while GOBBY_TEST_PROTECT=1."
    )


def _adapt_seed_value(value: Any) -> Any:
    if isinstance(value, dict | list):
        return Jsonb(value)
    return value


def _adapt_seed_rows(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return [tuple(_adapt_seed_value(value) for value in row) for row in rows]


logger = logging.getLogger(__name__)


def _dsn_identity(url: str) -> tuple[str, str, str] | None:
    """Return the (host, port, dbname) a DSN resolves to, or None if unparseable."""
    try:
        fields = conninfo_to_dict(url)
    except psycopg.Error:
        return None
    host = str(fields.get("host") or "")
    port = str(fields.get("port") or "") or _DEFAULT_POSTGRES_PORT
    return (
        "localhost" if host in _LOOPBACK_HOSTS else host,
        port,
        str(fields.get("dbname") or ""),
    )


def _live_hub_identity() -> tuple[str, str, str] | None:
    """Return the identity of the operator's configured hub, when one is configured."""
    try:
        from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

        database_url = load_bootstrap(resolve_database_url=True).database_url
    except BootstrapConfigError as exc:
        logger.debug("No bootstrap PostgreSQL DSN to guard tests against: %s", exc)
        return None
    return _dsn_identity(database_url) if database_url else None


def pytest_configure(config: pytest.Config) -> None:
    """Refuse to run any test against the operator's live hub database.

    The suite creates, resets, and drops schemas, and hub maintenance terminates
    every backend on the database it fences. Neither is survivable on the hub the
    running daemon owns, so abort before collection rather than at first connect.
    """
    del config
    url = os.environ.get("DATABASE_URL")
    if not url:
        return
    hub_identity = _live_hub_identity()
    if hub_identity is not None and _dsn_identity(url) == hub_identity:
        raise pytest.UsageError(
            f"DATABASE_URL points at the live Gobby hub database. {_TEST_DSN_REQUIRED}"
        )


def _require_test_database_url() -> str:
    """Return the explicit test DSN, refusing to invent one.

    There is deliberately no bootstrap fallback: that DSN names the hub the
    running daemon owns, and a suite run against it drops schemas out from
    under production. Under `GOBBY_TEST_PROTECT=1` a missing DSN is an error
    rather than a skip, so an agent cannot mistake "nothing ran" for "passed".
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    if is_test_protect_enabled():
        pytest.fail(_TEST_DSN_REQUIRED)
    pytest.skip(_TEST_DSN_REQUIRED)


@pytest.fixture(scope="session")
def postgres_database_url() -> str:
    return _require_test_database_url()


def _cleanup_orphaned_schemas(url: str, age_hours: int = 1) -> None:
    """Sweep old schemas while preserving live runs whose lease backend was terminated."""
    sweep_orphaned_test_schemas(url, age_hours)


def _capture_canonical_seed(
    conn: psycopg.Connection[Any],
) -> dict[str, list[tuple[Any, ...]]]:
    """Snapshot every non-bookkeeping table that has rows after a fresh
    `apply_migrations()`.

    Captures dynamically rather than hard-coding a fixed set of seed-bearing
    tables: any table that ships seed rows in the baseline (`projects`,
    `task_type_default_stages`, `task_stages_registry`, `sessions`, plus
    any table a future baseline adds) is automatically protected.
    Bookkeeping tables (the shared `_BASELINE_BOOKKEEPING_TABLES` set owned
    by `gobby.storage.hub.postgres`) are skipped because their rows survive
    resets verbatim.
    """
    snapshot: dict[str, list[tuple[Any, ...]]] = {}
    all_tables = [
        row[0]
        for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() ORDER BY tablename"
        ).fetchall()
    ]
    for table in all_tables:
        if table in _SCHEMA_BOOKKEEPING_TABLES:
            continue
        rows = conn.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))).fetchall()
        if rows:
            snapshot[table] = [tuple(r) for r in rows]
    return snapshot


def _delete_order(conn: psycopg.Connection[Any], tables: set[str]) -> list[str]:
    """Order tables child-first for immediate RESTRICT/NO ACTION foreign keys."""
    blockers: dict[str, set[str]] = {table: set() for table in tables}
    parents_by_child: dict[str, set[str]] = {table: set() for table in tables}
    foreign_keys = conn.execute(
        """
        SELECT child.relname, parent.relname
        FROM pg_constraint AS fk
        JOIN pg_class AS child ON child.oid = fk.conrelid
        JOIN pg_namespace AS child_schema ON child_schema.oid = child.relnamespace
        JOIN pg_class AS parent ON parent.oid = fk.confrelid
        JOIN pg_namespace AS parent_schema ON parent_schema.oid = parent.relnamespace
        WHERE fk.contype = 'f'
          AND (fk.confdeltype = 'r' OR (fk.confdeltype = 'a' AND NOT fk.condeferrable))
          AND child_schema.nspname = current_schema()
          AND parent_schema.nspname = current_schema()
        """
    ).fetchall()
    for child, parent in foreign_keys:
        if child == parent or child not in tables or parent not in tables:
            continue
        blockers[parent].add(child)
        parents_by_child[child].add(parent)

    ready = {table for table, children in blockers.items() if not children}
    ordered: list[str] = []
    while ready:
        table = min(ready)
        ready.remove(table)
        ordered.append(table)
        for parent in parents_by_child[table]:
            blockers[parent].remove(table)
            if not blockers[parent]:
                ready.add(parent)

    if len(ordered) != len(tables):
        blocked = sorted(tables - set(ordered))
        raise RuntimeError(f"Cannot reset schema with cyclic immediate foreign keys: {blocked}")
    return ordered


def _insert_order(conn: psycopg.Connection[Any], tables: set[str]) -> list[str]:
    """Order tables parent-first for every non-deferrable foreign key."""
    remaining_parents: dict[str, set[str]] = {table: set() for table in tables}
    children_by_parent: dict[str, set[str]] = {table: set() for table in tables}
    foreign_keys = conn.execute(
        """
        SELECT child.relname, parent.relname
        FROM pg_constraint AS fk
        JOIN pg_class AS child ON child.oid = fk.conrelid
        JOIN pg_namespace AS child_schema ON child_schema.oid = child.relnamespace
        JOIN pg_class AS parent ON parent.oid = fk.confrelid
        JOIN pg_namespace AS parent_schema ON parent_schema.oid = parent.relnamespace
        WHERE fk.contype = 'f'
          AND NOT fk.condeferrable
          AND child_schema.nspname = current_schema()
          AND parent_schema.nspname = current_schema()
        """
    ).fetchall()
    for child, parent in foreign_keys:
        if child == parent or child not in tables or parent not in tables:
            continue
        remaining_parents[child].add(parent)
        children_by_parent[parent].add(child)

    ready = {table for table, parents in remaining_parents.items() if not parents}
    ordered: list[str] = []
    while ready:
        table = min(ready)
        ready.remove(table)
        ordered.append(table)
        for child in children_by_parent[table]:
            remaining_parents[child].remove(table)
            if not remaining_parents[child]:
                ready.add(child)

    if len(ordered) != len(tables):
        blocked = sorted(tables - set(ordered))
        raise RuntimeError(
            f"Cannot restore canonical seed with cyclic non-deferrable foreign keys: {blocked}"
        )
    return ordered


def _reset_schema(
    url: str,
    schema: str,
    canonical_seed: dict[str, list[tuple[Any, ...]]],
) -> None:
    """Reset the worker schema to its fresh-baseline state.

    Algorithm:
      - Bookkeeping tables (`schema_migrations`) are left untouched; both rows
        and table existence are preserved verbatim.
      - All other application tables are emptied with DELETE, preserving their
        relfilenodes while deferred constraints allow dependency-safe ordering.
      - Sequences owned by reset tables are restarted to their fresh-schema values.
      - Every table present in `canonical_seed` is re-INSERTed parent-first
        inside the same transaction, supporting immediate foreign keys.

    After this call the schema is byte-for-byte equivalent to the state
    `PostgresHubDatabase.apply_migrations()` produces on a fresh schema —
    that is the invariant acceptance 2.2.3 enforces.
    """
    _enforce_safe_test_schema(schema)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        all_tables = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            ).fetchall()
        }
        reset_tables = _delete_order(conn, all_tables - _SCHEMA_BOOKKEEPING_TABLES)
        sequences = [
            row[0]
            for row in conn.execute(
                """
                SELECT sequence.relname
                FROM pg_class AS sequence
                JOIN pg_namespace AS sequence_schema
                  ON sequence_schema.oid = sequence.relnamespace
                JOIN pg_depend AS dependency
                  ON dependency.objid = sequence.oid
                JOIN pg_class AS owner_table
                  ON owner_table.oid = dependency.refobjid
                JOIN pg_namespace AS owner_schema
                  ON owner_schema.oid = owner_table.relnamespace
                WHERE sequence.relkind = 'S'
                  AND sequence_schema.nspname = current_schema()
                  AND owner_schema.nspname = current_schema()
                  AND dependency.classid = 'pg_class'::regclass
                  AND dependency.refclassid = 'pg_class'::regclass
                  AND dependency.deptype IN ('a', 'i')
                  AND owner_table.relname = ANY(%s)
                ORDER BY sequence.relname
                """,
                (reset_tables,),
            ).fetchall()
        ]

        with conn.transaction():
            conn.execute("SET CONSTRAINTS ALL DEFERRED")
            for table in reset_tables:
                conn.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))
            for sequence in sequences:
                conn.execute(sql.SQL("ALTER SEQUENCE {} RESTART").format(sql.Identifier(sequence)))
            for table in _insert_order(conn, set(reset_tables)):
                rows = canonical_seed.get(table, [])
                if not rows:
                    continue
                placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in range(len(rows[0])))
                with conn.cursor() as cur:
                    cur.executemany(
                        sql.SQL("INSERT INTO {} VALUES ({})").format(
                            sql.Identifier(table), placeholders
                        ),
                        _adapt_seed_rows(rows),
                    )


@contextlib.contextmanager
def isolated_test_schema(url: str, worker_label: str) -> Iterator[str]:
    """Create a uniquely named `gobby_test_*` schema; drop it on exit.

    Schema name layout: `gobby_test_<epoch>_<pid>_<label>_<nonce>` — six
    underscore-delimited parts so `_cleanup_orphaned_schemas` can recover
    the creation epoch from `parts[2]` regardless of the label. The label
    must not contain underscores.
    """
    created_epoch = int(time.time())
    nonce = uuid.uuid4().hex[:6]
    schema = f"{_TEST_SCHEMA_PREFIX}{created_epoch}_{os.getpid()}_{worker_label}_{nonce}"
    if (
        _test_schema_created_epoch(schema) is None
        or len(schema.encode()) > _POSTGRES_IDENTIFIER_MAX_BYTES
    ):
        raise ValueError(
            "PostgreSQL test worker label must be non-empty, contain no underscores, "
            "and fit the six-part schema name contract"
        )
    _enforce_safe_test_schema(schema)
    _cleanup_orphaned_schemas(url)
    application_name = f"{_ISOLATED_SCHEMA_APPLICATION_PREFIX}{worker_label}-{nonce}"
    with psycopg.connect(
        url,
        autocommit=True,
        application_name=application_name,
    ) as conn:
        backend_pid = conn.info.backend_pid
        logger.info(
            "Isolated test schema %s uses PostgreSQL backend pid %s (application_name=%s)",
            schema,
            backend_pid,
            application_name,
        )
        conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (schema,))
        try:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            try:
                yield schema
            finally:
                try:
                    _drop_test_schema(conn, schema)
                except psycopg.OperationalError as exc:
                    logger.warning(
                        "Isolated test schema %s lost PostgreSQL backend pid %s during "
                        "teardown; reconnecting (application_name=%s): %s",
                        schema,
                        backend_pid,
                        application_name,
                        exc,
                    )
                    with psycopg.connect(
                        url,
                        autocommit=True,
                        application_name=application_name,
                    ) as cleanup_conn:
                        logger.info(
                            "Isolated test schema %s cleanup uses PostgreSQL backend pid %s "
                            "(application_name=%s)",
                            schema,
                            cleanup_conn.info.backend_pid,
                            application_name,
                        )
                        _drop_test_schema(cleanup_conn, schema)
        finally:
            if not conn.closed:
                try:
                    conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (schema,))
                except psycopg.OperationalError:
                    # PostgreSQL released the session lock when the backend exited.
                    pass


def _drop_test_schema(conn: psycopg.Connection[Any], schema: str) -> None:
    exists = conn.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
        (schema,),
    ).fetchone()
    if exists:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture(scope="session")
def postgres_schema(postgres_database_url: str, worker_id: str) -> Iterator[str]:
    """Create a unique schema for this xdist worker; drop it on teardown.

    This schema is reserved for the canonical-baseline lifecycle
    (`postgres_canonical_seed` / `postgres_db`). Tests that need raw scratch
    tables without the migrated baseline must use their own
    `isolated_test_schema` so the baseline classifier never sees a schema
    holding foreign tables.

    Requires an explicit `DATABASE_URL` naming an isolated test database.
    """
    with isolated_test_schema(postgres_database_url, worker_id) as schema:
        yield schema


@pytest.fixture(scope="session")
def postgres_canonical_seed(
    postgres_database_url: str,
    postgres_schema: str,
) -> dict[str, list[tuple[Any, ...]]]:
    """One-time per-worker capture of fresh-baseline seed rows.

    Runs `PostgresHubDatabase.apply_migrations()` once against the worker
    schema, opens a connection with that schema on `search_path`, and
    snapshots every non-bookkeeping table that has rows. The snapshot is
    the source of truth for `_reset_schema`'s re-INSERT step on every
    per-test reset.
    """
    url = postgres_database_url
    apply_schema(url, schema=postgres_schema)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(postgres_schema)))
        conn.execute(
            """
            INSERT INTO users (id, email, name, password_hash)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (TEST_USER_ID, TEST_USER_EMAIL, TEST_USER_NAME, TEST_USER_PASSWORD_HASH),
        )
        conn.execute(
            f"""
                INSERT INTO machines (id, owner_user_id)
                SELECT ('{TEST_MACHINE_ID_PREFIX}' || LPAD(n::TEXT, 12, '0'))::UUID, %s
                FROM GENERATE_SERIES(1, 50) AS n
                ON CONFLICT (id) DO NOTHING
                """,
            (TEST_USER_ID,),
        )
        local_machine_id = get_machine_id()
        if local_machine_id is not None:
            conn.execute(
                """
                INSERT INTO machines (id, owner_user_id)
                VALUES (%s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (local_machine_id, TEST_USER_ID),
            )
        return _capture_canonical_seed(conn)


@pytest.fixture
def postgres_db(
    postgres_database_url: str,
    postgres_schema: str,
    postgres_canonical_seed: dict[str, list[tuple[Any, ...]]],
) -> Iterator[PostgresHubDatabase]:
    """Per-test `PostgresHubDatabase` over a reset worker schema.

    Both entry and exit resets receive the same canonical seed snapshot
    captured once per worker by `postgres_canonical_seed`.
    """
    base_url = postgres_database_url
    scoped_url = base_url + f"?options=-csearch_path%3D{postgres_schema}"
    db = PostgresHubDatabase(scoped_url)
    try:
        _reset_schema(base_url, postgres_schema, postgres_canonical_seed)
        yield db
    finally:
        try:
            _reset_schema(base_url, postgres_schema, postgres_canonical_seed)
        finally:
            db.close()
