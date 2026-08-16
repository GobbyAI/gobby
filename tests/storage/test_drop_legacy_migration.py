"""Directional backstop and drop of legacy workflow definition tables."""

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

_REPO = Path(__file__).resolve().parents[2]
_MIGRATION = _REPO / "crates/gcore/assets/schema/migrations/381_drop_legacy_workflow_tables.sql"

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

CREATE TABLE workflow_instances (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL,
    workflow_name text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    priority integer DEFAULT 100 NOT NULL,
    current_step text,
    step_entered_at timestamptz,
    step_action_count integer DEFAULT 0,
    total_action_count integer DEFAULT 0,
    variables jsonb DEFAULT '{}'::jsonb,
    context_injected boolean DEFAULT false,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);

CREATE TABLE legacy_copy_ledger (
    legacy_id uuid PRIMARY KEY,
    domain text NOT NULL,
    source_hash text NOT NULL,
    copied_at timestamptz DEFAULT now() NOT NULL
);

CREATE TABLE agent_definitions (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    definition_json jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE rule_definitions (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    definition_json jsonb NOT NULL DEFAULT '{}'::jsonb
);
"""


@pytest.fixture
def drop_schema(postgres_database_url: str) -> Iterator[tuple[str, str]]:
    schema = f"gobby_test_drop_{uuid.uuid4().hex[:12]}"
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


def _apply_drop(conn: psycopg.Connection[Any]) -> None:
    conn.execute(_MIGRATION.read_text(encoding="utf-8"))


def _insert_legacy(
    conn: psycopg.Connection[Any],
    *,
    row_id: str,
    name: str,
    workflow_type: str,
    definition: dict[str, Any],
    source: str = "installed",
    deleted: bool = False,
    description: str | None = None,
    version: str = "1.0",
    priority: int = 100,
    sources: list[str] | None = None,
    canvas: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO workflow_definitions (
            id, name, description, workflow_type, version, enabled,
            enabled_user_modified, priority, sources, definition_json,
            canvas_json, source, tags, deleted_at
        ) VALUES (
            %s, %s, %s, %s, %s, true, false, %s, %s, %s, %s, %s, %s,
            CASE WHEN %s THEN now() ELSE NULL END
        )
        """,
        (
            row_id,
            name,
            description or name,
            workflow_type,
            version,
            priority,
            Jsonb(sources) if sources is not None else None,
            Jsonb(definition),
            Jsonb(canvas) if canvas is not None else None,
            source,
            Jsonb(tags or ["gobby"]),
            deleted,
        ),
    )


def _ledger_hash(conn: psycopg.Connection[Any], row_id: str) -> str:
    row = conn.execute(
        """
        SELECT md5((
            CASE workflow_type
                WHEN 'agent' THEN jsonb_build_object(
                    'id', id,
                    'project_id', project_id,
                    'name', name,
                    'description', description,
                    'enabled', COALESCE(enabled, true),
                    'enabled_user_modified', enabled_user_modified,
                    'definition_json', definition_json,
                    'source', CASE
                        WHEN source IN ('installed', 'custom', 'project') THEN source
                        ELSE 'installed'
                    END,
                    'tags', tags,
                    'deleted_at', deleted_at
                )
                WHEN 'rule' THEN jsonb_build_object(
                    'id', id,
                    'project_id', project_id,
                    'name', name,
                    'description', description,
                    'enabled', COALESCE(enabled, true),
                    'enabled_user_modified', enabled_user_modified,
                    'priority', COALESCE(priority, 100),
                    'sources', sources,
                    'definition_json', definition_json,
                    'source', CASE
                        WHEN source IN ('installed', 'custom', 'project') THEN source
                        ELSE 'installed'
                    END,
                    'tags', tags,
                    'deleted_at', deleted_at
                )
                WHEN 'variable' THEN jsonb_build_object(
                    'id', id,
                    'project_id', project_id,
                    'name', COALESCE(definition_json->>'variable', name),
                    'description', description,
                    'enabled', COALESCE(enabled, true),
                    'enabled_user_modified', enabled_user_modified,
                    'default_value', definition_json->'value',
                    'source', CASE
                        WHEN source IN ('installed', 'custom', 'project') THEN source
                        ELSE 'installed'
                    END,
                    'tags', tags,
                    'deleted_at', deleted_at
                )
                WHEN 'pipeline' THEN jsonb_build_object(
                    'id', id,
                    'project_id', project_id,
                    'name', name,
                    'description', description,
                    'enabled', COALESCE(enabled, true),
                    'enabled_user_modified', enabled_user_modified,
                    'version', COALESCE(version, '1.0'),
                    'definition_json', definition_json,
                    'canvas_json', canvas_json,
                    'source', CASE
                        WHEN source IN ('installed', 'custom', 'project') THEN source
                        ELSE 'installed'
                    END,
                    'tags', tags,
                    'deleted_at', deleted_at
                )
                ELSE jsonb_build_object('id', id)
            END
        )::text) AS source_hash
        FROM workflow_definitions
        WHERE id = %s
        """,
        (row_id,),
    ).fetchone()
    assert row is not None
    return str(row["source_hash"])


