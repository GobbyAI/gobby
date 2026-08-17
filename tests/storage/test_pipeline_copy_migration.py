"""Copy migration for workflow_type='pipeline' rows into pipeline_definitions."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.integration

_REPO = Path(__file__).resolve().parents[2]
_MIGRATION = _REPO / "crates/gcore/assets/schema/migrations/380_copy_pipeline_definitions.sql"

_SCHEMA_SQL = """
CREATE TABLE workflow_definitions (
    id uuid PRIMARY KEY,
    project_id uuid,
    name text NOT NULL,
    description text,
    workflow_type text DEFAULT 'workflow'::text NOT NULL,
    version text DEFAULT '1.0'::text,
    enabled boolean DEFAULT true,
    enabled_user_modified boolean DEFAULT false NOT NULL,
    priority integer DEFAULT 100,
    sources jsonb,
    definition_json jsonb NOT NULL,
    canvas_json jsonb,
    source text DEFAULT 'installed'::text,
    tags jsonb,
    deleted_at timestamptz,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);

CREATE TABLE pipeline_definitions (
    id uuid PRIMARY KEY,
    project_id uuid,
    name text NOT NULL,
    description text,
    enabled boolean DEFAULT true NOT NULL,
    enabled_pinned boolean DEFAULT false NOT NULL,
    version text DEFAULT '1.0'::text NOT NULL,
    definition_json jsonb NOT NULL,
    canvas_json jsonb,
    source text DEFAULT 'installed'::text NOT NULL,
    tags jsonb,
    deleted_at timestamptz,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pipeline_definitions_source_check CHECK (
        (source = ANY (ARRAY['installed'::text, 'custom'::text, 'project'::text]))
    )
);
CREATE UNIQUE INDEX uq_pipeline_defs_live_name
    ON pipeline_definitions USING btree (name, project_id) NULLS NOT DISTINCT
    WHERE (deleted_at IS NULL);

