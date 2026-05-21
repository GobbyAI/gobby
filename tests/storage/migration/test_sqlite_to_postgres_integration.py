from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.sqlite import SqliteHubDatabase
from gobby.storage.migration.sqlite_to_postgres import migrate_sqlite_to_postgres
from gobby.storage.migration.validation import MigrationValidationError, validate_migration
from gobby.storage.migrations import BASELINE_VERSION

pytestmark = pytest.mark.integration


def test_migrate_sqlite_to_postgres_real_target_preserves_dry_run_and_imports_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    postgres_schema: str,
) -> None:
    target_url = _target_url(postgres_schema)
    _require_pg_search(postgres_schema)
    _initialize_postgres_target(target_url)
    source = _sqlite_hub_source(tmp_path)
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    monkeypatch.setattr(
        "gobby.storage.migration.sqlite_to_postgres.active_install_mode", lambda: "docker"
    )

    before_dry_run = _postgres_snapshot_hash(postgres_schema)
    dry_run = migrate_sqlite_to_postgres(
        source=source,
        target=target_url,
        batch_size=128,
        dry_run=True,
    )
    after_dry_run = _postgres_snapshot_hash(postgres_schema)

    assert dry_run["dry_run"] is True
    assert dry_run["rows"] > 0
    assert dry_run["tables"] > 0
    assert before_dry_run == after_dry_run

    result = migrate_sqlite_to_postgres(
        source=source,
        target=target_url,
        batch_size=128,
        dry_run=False,
    )

    assert result["dry_run"] is False
    assert result["rows"] == dry_run["rows"]
    assert result["tables"] == dry_run["tables"]
    assert result["log_path"] is not None
    assert result["validation_artifact"] is not None

    log_path = Path(result["log_path"])
    artifact_path = Path(result["validation_artifact"])
    assert log_path.exists()
    assert artifact_path.exists()
    assert any(
        record["event"] == "table_copy_end" and record["table"] == "config_store"
        for record in _read_json_lines(log_path)
    )
    assert json.loads(artifact_path.read_text(encoding="utf-8"))["ok"] is True

    with _connect_to_schema(postgres_schema) as conn:
        marker = conn.execute(
            "SELECT value FROM gobby_migration_state WHERE key = 'imported_from_sqlite_at'"
        ).fetchone()
        copied = conn.execute(
            "SELECT value FROM config_store WHERE key = 'gobby.test.import'"
        ).fetchone()

    assert marker is not None
    assert copied == {"value": "copied"}


def test_validate_migration_uses_real_bm25_catalog_for_empty_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    postgres_schema: str,
) -> None:
    _allow_minimal_source_schema(monkeypatch)
    _require_pg_search(postgres_schema)
    source = _sqlite_tasks_source()
    with _connect_to_schema(postgres_schema) as conn:
        _create_tasks_bm25_target(conn)

        report = validate_migration(source, conn, artifact_dir=tmp_path / "artifacts")

    bm25 = next(check for check in report.checks if check.name == "bm25 indexes")
    assert bm25.ok is True
    assert bm25.samples == [
        {"table": "tasks", "state": "empty-source", "index": "tasks_search_bm25"}
    ]


def test_validate_migration_fails_when_real_bm25_index_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    postgres_schema: str,
) -> None:
    _allow_minimal_source_schema(monkeypatch)
    _require_pg_search(postgres_schema)
    source = _sqlite_tasks_source()
    with _connect_to_schema(postgres_schema) as conn:
        _create_tasks_bm25_target(conn)
        conn.execute("DROP INDEX tasks_search_bm25")

        with pytest.raises(MigrationValidationError, match="BM25 index validation failed"):
            validate_migration(source, conn, artifact_dir=tmp_path / "artifacts")


def _target_url(schema: str) -> str:
    base_url = os.environ["DATABASE_URL"]
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}options=-csearch_path%3D{schema}"


def _allow_minimal_source_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.storage.migration.validation.validate_sqlite_source_schema",
        lambda _source: SimpleNamespace(ok=True, message="SQLite schema baseline test bypass"),
    )


def _initialize_postgres_target(target_url: str) -> None:
    db = PostgresHubDatabase(target_url)
    try:
        db.apply_migrations()
    finally:
        db.close()


def _sqlite_hub_source(tmp_path: Path) -> Path:
    source = tmp_path / "gobby-hub.db"
    db = SqliteHubDatabase(str(source))
    try:
        db.apply_migrations()
        db.execute(
            """
            INSERT INTO config_store (key, value, source, is_secret, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("gobby.test.import", "copied", "test", 0, "2026-05-20T00:00:00+00:00"),
        )
    finally:
        db.close()
    return source


def _sqlite_tasks_source() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (BASELINE_VERSION,))
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT
        )
        """
    )
    return conn


def _connect_to_schema(schema: str) -> psycopg.Connection[Any]:
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row)
    conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
    return conn


def _require_pg_search(schema: str) -> None:
    with _connect_to_schema(schema) as conn:
        row = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_search'").fetchone()
    if row is None:
        pytest.skip("pg_search extension is required for migration integration tests")


def _create_tasks_bm25_target(conn: psycopg.Connection[Any]) -> None:
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX tasks_search_bm25 ON tasks
        USING bm25 (id, title, description)
        WITH (key_field='id')
        """
    )


def _postgres_snapshot_hash(schema: str) -> str:
    digest = hashlib.sha256()
    with _connect_to_schema(schema) as conn:
        tables = [
            row["tablename"]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            ).fetchall()
        ]
        for table in sorted(tables):
            digest.update(f"table:{table}\n".encode())
            rows = conn.execute(
                sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
            ).fetchall()
            for encoded in sorted(_canonical_json(row) for row in rows):
                digest.update(encoded.encode("utf-8"))
                digest.update(b"\n")
        indexes = conn.execute(
            """
            SELECT indexname, indexdef
              FROM pg_indexes
             WHERE schemaname = current_schema()
             ORDER BY indexname
            """
        ).fetchall()
        for row in indexes:
            digest.update(_canonical_json(row).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
