"""Copy migration for workflow_type='rule' rows into rule_definitions."""

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
_MIGRATION = _REPO / "crates/gcore/assets/schema/migrations/378_copy_rule_definitions.sql"

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

CREATE TABLE rule_definitions (
    id uuid PRIMARY KEY,
    project_id uuid,
    name text NOT NULL,
    description text,
    enabled boolean DEFAULT true NOT NULL,
    enabled_pinned boolean DEFAULT false NOT NULL,
    priority integer DEFAULT 100 NOT NULL,
    sources jsonb,
    definition_json jsonb NOT NULL,
    source text DEFAULT 'installed'::text NOT NULL,
    tags jsonb,
    deleted_at timestamptz,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT rule_definitions_source_check CHECK (
        (source = ANY (ARRAY['installed'::text, 'custom'::text, 'project'::text]))
    )
);
CREATE UNIQUE INDEX uq_rule_defs_live_name
    ON rule_definitions USING btree (name, project_id) NULLS NOT DISTINCT
    WHERE (deleted_at IS NULL);

CREATE TABLE legacy_copy_ledger (
    legacy_id uuid PRIMARY KEY,
    domain text NOT NULL,
    source_hash text NOT NULL,
    copied_at timestamptz DEFAULT now() NOT NULL
);
"""

_BLOCK_BODY = {
    "event": "before_tool",
    "effects": [{"type": "block", "reason": "nope", "tools": ["Bash"]}],
}


@pytest.fixture
def copy_schema(postgres_database_url: str) -> Iterator[tuple[str, str]]:
    schema = f"gobby_test_rulecopy_{uuid.uuid4().hex[:12]}"
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
    definition: dict[str, Any] | None = None,
    workflow_type: str = "rule",
    source: str = "installed",
    enabled: bool = True,
    enabled_user_modified: bool = False,
    priority: int = 100,
    sources: list[str] | None = None,
    tags: list[str] | None = None,
    deleted: bool = False,
    description: str | None = None,
    project_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO workflow_definitions (
            id, project_id, name, description, workflow_type, enabled,
            enabled_user_modified, priority, sources, definition_json, source,
            tags, deleted_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
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
            priority,
            Jsonb(sources) if sources is not None else None,
            Jsonb(definition or {**_BLOCK_BODY, "name": name}),
            source,
            Jsonb(tags or ["gobby"]),
            deleted,
        ),
    )


def test_first_run_copies_rules_including_soft_deleted(
    copy_schema: tuple[str, str],
) -> None:
    url, schema = copy_schema
    live_ids = [str(uuid.uuid4()) for _ in range(160)]
    deleted_ids = [str(uuid.uuid4()) for _ in range(3)]
    skipped_id = str(uuid.uuid4())
    pinned_id = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        for index, row_id in enumerate(live_ids):
            _insert_legacy(
                conn,
                row_id=row_id,
                name=f"rule-{index:03d}",
                priority=10 + (index % 5),
                sources=["claude"],
                tags=["gobby", "bulk"],
            )
        _insert_legacy(
            conn,
            row_id=pinned_id,
            name="pinned-rule",
            source="template",
            enabled_user_modified=True,
            priority=1,
            sources=["codex"],
            tags=["gobby"],
        )
        for index, row_id in enumerate(deleted_ids):
            _insert_legacy(
                conn,
                row_id=row_id,
                name=f"retired-rule-{index}",
                deleted=True,
                priority=50,
            )
        _insert_legacy(
            conn,
            row_id=skipped_id,
            name="not-a-rule",
            workflow_type="variable",
        )
        _apply_copy(conn)

        copied = conn.execute(
            "SELECT id, name, source, enabled_pinned, priority, sources, tags, deleted_at "
            "FROM rule_definitions"
        ).fetchall()
        copied_ids = {str(row["id"]) for row in copied}
        assert len(copied) == 164
        assert copied_ids == set(live_ids) | set(deleted_ids) | {pinned_id}
        assert skipped_id not in copied_ids

        by_id = {str(row["id"]): row for row in copied}
        assert by_id[pinned_id]["source"] == "installed"
        assert by_id[pinned_id]["enabled_pinned"] is True
        assert by_id[pinned_id]["priority"] == 1
        assert by_id[pinned_id]["sources"] == ["codex"]
        assert all(by_id[row_id]["deleted_at"] is not None for row_id in deleted_ids)

        ledger = conn.execute(
            "SELECT legacy_id, domain, source_hash FROM legacy_copy_ledger"
        ).fetchall()
        assert {str(row["legacy_id"]) for row in ledger} == copied_ids
        assert all(row["domain"] == "rules" and row["source_hash"] for row in ledger)


def test_rerun_over_live_rows(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    live_id = str(uuid.uuid4())
    first_deleted = str(uuid.uuid4())
    twin_deleted = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        _insert_legacy(conn, row_id=live_id, name="live-rule", priority=7)
        _insert_legacy(
            conn,
            row_id=first_deleted,
            name="shared-name",
            definition={**_BLOCK_BODY, "variant": "a"},
            deleted=True,
        )
        _insert_legacy(
            conn,
            row_id=twin_deleted,
            name="shared-name",
            definition={**_BLOCK_BODY, "variant": "b"},
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
        rows = conn.execute("SELECT id, definition_json FROM rule_definitions").fetchall()
        assert {str(row["id"]) for row in rows} == {live_id, first_deleted, twin_deleted}
        by_id = {str(row["id"]): row["definition_json"] for row in rows}
        assert by_id[first_deleted]["variant"] == "a"
        assert by_id[twin_deleted]["variant"] == "b"


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
            "SELECT id FROM rule_definitions WHERE name = %s", ("retired-once",)
        ).fetchall()
        assert {str(row["id"]) for row in rows} == {row_id}


def test_divergent_payload_fails_loudly(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    row_id = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        _insert_legacy(
            conn,
            row_id=row_id,
            name="no-push",
            definition={**_BLOCK_BODY, "reason": "legacy"},
        )
        conn.execute(
            """
            INSERT INTO rule_definitions (
                id, name, description, enabled, enabled_pinned, priority,
                definition_json, source
            ) VALUES (%s, %s, %s, true, false, 100, %s, 'installed')
            """,
            (row_id, "no-push", "no-push", Jsonb({**_BLOCK_BODY, "reason": "typed-drift"})),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="no-push"):
            _apply_copy(conn)


def test_divergent_identity_fails(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    source_id = str(uuid.uuid4())
    typed_id = str(uuid.uuid4())
    body = {**_BLOCK_BODY, "reason": "same"}
    with _connect(url, schema) as conn:
        _insert_legacy(conn, row_id=source_id, name="no-push", definition=body)
        conn.execute(
            """
            INSERT INTO rule_definitions (
                id, name, description, enabled, enabled_pinned, priority,
                definition_json, source
            ) VALUES (%s, %s, %s, true, false, 100, %s, 'installed')
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
        _insert_legacy(holder, row_id=row_id, name="locked-rule")
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
            "SELECT description FROM rule_definitions WHERE id = %s",
            (row_id,),
        ).fetchone()
        drifted = waiter.execute(
            "SELECT description FROM workflow_definitions WHERE id = %s",
            (row_id,),
        ).fetchone()
        assert copied is not None and copied["description"] == "locked-rule"
        assert drifted is not None and drifted["description"] == "drift"
    finally:
        holder.close()
        waiter.close()


def test_absent_legacy_table_is_receipted_noop(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    with _connect(url, schema) as conn:
        conn.execute("DROP TABLE workflow_definitions")
        _apply_copy(conn)
        remaining = conn.execute("SELECT count(*) AS n FROM rule_definitions").fetchone()
        assert remaining is not None and int(remaining["n"]) == 0
