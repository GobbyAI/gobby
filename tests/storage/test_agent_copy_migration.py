"""Copy migration for workflow_type='agent' rows into typed agent tables."""

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
_MIGRATION = _REPO / "crates/gcore/assets/schema/migrations/376_copy_agent_definitions.sql"

_SCHEMA_SQL = """
CREATE TABLE workflow_definitions (
    id uuid PRIMARY KEY,
    project_id uuid,
    name text NOT NULL,
    description text,
    workflow_type text DEFAULT 'workflow'::text NOT NULL,
    enabled boolean DEFAULT true,
    enabled_user_modified boolean DEFAULT false NOT NULL,
    definition_json jsonb NOT NULL,
    source text DEFAULT 'installed'::text,
    tags jsonb,
    deleted_at timestamptz,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);

CREATE TABLE agent_definitions (
    id uuid PRIMARY KEY,
    project_id uuid,
    name text NOT NULL,
    description text,
    enabled boolean DEFAULT true NOT NULL,
    enabled_pinned boolean DEFAULT false NOT NULL,
    definition_json jsonb NOT NULL,
    source text DEFAULT 'installed'::text NOT NULL,
    tags jsonb,
    deleted_at timestamptz,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT agent_definitions_source_check CHECK (
        (source = ANY (ARRAY['installed'::text, 'custom'::text, 'project'::text]))
    )
);
CREATE UNIQUE INDEX uq_agent_defs_live_name
    ON agent_definitions USING btree (name, project_id) NULLS NOT DISTINCT
    WHERE (deleted_at IS NULL);

CREATE TABLE agent_step_workflows (
    id uuid PRIMARY KEY,
    agent_definition_id uuid NOT NULL UNIQUE
        REFERENCES agent_definitions(id) ON DELETE CASCADE,
    steps_json jsonb NOT NULL,
    variables_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    exit_condition text,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);

CREATE TABLE legacy_copy_ledger (
    legacy_id uuid PRIMARY KEY,
    domain text NOT NULL,
    source_hash text NOT NULL,
    copied_at timestamptz DEFAULT now() NOT NULL
);
"""

_FLAT_STEPS = [{"name": "implement", "prompt": "write it"}]
_NESTED_STEPS = [{"name": "review", "prompt": "check it"}]


def _now_ids() -> dict[str, str]:
    return {
        "flat": str(uuid.uuid4()),
        "nested": str(uuid.uuid4()),
        "null_steps": str(uuid.uuid4()),
        "deleted": str(uuid.uuid4()),
        "generated": str(uuid.uuid4()),
    }


@pytest.fixture
def copy_schema(postgres_database_url: str) -> Iterator[tuple[str, str]]:
    schema = f"gobby_test_agentcopy_{uuid.uuid4().hex[:12]}"
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
    definition: dict[str, Any],
    workflow_type: str = "agent",
    source: str = "installed",
    enabled: bool = True,
    enabled_user_modified: bool = False,
    deleted: bool = False,
    description: str | None = None,
    project_id: str | None = None,
    tags: list[str] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO workflow_definitions (
            id, project_id, name, description, workflow_type, enabled,
            enabled_user_modified, definition_json, source, tags, deleted_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
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
            Jsonb(definition),
            source,
            Jsonb(tags or ["gobby"]),
            deleted,
        ),
    )


def _seed_mixed(conn: psycopg.Connection[Any], ids: dict[str, str]) -> None:
    _insert_legacy(
        conn,
        row_id=ids["flat"],
        name="flat-agent",
        definition={
            "name": "flat-agent",
            "provider": "claude",
            "steps": _FLAT_STEPS,
            "step_variables": {"goal": "ship"},
            "exit_condition": "done",
        },
        source="template",
        enabled_user_modified=True,
    )
    _insert_legacy(
        conn,
        row_id=ids["nested"],
        name="nested-agent",
        definition={
            "name": "nested-agent",
            "provider": "codex",
            "step_workflow": {
                "variables": {"goal": "review"},
                "exit_condition": "pass",
                "steps": _NESTED_STEPS,
            },
        },
    )
    _insert_legacy(
        conn,
        row_id=ids["null_steps"],
        name="comms-agent",
        definition={"name": "comms-agent", "provider": "claude", "steps": None},
    )
    _insert_legacy(
        conn,
        row_id=ids["deleted"],
        name="retired-agent",
        definition={
            "name": "retired-agent",
            "steps": [{"name": "old"}],
            "step_variables": {},
            "exit_condition": None,
        },
        deleted=True,
    )
    _insert_legacy(
        conn,
        row_id=ids["generated"],
        name="flat-agent-steps",
        workflow_type="workflow",
        source="agent",
        definition={
            "name": "flat-agent-steps",
            "type": "step",
            "steps": _FLAT_STEPS,
            "variables": {},
        },
    )


