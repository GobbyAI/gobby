from __future__ import annotations

import importlib
import os
import queue
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import pytest

_HEX40 = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class _CounterLock:
    PRIORITY: ClassVar[int] = 950
    name: str


class _EmptyResult:
    def fetchall(self) -> list[Any]:
        return []

    def fetchone(self) -> Any | None:
        return None


class _CopyTarget:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.transaction_entries = 0

    @contextmanager
    def transaction(self) -> Iterator[_CopyTarget]:
        self.transaction_entries += 1
        yield self

    def execute(self, statement: object, params: object = ()) -> _EmptyResult:
        _ = params
        self.statements.append(str(statement))
        return _EmptyResult()


def test_post_phase5_audit_report_frontmatter_and_rows(repo_root: Path) -> None:
    report_path = repo_root / "docs" / "postgres-concurrency-audit.md"
    text = report_path.read_text()

    top_frontmatter = _parse_top_frontmatter(text)
    assert top_frontmatter["audit_version"] == "1"
    assert top_frontmatter["phase_baseline"] == "P4"
    assert _HEX40.fullmatch(top_frontmatter["audit_commit"])

    post_frontmatter = _parse_post_phase_frontmatter(text)
    assert post_frontmatter["audit_version"] == "2"
    assert post_frontmatter["phase_baseline"] == "P5"
    assert _HEX40.fullmatch(post_frontmatter["audit_commit"])
    assert _HEX40.fullmatch(post_frontmatter["prior_audit_commit"])
    assert post_frontmatter["prior_audit_commit"] == top_frontmatter["audit_commit"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", post_frontmatter["audited_at"])

    assert "Unresolved High/Medium findings: None." in text
    assert "| Callback Site | Risk Level | Read-Modify-Write Risk |" in text
    for required in (
        "sqlite_to_postgres.migrate_sqlite_to_postgres",
        "_copy_sqlite_rows_to_postgres",
        "reseed_identity_sequences",
        "validate_migration",
        "Postgres after-commit callbacks",
        "transaction_immediate",
    ):
        assert required in text


def test_importer_copy_forces_deferred_constraints_before_return(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    migration = importlib.import_module("gobby.storage.migration.sqlite_to_postgres")
    source = sqlite3.connect(":memory:")
    target = _CopyTarget()

    monkeypatch.setattr(migration, "_sqlite_application_tables", lambda _source: {"child"})
    monkeypatch.setattr(
        migration, "_dependency_ordered_tables", lambda _source, _tables: ("child",)
    )
    monkeypatch.setattr(migration, "_copy_columns", lambda *_args: ("id", "parent_id"))
    monkeypatch.setattr(migration, "_copy_table", lambda *_args: 1)
    monkeypatch.setattr(migration, "_write_import_log", lambda *_args: None)

    try:
        result = migration._copy_sqlite_rows_to_postgres(
            source, target, 100, tmp_path / "import.log"
        )
    finally:
        source.close()

    assert result.rows == 1
    assert result.tables == 1
    assert target.transaction_entries == 1
    assert target.statements == [
        "SET CONSTRAINTS ALL DEFERRED",
        "SET CONSTRAINTS ALL IMMEDIATE",
    ]


@pytest.mark.integration
def test_after_commit_async_reader_uses_committed_state(postgres_db: Any) -> None:
    table = "mvcc_async_reader"
    _drop_table(postgres_db, table)
    postgres_db.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')

    try:
        results: queue.Queue[object] = queue.Queue()

        def callback() -> None:
            def read_from_pool() -> None:
                try:
                    row = postgres_db.fetchone(f'SELECT value FROM "{table}" WHERE id = $1', (1,))
                    results.put(None if row is None else row["value"])
                except BaseException as exc:  # pragma: no cover - re-raised in main thread
                    results.put(exc)

            thread = threading.Thread(target=read_from_pool)
            thread.start()
            thread.join(timeout=5)
            if thread.is_alive():
                results.put(TimeoutError("after_commit reader did not finish"))

        with postgres_db.transaction() as txn:
            txn.execute(f'INSERT INTO "{table}" (id, value) VALUES ($1, $2)', (1, "committed"))
            txn.after_commit(callback)
            assert results.empty()

        result = results.get(timeout=5)
        if isinstance(result, BaseException):
            raise AssertionError("after_commit reader failed") from result
        assert result == "committed"
    finally:
        _drop_table(postgres_db, table)


@pytest.mark.integration
def test_after_commit_reader_respects_long_running_snapshot(
    postgres_db: Any,
    postgres_schema: str,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    table = "mvcc_snapshot_reader"
    _drop_table(postgres_db, table)
    postgres_db.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')

    held = psycopg.connect(_scoped_dsn(postgres_schema), autocommit=True)
    try:
        held.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        first_count = held.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        assert first_count == 0

        observed: list[int] = []

        def callback() -> None:
            row = held.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            observed.append(int(row[0]))

        with postgres_db.transaction() as txn:
            txn.execute(f'INSERT INTO "{table}" (id, value) VALUES ($1, $2)', (1, "committed"))
            txn.after_commit(callback)

        assert observed == [0]
        held.execute("COMMIT")
        fresh_count = held.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        assert fresh_count == 1
    finally:
        try:
            held.execute("ROLLBACK")
        except Exception:
            pass
        held.close()
        _drop_table(postgres_db, table)


@pytest.mark.integration
def test_savepoint_callback_rollback_safe_with_postgres(postgres_db: Any) -> None:
    table = "mvcc_savepoint_reader"
    _drop_table(postgres_db, table)
    postgres_db.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')

    try:
        seen: list[int] = []

        with postgres_db.transaction() as txn:
            txn.execute(f'INSERT INTO "{table}" (id, value) VALUES ($1, $2)', (1, "outer"))
            savepoint = txn.savepoint("mvcc_rollback")
            txn.execute(f'INSERT INTO "{table}" (id, value) VALUES ($1, $2)', (2, "rolled-back"))
            savepoint.rollback()
            savepoint.release()
            txn.after_commit(
                lambda: seen.extend(
                    int(row["id"]) for row in postgres_db.fetchall(f'SELECT id FROM "{table}"')
                )
            )

        assert seen == [1]
    finally:
        _drop_table(postgres_db, table)


@pytest.mark.integration
def test_read_modify_write_path_serializes_concurrent_writers(postgres_db: Any) -> None:
    table = "mvcc_counter"
    _drop_table(postgres_db, table)
    postgres_db.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)')
    postgres_db.execute(f'INSERT INTO "{table}" (id, value) VALUES ($1, $2)', (1, 0))

    try:
        errors: queue.Queue[BaseException] = queue.Queue()

        def worker() -> None:
            try:
                with postgres_db.transaction_immediate(_CounterLock(table)) as txn:
                    row = txn.execute(f'SELECT value FROM "{table}" WHERE id = $1', (1,)).fetchone()
                    assert row is not None
                    next_value = int(row["value"]) + 1
                    time.sleep(0.05)
                    txn.execute(f'UPDATE "{table}" SET value = $1 WHERE id = $2', (next_value, 1))
            except BaseException as exc:  # pragma: no cover - re-raised in main thread
                errors.put(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert all(not thread.is_alive() for thread in threads)
        if not errors.empty():
            raise AssertionError("concurrent writer failed") from errors.get()

        row = postgres_db.fetchone(f'SELECT value FROM "{table}" WHERE id = $1', (1,))
        assert row is not None
        assert row["value"] == 2
    finally:
        _drop_table(postgres_db, table)


@pytest.mark.integration
def test_deferrable_constraint_is_forced_before_marker(postgres_schema: str) -> None:
    psycopg = pytest.importorskip("psycopg")
    parent = "mvcc_parent"
    child = "mvcc_child"

    with psycopg.connect(_scoped_dsn(postgres_schema), autocommit=True) as conn:
        conn.execute(f'DROP TABLE IF EXISTS "{child}"')
        conn.execute(f'DROP TABLE IF EXISTS "{parent}"')
        conn.execute(f'CREATE TABLE "{parent}" (id INTEGER PRIMARY KEY)')
        conn.execute(
            f'CREATE TABLE "{child}" ('
            "id INTEGER PRIMARY KEY, "
            "parent_id INTEGER REFERENCES "
            f'"{parent}"(id) DEFERRABLE INITIALLY IMMEDIATE)'
        )
        try:
            with conn.transaction():
                conn.execute("SET CONSTRAINTS ALL DEFERRED")
                conn.execute(f'INSERT INTO "{child}" (id, parent_id) VALUES (1, 7)')
                conn.execute(f'INSERT INTO "{parent}" (id) VALUES (7)')
                conn.execute("SET CONSTRAINTS ALL IMMEDIATE")

            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with conn.transaction():
                    conn.execute("SET CONSTRAINTS ALL DEFERRED")
                    conn.execute(f'INSERT INTO "{child}" (id, parent_id) VALUES (2, 404)')
                    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")

            row = conn.execute(f'SELECT COUNT(*) FROM "{child}" WHERE id = 2').fetchone()
            assert row[0] == 0
        finally:
            conn.execute(f'DROP TABLE IF EXISTS "{child}"')
            conn.execute(f'DROP TABLE IF EXISTS "{parent}"')


def _parse_top_frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"---\n(?P<body>.*?)\n---", text, flags=re.DOTALL)
    assert match is not None
    return _parse_key_values(match.group("body"))


def _parse_post_phase_frontmatter(text: str) -> dict[str, str]:
    _before, _heading, after = text.partition("## Post-Phase-5 re-audit")
    assert after
    match = re.search(r"```yaml\n(?P<body>.*?)\n```", after, flags=re.DOTALL)
    assert match is not None
    parsed = _parse_key_values(match.group("body"))
    for key in {
        "audit_version",
        "phase_baseline",
        "audit_commit",
        "prior_audit_commit",
        "audited_at",
    }:
        assert key in parsed
    return parsed


def _parse_key_values(block: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _scoped_dsn(postgres_schema: str) -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is required for PostgreSQL MVCC tests")
    return dsn + f"?options=-csearch_path%3D{postgres_schema}"


def _drop_table(postgres_db: Any, table: str) -> None:
    postgres_db.execute(f'DROP TABLE IF EXISTS "{table}"')
