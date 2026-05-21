from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from gobby.storage.migration import sqlite_to_postgres as migration
from gobby.storage.migration import validation
from gobby.storage.migrations import BASELINE_VERSION, _execute_sql_script, _sqlite_baseline_sql

pytestmark = pytest.mark.integration


def test_migrate_sqlite_to_postgres_real_target_dry_run_import_and_bm25_validation(
    postgres_db: object,
    postgres_schema: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del postgres_db
    target = _target_url(postgres_schema)
    source_path, task_id = _sqlite_source_with_task(tmp_path)
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))

    before_hash = _target_hash(target)
    dry_run = migration.migrate_sqlite_to_postgres(
        source=source_path,
        target=target,
        dry_run=True,
        emit=lambda _line: None,
    )

    assert dry_run["dry_run"] is True
    assert dry_run["validation_artifact"] is None
    assert _target_hash(target) == before_hash

    imported = migration.migrate_sqlite_to_postgres(
        source=source_path,
        target=target,
        batch_size=2,
        dry_run=False,
        emit=lambda _line: None,
    )

    assert imported["dry_run"] is False
    assert imported["rows"] > 0
    assert imported["tables"] > 0
    _assert_task_and_marker_copied(target, task_id)
    _assert_import_log(imported["log_path"])
    artifact = _load_validation_artifact(imported["validation_artifact"])
    _assert_bm25_artifact_records_smoke_and_empty_source(artifact, source_path)
    _assert_dropped_bm25_index_fails_validation(target, source_path, tmp_path)


def _sqlite_source_with_task(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "gobby-hub.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _execute_sql_script(conn, _sqlite_baseline_sql("schema_version"))
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (BASELINE_VERSION,))
        project_id = conn.execute("SELECT id FROM projects WHERE name = '_personal'").fetchone()[
            "id"
        ]
        task_id = "task-migration-bm25-smoke"
        conn.execute(
            """
            INSERT INTO tasks (id, project_id, title, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                project_id,
                "migration validation needle",
                "bm25 smoke query content",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return path, task_id


def _target_url(postgres_schema: str) -> str:
    base_url = os.environ["DATABASE_URL"]
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}options=-csearch_path%3D{postgres_schema}"


def _target_hash(target: str) -> str:
    payload: list[dict[str, object]] = []
    with psycopg.connect(target, row_factory=dict_row) as conn:
        tables = [
            row["tablename"]
            for row in conn.execute(
                """
                SELECT tablename
                  FROM pg_tables
                 WHERE schemaname = current_schema()
                 ORDER BY tablename
                """
            ).fetchall()
        ]
        for table in tables:
            rows = conn.execute(
                sql.SQL(
                    """
                    SELECT row_to_json(t)::text AS row_json
                      FROM (SELECT * FROM {}) AS t
                     ORDER BY row_json
                    """
                ).format(sql.Identifier(str(table)))
            ).fetchall()
            payload.append({"table": str(table), "rows": [str(row["row_json"]) for row in rows]})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _assert_task_and_marker_copied(target: str, task_id: str) -> None:
    with psycopg.connect(target, row_factory=dict_row) as conn:
        task = conn.execute(
            "SELECT title, description FROM tasks WHERE id = %s",
            (task_id,),
        ).fetchone()
        assert task == {
            "title": "migration validation needle",
            "description": "bm25 smoke query content",
        }
        marker = conn.execute(
            """
            SELECT value
              FROM gobby_migration_state
             WHERE key = 'imported_from_sqlite_at'
            """
        ).fetchone()
        assert marker is not None
        assert marker["value"]


def _assert_import_log(log_path: object) -> None:
    assert isinstance(log_path, str)
    records = [json.loads(line) for line in Path(log_path).read_text().splitlines()]
    task_end = [
        record
        for record in records
        if record["event"] == "table_copy_end" and record["table"] == "tasks"
    ]
    assert len(task_end) == 1
    assert task_end[0]["rows"] == 1
    assert any(record["event"] == "table_copy_start" for record in records)


def _load_validation_artifact(artifact_path: object) -> dict[str, Any]:
    assert isinstance(artifact_path, str)
    artifact = json.loads(Path(artifact_path).read_text())
    assert artifact["ok"] is True
    assert any(check["name"] == "row counts" for check in artifact["checks"])
    return artifact


def _assert_bm25_artifact_records_smoke_and_empty_source(
    artifact: dict[str, Any],
    source_path: Path,
) -> None:
    bm25 = next(check for check in artifact["checks"] if check["name"] == "bm25 indexes")
    states = {sample["table"]: sample["state"] for sample in bm25["samples"]}
    assert states["tasks"] == "smoke-query"
    empty_tables = {
        table
        for table in ("memories", "code_symbols", "code_content_chunks", "skills")
        if _sqlite_count(source_path, table) == 0
    }
    assert empty_tables
    for table in empty_tables:
        assert states[table] == "empty-source"


def _assert_dropped_bm25_index_fails_validation(
    target: str,
    source_path: Path,
    tmp_path: Path,
) -> None:
    with sqlite3.connect(source_path) as source:
        source.row_factory = sqlite3.Row
        with psycopg.connect(target, autocommit=True, row_factory=dict_row) as pg:
            pg.execute(sql.SQL("DROP INDEX {}").format(sql.Identifier("tasks_search_bm25")))
            try:
                with pytest.raises(
                    validation.MigrationValidationError,
                    match="BM25 index validation failed",
                ):
                    validation.validate_migration(
                        source,
                        pg,
                        artifact_dir=tmp_path / "dropped-index-validation",
                    )
            finally:
                migration._drop_bm25_indexes(pg)
                migration._recreate_bm25_indexes(pg)


def _sqlite_count(source_path: Path, table: str) -> int:
    with sqlite3.connect(source_path) as conn:
        row = conn.execute(f"SELECT COUNT(*) FROM {validation._quote_identifier(table)}").fetchone()
        return int(row[0])
