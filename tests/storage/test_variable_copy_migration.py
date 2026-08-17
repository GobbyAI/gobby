"""Copy migration for workflow_type='variable' rows into session_variable_defaults."""

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
_MIGRATION = _REPO / "crates/gcore/assets/schema/migrations/379_copy_session_variable_defaults.sql"

_SCHEMA_SQL = """
CREATE TABLE workflow_definitions (
    id uuid PRIMARY KEY,
    project_id uuid,
    name text NOT NULL,
    description text,
    workflow_type text DEFAULT 'workflow'::text NOT NULL,
    enabled boolean DEFAULT true,
    enabled_user_modified boolean DEFAULT false NOT NULL,
    priority integer DEFAULT 100,
    sources jsonb,
    definition_json jsonb NOT NULL,
    source text DEFAULT 'installed'::text,
    tags jsonb,
    deleted_at timestamptz,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);

CREATE TABLE session_variable_defaults (
    id uuid PRIMARY KEY,
    project_id uuid,
    name text NOT NULL,
    description text,
    enabled boolean DEFAULT true NOT NULL,
    enabled_pinned boolean DEFAULT false NOT NULL,
    default_value jsonb,
    source text DEFAULT 'installed'::text NOT NULL,
    tags jsonb,
    deleted_at timestamptz,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT session_variable_defaults_source_check CHECK (
        (source = ANY (ARRAY['installed'::text, 'custom'::text, 'project'::text]))
    )
);
CREATE UNIQUE INDEX uq_session_var_defs_live_name
    ON session_variable_defaults USING btree (name, project_id) NULLS NOT DISTINCT
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
    schema = f"gobby_test_varcopy_{uuid.uuid4().hex[:12]}"
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
    conn.execute(_MIGRATION.read_text(encoding="utf-8"))


def _insert_legacy(
    conn: psycopg.Connection[Any],
    *,
    row_id: str,
    name: str,
    value: Any = None,
    variable: str | None = None,
    workflow_type: str = "variable",
    source: str = "installed",
    enabled: bool = True,
    enabled_user_modified: bool = False,
    tags: list[str] | None = None,
    deleted: bool = False,
    description: str | None = None,
    project_id: str | None = None,
) -> None:
    body = {"variable": variable if variable is not None else name, "value": value}
    conn.execute(
        """
        INSERT INTO workflow_definitions (
            id, project_id, name, description, workflow_type, enabled,
            enabled_user_modified, priority, sources, definition_json, source,
            tags, deleted_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, 100, NULL, %s, %s, %s,
            CASE WHEN %s THEN now() ELSE NULL END
        )
        """,
        (
            row_id,
            project_id,
            name,
            description or name,
            workflow_type,
            enabled,
            enabled_user_modified,
            Jsonb(body),
            source,
            Jsonb(tags or ["gobby"]),
            deleted,
        ),
    )


def test_first_run_copies_variables_including_soft_deleted(
    copy_schema: tuple[str, str],
) -> None:
    url, schema = copy_schema
    live_ids = [str(uuid.uuid4()) for _ in range(40)]
    gobby_id = str(uuid.uuid4())
    aliased_id = str(uuid.uuid4())
    deleted_id = str(uuid.uuid4())
    skipped_id = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        for index, row_id in enumerate(live_ids):
            _insert_legacy(
                conn,
                row_id=row_id,
                name=f"var-{index:03d}",
                value=index,
            )
        _insert_legacy(
            conn,
            row_id=gobby_id,
            name="legacy-gobby-var",
            value="anomaly",
            source="gobby",
            enabled_user_modified=True,
        )
        _insert_legacy(
            conn,
            row_id=aliased_id,
            name="row-name",
            variable="canonical_name",
            value={"nested": True},
        )
        _insert_legacy(
            conn,
            row_id=deleted_id,
            name="deleted-legacy-var",
            value="gone",
            deleted=True,
        )
        _insert_legacy(
            conn,
            row_id=skipped_id,
            name="not-a-variable",
            workflow_type="rule",
        )
        _apply_copy(conn)

        copied = conn.execute(
            "SELECT id, name, source, enabled_pinned, default_value, deleted_at "
            "FROM session_variable_defaults"
        ).fetchall()
        copied_ids = {str(row["id"]) for row in copied}
        expected = set(live_ids) | {gobby_id, aliased_id, deleted_id}
        assert len(copied) == 43
        assert copied_ids == expected
        assert skipped_id not in copied_ids

        by_id = {str(row["id"]): row for row in copied}
        assert by_id[gobby_id]["source"] == "installed"
        assert by_id[gobby_id]["enabled_pinned"] is True
        assert by_id[gobby_id]["name"] == "legacy-gobby-var"
        assert by_id[aliased_id]["name"] == "canonical_name"
        assert by_id[aliased_id]["default_value"] == {"nested": True}
        assert by_id[deleted_id]["deleted_at"] is not None

        ledger = conn.execute(
            "SELECT legacy_id, domain, source_hash FROM legacy_copy_ledger"
        ).fetchall()
        assert {str(row["legacy_id"]) for row in ledger} == copied_ids
        assert all(row["domain"] == "variables" and row["source_hash"] for row in ledger)


def test_rerun_over_live_rows(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    live_id = str(uuid.uuid4())
    first_deleted = str(uuid.uuid4())
    twin_deleted = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        _insert_legacy(conn, row_id=live_id, name="live-var", value="keep")
        _insert_legacy(
            conn,
            row_id=first_deleted,
            name="shared-name",
            value="a",
            deleted=True,
        )
        _insert_legacy(
            conn,
            row_id=twin_deleted,
            name="shared-name",
            value="b",
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
        rows = conn.execute("SELECT id, default_value FROM session_variable_defaults").fetchall()
        assert {str(row["id"]) for row in rows} == {live_id, first_deleted, twin_deleted}
        by_id = {str(row["id"]): row["default_value"] for row in rows}
        assert by_id[first_deleted] == "a"
        assert by_id[twin_deleted] == "b"


def test_rerun_over_soft_deleted_rows(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    row_id = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        _insert_legacy(conn, row_id=row_id, name="retired-once", value=1, deleted=True)
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
            "SELECT id FROM session_variable_defaults WHERE name = %s",
            ("retired-once",),
        ).fetchall()
        assert {str(row["id"]) for row in rows} == {row_id}


def test_divergent_payload_fails_loudly(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    row_id = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        _insert_legacy(conn, row_id=row_id, name="no-push", value="legacy")
        conn.execute(
            """
            INSERT INTO session_variable_defaults (
                id, name, description, enabled, enabled_pinned, default_value, source
            ) VALUES (%s, %s, %s, true, false, %s, 'installed')
            """,
            (row_id, "no-push", "no-push", Jsonb("typed-drift")),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="no-push"):
            _apply_copy(conn)


def test_divergent_identity_fails(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    source_id = str(uuid.uuid4())
    typed_id = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        _insert_legacy(conn, row_id=source_id, name="no-push", value="same")
        conn.execute(
            """
            INSERT INTO session_variable_defaults (
                id, name, description, enabled, enabled_pinned, default_value, source
            ) VALUES (%s, %s, %s, true, false, %s, 'installed')
            """,
            (typed_id, "no-push", "no-push", Jsonb("same")),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="no-push"):
            _apply_copy(conn)


def test_copy_lock_fences_concurrent_writes(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    row_id = str(uuid.uuid4())
    holder = _connect(url, schema, autocommit=False)
    waiter = _connect(url, schema, autocommit=True)
    try:
        _insert_legacy(holder, row_id=row_id, name="locked-var", value=1)
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
            "SELECT description FROM session_variable_defaults WHERE id = %s",
            (row_id,),
        ).fetchone()
        drifted = waiter.execute(
            "SELECT description FROM workflow_definitions WHERE id = %s",
            (row_id,),
        ).fetchone()
        assert copied is not None and copied["description"] == "locked-var"
        assert drifted is not None and drifted["description"] == "drift"
    finally:
        holder.close()
        waiter.close()


def test_absent_legacy_table_is_receipted_noop(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    with _connect(url, schema) as conn:
        conn.execute("DROP TABLE workflow_definitions")
        _apply_copy(conn)
        remaining = conn.execute("SELECT count(*) AS n FROM session_variable_defaults").fetchone()
        assert remaining is not None and int(remaining["n"]) == 0