def _parent_body(row_id: str, conn: psycopg.Connection[Any]) -> dict[str, Any]:
    row = conn.execute(
        "SELECT definition_json FROM agent_definitions WHERE id = %s",
        (row_id,),
    ).fetchone()
    assert row is not None
    body = row["definition_json"]
    assert isinstance(body, dict)
    return body


def _child(row_id: str, conn: psycopg.Connection[Any]) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT steps_json, variables_json, exit_condition "
        "FROM agent_step_workflows WHERE agent_definition_id = %s",
        (row_id,),
    ).fetchone()
    return None if row is None else dict(row)


def test_first_run_copies_mixed_shapes_and_skips_generated(
    copy_schema: tuple[str, str],
) -> None:
    url, schema = copy_schema
    ids = _now_ids()
    with _connect(url, schema) as conn:
        _seed_mixed(conn, ids)
        _apply_copy(conn)

        parents = conn.execute(
            "SELECT id, name, source, enabled_pinned, deleted_at FROM agent_definitions"
        ).fetchall()
        assert {str(row["id"]) for row in parents} == {
            ids["flat"],
            ids["nested"],
            ids["null_steps"],
            ids["deleted"],
        }
        by_id = {str(row["id"]): row for row in parents}
        assert by_id[ids["flat"]]["source"] == "installed"
        assert by_id[ids["flat"]]["enabled_pinned"] is True
        assert by_id[ids["deleted"]]["deleted_at"] is not None

        flat_body = _parent_body(ids["flat"], conn)
        nested_body = _parent_body(ids["nested"], conn)
        null_body = _parent_body(ids["null_steps"], conn)
        for body in (flat_body, nested_body, null_body):
            assert "steps" not in body
            assert "step_variables" not in body
            assert "exit_condition" not in body
            assert "step_workflow" not in body

        flat_child = _child(ids["flat"], conn)
        nested_child = _child(ids["nested"], conn)
        assert flat_child is not None
        assert flat_child["steps_json"] == _FLAT_STEPS
        assert flat_child["variables_json"] == {"goal": "ship"}
        assert flat_child["exit_condition"] == "done"
        assert nested_child is not None
        assert nested_child["steps_json"] == _NESTED_STEPS
        assert nested_child["variables_json"] == {"goal": "review"}
        assert nested_child["exit_condition"] == "pass"
        assert _child(ids["null_steps"], conn) is None

        generated = conn.execute(
            "SELECT count(*) AS n FROM agent_definitions WHERE name = %s",
            ("flat-agent-steps",),
        ).fetchone()
        assert generated is not None and int(generated["n"]) == 0

        ledger = conn.execute(
            "SELECT legacy_id, domain, source_hash FROM legacy_copy_ledger"
        ).fetchall()
        assert {str(row["legacy_id"]) for row in ledger} == {
            ids["flat"],
            ids["nested"],
            ids["null_steps"],
            ids["deleted"],
        }
        assert all(row["domain"] == "agents" and row["source_hash"] for row in ledger)


