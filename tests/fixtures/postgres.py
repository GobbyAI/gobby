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

import logging
import os
import time
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from gobby.storage.hub.postgres import (
    _BASELINE_BOOKKEEPING_TABLES,
    PostgresHubDatabase,
)

_TEST_SCHEMA_PREFIX = "gobby_test_"


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


def _configured_postgres_database_url() -> str | None:
    if os.environ.get("GOBBY_TEST_PROTECT") != "1":
        return None
    try:
        from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

        return load_bootstrap().database_url
    except BootstrapConfigError as exc:
        logger.warning("Failed to load bootstrap PostgreSQL DSN for tests: %s", exc)
        return None


@pytest.fixture(scope="session")
def postgres_database_url() -> str:
    url = os.environ.get("DATABASE_URL") or _configured_postgres_database_url()
    if not url:
        pytest.skip("DATABASE_URL or configured bootstrap database_url is required")
    return url


def _cleanup_orphaned_schemas(url: str, age_hours: int = 24) -> None:
    """Drop only aged `gobby_test_*` schemas from abandoned test runs."""
    cutoff_epoch = int(time.time()) - age_hours * 3600
    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute(
            """
            SELECT schema_name
            FROM information_schema.schemata
            WHERE schema_name LIKE 'gobby_test_%'
            """
        ).fetchall()
        for (schema_name,) in rows:
            parts = schema_name.split("_", 5)
            if len(parts) != 6:
                continue
            try:
                created_epoch = int(parts[2])
            except ValueError:
                continue
            if created_epoch > cutoff_epoch:
                continue
            logger.warning("Dropping orphaned Postgres test schema %s", schema_name)
            try:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name)))
            except psycopg.Error:
                logger.exception("Failed to drop orphaned schema %s", schema_name)


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
        if table in _BASELINE_BOOKKEEPING_TABLES:
            continue
        rows = conn.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))).fetchall()
        if rows:
            snapshot[table] = [tuple(r) for r in rows]
    return snapshot


def _reset_schema(
    url: str,
    schema: str,
    canonical_seed: dict[str, list[tuple[Any, ...]]],
) -> None:
    """Reset the worker schema to its fresh-baseline state.

    Algorithm:
      - Bookkeeping tables (`schema_migrations`, `gobby_migration_state`) are
        left untouched — both rows and table existence preserved verbatim.
      - All other application tables are TRUNCATE … RESTART IDENTITY CASCADE'd.
      - Every table present in `canonical_seed` is re-INSERTed inside the
        same transaction with all FK constraints deferred, so re-seed order
        does not need to follow the dependency graph.

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
        truncate_tables = sorted(all_tables - _BASELINE_BOOKKEEPING_TABLES)

        with conn.transaction():
            conn.execute("SET CONSTRAINTS ALL DEFERRED")
            if truncate_tables:
                joined = sql.SQL(", ").join(sql.Identifier(t) for t in truncate_tables)
                conn.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(joined))
            for table in sorted(canonical_seed):
                rows = canonical_seed[table]
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


@pytest.fixture(scope="session")
def postgres_schema(postgres_database_url: str, worker_id: str) -> Iterator[str]:
    """Create a unique schema for this xdist worker; drop it on teardown.

    Schema name layout: `gobby_test_<epoch>_<pid>_<worker>_<nonce>` — six
    underscore-delimited parts so `_cleanup_orphaned_schemas` can recover
    the creation epoch from `parts[2]` even when the worker id is `master`.

    Uses `DATABASE_URL` when configured, otherwise falls back to the local
    Gobby bootstrap database_url under `GOBBY_TEST_PROTECT=1`.
    """
    url = postgres_database_url
    created_epoch = int(time.time())
    nonce = uuid.uuid4().hex[:6]
    schema = f"{_TEST_SCHEMA_PREFIX}{created_epoch}_{os.getpid()}_{worker_id}_{nonce}"
    _enforce_safe_test_schema(schema)
    _cleanup_orphaned_schemas(url)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield schema
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                (schema,),
            ).fetchone()
            if exists:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


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
    db = PostgresHubDatabase(url + f"?options=-csearch_path%3D{postgres_schema}")
    try:
        db.apply_migrations()
    finally:
        db.close()
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(postgres_schema)))
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
    # apply_migrations is idempotent; the session-scoped seed fixture ran it
    # before this test was created, so this call is a no-op.
    db = PostgresHubDatabase(scoped_url)
    db.apply_migrations()
    _reset_schema(base_url, postgres_schema, postgres_canonical_seed)
    try:
        yield db
    finally:
        try:
            _reset_schema(base_url, postgres_schema, postgres_canonical_seed)
        finally:
            db.close()