def _checkpoint(
    conn: psycopg.Connection[Any], row_id: str, domain: str, source_hash: str | None = None
) -> None:
    hashed = source_hash if source_hash is not None else _ledger_hash(conn, row_id)
    conn.execute(
        """
        INSERT INTO legacy_copy_ledger (legacy_id, domain, source_hash)
        VALUES (%s, %s, %s)
        ON CONFLICT (legacy_id) DO UPDATE SET source_hash = EXCLUDED.source_hash
        """,
        (row_id, domain, hashed),
    )


def _seed_supported(conn: psycopg.Connection[Any]) -> dict[str, str]:
    ids = {
        "agent": str(uuid.uuid4()),
        "rule": str(uuid.uuid4()),
        "variable": str(uuid.uuid4()),
        "pipeline": str(uuid.uuid4()),
        "generated": str(uuid.uuid4()),
        "deleted": str(uuid.uuid4()),
    }
    _insert_legacy(
        conn,
        row_id=ids["agent"],
        name="ship-agent",
        workflow_type="agent",
        definition={"provider": "claude"},
    )
    _insert_legacy(
        conn,
        row_id=ids["rule"],
        name="block-shell",
        workflow_type="rule",
        definition={"event": "before_tool"},
        sources=["claude"],
    )
    _insert_legacy(
        conn,
        row_id=ids["variable"],
        name="goal",
        workflow_type="variable",
        definition={"variable": "goal", "value": "ship"},
    )
    _insert_legacy(
        conn,
        row_id=ids["pipeline"],
        name="nightly",
        workflow_type="pipeline",
        definition={"steps": []},
        canvas={"x": 1},
    )
    _insert_legacy(
        conn,
        row_id=ids["generated"],
        name="ship-agent-steps",
        workflow_type="workflow",
        source="agent",
        definition={"type": "step", "steps": [{"name": "do"}]},
    )
    _insert_legacy(
        conn,
        row_id=ids["deleted"],
        name="retired-rule",
        workflow_type="rule",
        definition={"event": "session_end"},
        deleted=True,
    )
    for key, domain in (
        ("agent", "agents"),
        ("rule", "rules"),
        ("variable", "variables"),
        ("pipeline", "pipelines"),
        ("deleted", "rules"),
    ):
        _checkpoint(conn, ids[key], domain)
    return ids


def _tables_exist(conn: psycopg.Connection[Any]) -> dict[str, bool]:
    row = conn.execute(
        """
        SELECT
            to_regclass('workflow_definitions') IS NOT NULL AS defs,
            to_regclass('workflow_instances') IS NOT NULL AS inst,
            to_regclass('legacy_copy_ledger') IS NOT NULL AS ledger
        """
    ).fetchone()
    assert row is not None
    return {"defs": bool(row["defs"]), "inst": bool(row["inst"]), "ledger": bool(row["ledger"])}


def test_drop_succeeds_when_every_non_generated_row_matches_ledger(
    drop_schema: tuple[str, str],
) -> None:
    url, schema = drop_schema
    with _connect(url, schema) as conn:
        _seed_supported(conn)
        conn.execute(
            """
            CREATE FUNCTION gobby_reject_workflow_instance_writes() RETURNS trigger
            LANGUAGE plpgsql AS $fn$ BEGIN RETURN NEW; END $fn$
            """
        )
        _apply_drop(conn)
        assert _tables_exist(conn) == {"defs": False, "inst": False, "ledger": False}
        leftover = conn.execute(
            """
            SELECT 1 FROM pg_proc
            WHERE proname = 'gobby_reject_workflow_instance_writes'
              AND pg_function_is_visible(oid)
            """
        ).fetchone()
        assert leftover is None


def test_backstop_refuses_hash_mismatch_and_post_copy_insert(
    drop_schema: tuple[str, str],
) -> None:
    url, schema = drop_schema
    with _connect(url, schema) as conn:
        ids = _seed_supported(conn)
        conn.execute(
            "UPDATE workflow_definitions SET description = 'tampered' WHERE id = %s",
            (ids["agent"],),
        )
        with pytest.raises(psycopg.errors.RaiseException, match=r"ship-agent") as mismatch:
            _apply_drop(conn)
        assert str(ids["agent"]) in str(mismatch.value)
        assert _tables_exist(conn)["defs"] is True

        conn.execute(
            "UPDATE workflow_definitions SET description = 'ship-agent' WHERE id = %s",
            (ids["agent"],),
        )
        _checkpoint(conn, ids["agent"], "agents")
        inserted = str(uuid.uuid4())
        _insert_legacy(
            conn,
            row_id=inserted,
            name="late-rule",
            workflow_type="rule",
            definition={"event": "turn_end"},
        )
        with pytest.raises(psycopg.errors.RaiseException, match=r"late-rule") as missing:
            _apply_drop(conn)
        assert inserted in str(missing.value)
        assert _tables_exist(conn)["defs"] is True