def test_nested_and_flat_source_shapes(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    ids = _now_ids()
    with _connect(url, schema) as conn:
        _seed_mixed(conn, ids)
        _apply_copy(conn)
        assert "steps" not in _parent_body(ids["flat"], conn)
        assert "step_workflow" not in _parent_body(ids["nested"], conn)
        flat_child = _child(ids["flat"], conn)
        nested_child = _child(ids["nested"], conn)
        assert flat_child is not None
        assert nested_child is not None
        assert flat_child["steps_json"] == _FLAT_STEPS
        assert nested_child["steps_json"] == _NESTED_STEPS


def test_rerun_over_live_rows_is_idempotent(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    ids = _now_ids()
    with _connect(url, schema) as conn:
        _seed_mixed(conn, ids)
        _apply_copy(conn)
        first = conn.execute(
            "SELECT source_hash FROM legacy_copy_ledger WHERE legacy_id = %s",
            (ids["flat"],),
        ).fetchone()
        _apply_copy(conn)
        second = conn.execute(
            "SELECT source_hash FROM legacy_copy_ledger WHERE legacy_id = %s",
            (ids["flat"],),
        ).fetchone()
        assert first is not None and second is not None
        assert first["source_hash"] == second["source_hash"]
        children = conn.execute("SELECT count(*) AS n FROM agent_step_workflows").fetchone()
        assert children is not None and int(children["n"]) == 3


def test_rerun_over_soft_deleted_rows(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    first_id = str(uuid.uuid4())
    twin_id = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        _insert_legacy(
            conn,
            row_id=first_id,
            name="shared-name",
            definition={"name": "shared-name", "steps": [{"name": "a"}]},
            deleted=True,
        )
        _insert_legacy(
            conn,
            row_id=twin_id,
            name="shared-name",
            definition={"name": "shared-name", "steps": [{"name": "b"}]},
            deleted=True,
        )
        _apply_copy(conn)
        _apply_copy(conn)
        rows = conn.execute(
            "SELECT id, definition_json FROM agent_definitions WHERE name = %s",
            ("shared-name",),
        ).fetchall()
        assert {str(row["id"]) for row in rows} == {first_id, twin_id}
        children = {
            str(row["agent_definition_id"]): row["steps_json"]
            for row in conn.execute(
                "SELECT agent_definition_id, steps_json FROM agent_step_workflows"
            ).fetchall()
        }
        assert children[first_id] == [{"name": "a"}]
        assert children[twin_id] == [{"name": "b"}]


def test_divergent_payload_fails_loudly(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    row_id = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        _insert_legacy(
            conn,
            row_id=row_id,
            name="coder",
            definition={"name": "coder", "role": "legacy", "steps": [{"name": "a"}]},
        )
        conn.execute(
            """
            INSERT INTO agent_definitions (
                id, name, description, enabled, enabled_pinned, definition_json, source
            ) VALUES (%s, %s, %s, true, false, %s, 'installed')
            """,
            (row_id, "coder", "coder", Jsonb({"name": "coder", "role": "typed-drift"})),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="coder"):
            _apply_copy(conn)


def test_different_uuid_same_payload_fails_loudly(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    source_id = str(uuid.uuid4())
    typed_id = str(uuid.uuid4())
    body = {"name": "coder", "role": "same"}
    with _connect(url, schema) as conn:
        _insert_legacy(conn, row_id=source_id, name="coder", definition=body)
        conn.execute(
            """
            INSERT INTO agent_definitions (
                id, name, description, enabled, enabled_pinned, definition_json, source
            ) VALUES (%s, %s, %s, true, false, %s, 'installed')
            """,
            (typed_id, "coder", "coder", Jsonb(body)),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="coder"):
            _apply_copy(conn)


def test_copy_lock_fences_concurrent_writes(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    row_id = str(uuid.uuid4())
    holder = _connect(url, schema, autocommit=False)
    waiter = _connect(url, schema, autocommit=True)
    try:
        _insert_legacy(
            holder,
            row_id=row_id,
            name="locked-agent",
            definition={"name": "locked-agent", "steps": [{"name": "hold"}]},
        )
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
            "SELECT description FROM agent_definitions WHERE id = %s",
            (row_id,),
        ).fetchone()
        drifted = waiter.execute(
            "SELECT description FROM workflow_definitions WHERE id = %s",
            (row_id,),
        ).fetchone()
        assert copied is not None and copied["description"] == "locked-agent"
        assert drifted is not None and drifted["description"] == "drift"
    finally:
        holder.close()
        waiter.close()


def test_absent_legacy_table_is_receipted_noop(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    with _connect(url, schema) as conn:
        conn.execute("DROP TABLE workflow_definitions")
        _apply_copy(conn)
        remaining = conn.execute("SELECT count(*) AS n FROM agent_definitions").fetchone()
        assert remaining is not None and int(remaining["n"]) == 0