CREATE TABLE legacy_copy_ledger (
    legacy_id uuid PRIMARY KEY,
    domain text NOT NULL,
    source_hash text NOT NULL,
    copied_at timestamptz DEFAULT now() NOT NULL
);
"""


@pytest.fixture
def copy_schema(postgres_database_url: str) -> Iterator[tuple[str, str]]:
    schema = f"gobby_test_pipecopy_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(postgres_database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.execute(_SCHEMA_SQL)
    try:
        yield postgres_database_url, schema
    finally:
        with psycopg.connect(postgres_database_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def _connect(url: str, schema: str, *, autocommit: bool = True) -> psycopg.Connection[Any]:
    conn = psycopg.connect(url, autocommit=autocommit, row_factory=dict_row)
    conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
    return conn


def _apply_copy(conn: psycopg.Connection[Any]) -> None:
    assert _MIGRATION.is_file(), f"missing pipeline copy migration: {_MIGRATION}"
    conn.execute(_MIGRATION.read_text(encoding="utf-8"))


def _pipeline_body(name: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": name,
        "type": "pipeline",
        "steps": [{"id": "s1", "exec": "echo hi"}],
    }
    if extra:
        body.update(extra)
    return body


def _insert_legacy(
    conn: psycopg.Connection[Any],
    *,
    row_id: str,
    name: str,
    workflow_type: str = "pipeline",
    source: str = "installed",
    enabled: bool = True,
    enabled_user_modified: bool = False,
    version: str = "1.0",
    tags: list[str] | None = None,
    deleted: bool = False,
    description: str | None = None,
    project_id: str | None = None,
    definition: dict[str, Any] | None = None,
    canvas: dict[str, Any] | None = None,
) -> None:
    body = definition if definition is not None else _pipeline_body(name)
    conn.execute(
        """
        INSERT INTO workflow_definitions (
            id, project_id, name, description, workflow_type, version, enabled,
            enabled_user_modified, priority, sources, definition_json, canvas_json,
            source, tags, deleted_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, 100, NULL, %s, %s, %s, %s,
            CASE WHEN %s THEN now() ELSE NULL END
        )
        """,
        (
            row_id,
            project_id,
            name,
            description or name,
            workflow_type,
            version,
            enabled,
            enabled_user_modified,
            Jsonb(body),
            Jsonb(canvas) if canvas is not None else None,
            source,
            Jsonb(tags or ["gobby"]),
            deleted,
        ),
    )


def test_first_run_copies_eleven_pipelines_including_soft_deleted(
    copy_schema: tuple[str, str],
) -> None:
    url, schema = copy_schema
    live_ids = [str(uuid.uuid4()) for _ in range(9)]
    gobby_id = str(uuid.uuid4())
    deleted_id = str(uuid.uuid4())
    skipped_id = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        for index, row_id in enumerate(live_ids):
            _insert_legacy(
                conn,
                row_id=row_id,
                name=f"pipe-{index:03d}",
                version="2.0",
                canvas={"x": index},
            )
        _insert_legacy(
            conn,
            row_id=gobby_id,
            name="legacy-gobby-pipe",
            source="gobby",
            enabled_user_modified=True,
        )
        _insert_legacy(
            conn,
            row_id=deleted_id,
            name="retired-pipe",
            deleted=True,
        )
        _insert_legacy(
            conn,
            row_id=skipped_id,
            name="not-a-pipeline",
            workflow_type="rule",
        )
        _apply_copy(conn)

        copied = conn.execute(
            "SELECT id, name, source, enabled_pinned, version, canvas_json, deleted_at "
            "FROM pipeline_definitions"
        ).fetchall()
        copied_ids = {str(row["id"]) for row in copied}
        expected = set(live_ids) | {gobby_id, deleted_id}
        assert len(copied) == 11
        assert copied_ids == expected
        assert skipped_id not in copied_ids

        by_id = {str(row["id"]): row for row in copied}
        assert by_id[gobby_id]["source"] == "installed"
        assert by_id[gobby_id]["enabled_pinned"] is True
        assert by_id[live_ids[0]]["version"] == "2.0"
        assert by_id[live_ids[0]]["canvas_json"] == {"x": 0}
        assert by_id[deleted_id]["deleted_at"] is not None

        ledger = conn.execute(
            "SELECT legacy_id, domain, source_hash FROM legacy_copy_ledger"
        ).fetchall()
        assert {str(row["legacy_id"]) for row in ledger} == copied_ids
        assert all(row["domain"] == "pipelines" and row["source_hash"] for row in ledger)


def test_rerun_over_live_rows(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    live_id = str(uuid.uuid4())
    first_deleted = str(uuid.uuid4())
    twin_deleted = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        _insert_legacy(conn, row_id=live_id, name="live-pipe")
        _insert_legacy(
            conn,
            row_id=first_deleted,
            name="shared-name",
            definition=_pipeline_body("shared-name", {"description": "a"}),
            deleted=True,
        )
        _insert_legacy(
            conn,
            row_id=twin_deleted,
            name="shared-name",
            definition=_pipeline_body("shared-name", {"description": "b"}),
            deleted=True,
        )
        _apply_copy(conn)
        first = conn.execute(
            "SELECT source_hash FROM legacy_copy_ledger WHERE legacy_id = %s",
            (live_id,),
        ).fetchone()
        _apply_copy(conn)
        second = conn.execute(
            "SELECT source_hash FROM legacy_copy_ledger WHERE legacy_id = %s",
            (live_id,),
        ).fetchone()
        assert first is not None and second is not None
        assert first["source_hash"] == second["source_hash"]
        rows = conn.execute("SELECT id, definition_json FROM pipeline_definitions").fetchall()
        assert {str(row["id"]) for row in rows} == {live_id, first_deleted, twin_deleted}
        by_id = {str(row["id"]): row["definition_json"] for row in rows}
        assert by_id[first_deleted]["description"] == "a"
        assert by_id[twin_deleted]["description"] == "b"


def test_rerun_over_soft_deleted_rows(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    row_id = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        _insert_legacy(conn, row_id=row_id, name="retired-once", deleted=True)
        _apply_copy(conn)
        first = conn.execute(
            "SELECT source_hash FROM legacy_copy_ledger WHERE legacy_id = %s",
            (row_id,),
        ).fetchone()
        _apply_copy(conn)
        second = conn.execute(
            "SELECT source_hash FROM legacy_copy_ledger WHERE legacy_id = %s",
            (row_id,),
        ).fetchone()
        assert first is not None and second is not None
        assert first["source_hash"] == second["source_hash"]
        rows = conn.execute(
            "SELECT id FROM pipeline_definitions WHERE name = %s",
            ("retired-once",),
        ).fetchall()
        assert {str(row["id"]) for row in rows} == {row_id}


def test_divergent_payload_fails_loudly(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    row_id = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        _insert_legacy(conn, row_id=row_id, name="no-push")
        conn.execute(
            """
            INSERT INTO pipeline_definitions (
                id, name, description, enabled, enabled_pinned, version,
                definition_json, source
            ) VALUES (%s, %s, %s, true, false, '1.0', %s, 'installed')
            """,
            (
                row_id,
                "no-push",
                "no-push",
                Jsonb(_pipeline_body("no-push", {"description": "typed-drift"})),
            ),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="no-push"):
            _apply_copy(conn)


def test_divergent_identity_fails(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    source_id = str(uuid.uuid4())
    typed_id = str(uuid.uuid4())
    body = _pipeline_body("no-push")
    with _connect(url, schema) as conn:
        _insert_legacy(conn, row_id=source_id, name="no-push", definition=body)
        conn.execute(
            """
            INSERT INTO pipeline_definitions (
                id, name, description, enabled, enabled_pinned, version,
                definition_json, source
            ) VALUES (%s, %s, %s, true, false, '1.0', %s, 'installed')
            """,
            (typed_id, "no-push", "no-push", Jsonb(body)),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="no-push"):
            _apply_copy(conn)


def test_copy_lock_fences_concurrent_writes(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    row_id = str(uuid.uuid4())
    holder = _connect(url, schema, autocommit=False)
    waiter = _connect(url, schema, autocommit=True)
    try:
        _insert_legacy(holder, row_id=row_id, name="locked-pipe")
        holder.commit()
        _apply_copy(holder)
        waiter.execute("SET lock_timeout = '200ms'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            waiter.execute(
                "UPDATE workflow_definitions SET description = %s WHERE id = %s",
                ("drift", row_id),
            )
        holder.commit()
        waiter.execute(
            "UPDATE workflow_definitions SET description = %s WHERE id = %s",
            ("drift", row_id),
        )
        copied = holder.execute(
            "SELECT description FROM pipeline_definitions WHERE id = %s",
            (row_id,),
        ).fetchone()
        drifted = waiter.execute(
            "SELECT description FROM workflow_definitions WHERE id = %s",
            (row_id,),
        ).fetchone()
        assert copied is not None and copied["description"] == "locked-pipe"
        assert drifted is not None and drifted["description"] == "drift"
    finally:
        holder.close()
        waiter.close()


def test_absent_legacy_table_is_receipted_noop(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    with _connect(url, schema) as conn:
        conn.execute("DROP TABLE workflow_definitions")
        _apply_copy(conn)
        remaining = conn.execute("SELECT count(*) AS n FROM pipeline_definitions").fetchone()
        assert remaining is not None and int(remaining["n"]) == 0