def test_backstop_covers_soft_deleted_rows(drop_schema: tuple[str, str]) -> None:
    url, schema = drop_schema
    with _connect(url, schema) as conn:
        _seed_supported(conn)
        late = str(uuid.uuid4())
        _insert_legacy(
            conn,
            row_id=late,
            name="ghost-rule",
            workflow_type="rule",
            definition={"event": "turn_start"},
            deleted=True,
        )
        with pytest.raises(psycopg.errors.RaiseException, match=r"ghost-rule") as exc:
            _apply_drop(conn)
        assert late in str(exc.value)
        assert _tables_exist(conn)["defs"] is True


def test_unsupported_row_classification(drop_schema: tuple[str, str]) -> None:
    url, schema = drop_schema
    with _connect(url, schema) as conn:
        ids = _seed_supported(conn)
        standalone = str(uuid.uuid4())
        _insert_legacy(
            conn,
            row_id=standalone,
            name="user-flow",
            workflow_type="workflow",
            source="custom",
            definition={"steps": [{"name": "a"}]},
        )
        with pytest.raises(psycopg.errors.RaiseException, match=r"user-flow") as unsupported:
            _apply_drop(conn)
        assert standalone in str(unsupported.value)

        conn.execute("DELETE FROM workflow_definitions WHERE id = %s", (standalone,))
        unknown = str(uuid.uuid4())
        _insert_legacy(
            conn,
            row_id=unknown,
            name="mystery",
            workflow_type="skill",
            definition={"ok": True},
        )
        with pytest.raises(psycopg.errors.RaiseException, match=r"mystery") as unknown_exc:
            _apply_drop(conn)
        assert unknown in str(unknown_exc.value)
        assert str(ids["generated"]) not in str(unknown_exc.value)
        assert _tables_exist(conn)["defs"] is True


def test_directional_backstop(drop_schema: tuple[str, str]) -> None:
    url, schema = drop_schema
    with _connect(url, schema) as conn:
        ids = _seed_supported(conn)
        conn.execute(
            """
            INSERT INTO agent_definitions (id, name, definition_json)
            VALUES (%s, 'ship-agent', '{"provider":"claude","edited":true}'::jsonb)
            """,
            (ids["agent"],),
        )
        conn.execute(
            "INSERT INTO rule_definitions (id, name) VALUES (%s, 'block-shell')",
            (ids["rule"],),
        )
        conn.execute(
            "UPDATE agent_definitions SET name = 'ship-agent-v2' WHERE id = %s",
            (ids["agent"],),
        )
        conn.execute("DELETE FROM rule_definitions WHERE id = %s", (ids["rule"],))
        _apply_drop(conn)
        assert _tables_exist(conn) == {"defs": False, "inst": False, "ledger": False}

    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        admin.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        admin.execute(_SCHEMA_SQL)
    with _connect(url, schema) as conn:
        ids = _seed_supported(conn)
        conn.execute(
            "UPDATE workflow_definitions SET definition_json = %s WHERE id = %s",
            (Jsonb({"provider": "codex"}), ids["agent"]),
        )
        with pytest.raises(psycopg.errors.RaiseException, match=r"ship-agent"):
            _apply_drop(conn)
        conn.execute(
            "UPDATE workflow_definitions SET definition_json = %s WHERE id = %s",
            (Jsonb({"provider": "claude"}), ids["agent"]),
        )
        _checkpoint(conn, ids["agent"], "agents")
        inserted = str(uuid.uuid4())
        _insert_legacy(
            conn,
            row_id=inserted,
            name="extra-agent",
            workflow_type="agent",
            definition={"provider": "droid"},
        )
        with pytest.raises(psycopg.errors.RaiseException, match=r"extra-agent"):
            _apply_drop(conn)


def test_fresh_schema_without_legacy_tables_is_a_noop(drop_schema: tuple[str, str]) -> None:
    url, schema = drop_schema
    with _connect(url, schema) as conn:
        conn.execute("DROP TABLE workflow_instances, workflow_definitions, legacy_copy_ledger")
        _apply_drop(conn)
        assert _tables_exist(conn) == {"defs": False, "inst": False, "ledger": False}
        remaining = conn.execute(
            "SELECT to_regclass('agent_definitions') IS NOT NULL AS ok"
        ).fetchone()
        assert remaining is not None
        assert remaining["ok"] is True
