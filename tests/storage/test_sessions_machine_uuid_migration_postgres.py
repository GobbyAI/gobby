"""PostgreSQL integration coverage for sessions machine UUID migration 366."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from psycopg import sql

from tests.fixtures.postgres import isolated_test_schema

pytestmark = pytest.mark.integration

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "src/gobby/storage/migrations/366_sessions_machine_uuid_fk.sql"
)
PROJECT_ID = "10000000-0000-4000-8000-000000000001"
MACHINE_ID = "20000000-0000-4000-8000-000000000001"
SURVIVOR_ID = "30000000-0000-4000-8000-000000000001"
LOSER_ID = "30000000-0000-4000-8000-000000000002"
CHILD_SESSION_ID = "30000000-0000-4000-8000-000000000003"


def _create_pre_migration_schema(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    conn.execute(
        """
        CREATE TABLE machines (id UUID PRIMARY KEY);
        CREATE TABLE projects (id UUID PRIMARY KEY);
        CREATE TABLE sessions (
            id UUID PRIMARY KEY,
            external_id TEXT NOT NULL,
            machine_id TEXT,
            source TEXT NOT NULL,
            project_id UUID NOT NULL REFERENCES projects(id),
            session_type TEXT NOT NULL DEFAULT 'terminal',
            parent_session_id UUID REFERENCES sessions(id),
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        );
        CREATE UNIQUE INDEX idx_sessions_unique
            ON sessions(external_id, machine_id, source, project_id, session_type);
        CREATE TABLE session_variables (
            session_id UUID PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
            variables JSONB NOT NULL
        );
        CREATE TABLE session_events (
            id UUID PRIMARY KEY,
            session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            event_key TEXT NOT NULL,
            payload TEXT NOT NULL,
            CONSTRAINT session_events_session_key_unique UNIQUE (session_id, event_key)
        );
        """
    )


def test_migration_merges_duplicate_sessions_and_child_rows(
    postgres_database_url: str,
) -> None:
    notices: list[str] = []
    with isolated_test_schema(postgres_database_url, "machmig") as schema_name:
        with psycopg.connect(postgres_database_url, autocommit=True) as conn:
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
            conn.add_notice_handler(
                lambda diagnostic: notices.append(diagnostic.message_primary or "")
            )
            _create_pre_migration_schema(conn)
            conn.execute("INSERT INTO machines(id) VALUES (%s)", (MACHINE_ID,))
            conn.execute("INSERT INTO projects(id) VALUES (%s)", (PROJECT_ID,))
            conn.execute(
                """
                INSERT INTO sessions(
                    id, external_id, machine_id, source, project_id, session_type,
                    parent_session_id, created_at, updated_at
                ) VALUES
                    (%s, 'duplicate', 'pipeline', 'pipeline', %s, 'terminal', NULL,
                     '2026-01-01T00:00:00Z', '2026-01-03T00:00:00Z'),
                    (%s, 'duplicate', 'unknown', 'pipeline', %s, 'terminal', NULL,
                     '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z'),
                    (%s, 'child', %s, 'codex', %s, 'terminal', %s,
                     '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """,
                (
                    SURVIVOR_ID,
                    PROJECT_ID,
                    LOSER_ID,
                    PROJECT_ID,
                    CHILD_SESSION_ID,
                    MACHINE_ID,
                    PROJECT_ID,
                    LOSER_ID,
                ),
            )
            conn.execute(
                """
                INSERT INTO session_variables(session_id, variables) VALUES
                    (%s, '{"owner":"survivor"}'),
                    (%s, '{"owner":"loser"}')
                """,
                (SURVIVOR_ID, LOSER_ID),
            )
            conn.execute(
                """
                INSERT INTO session_events(id, session_id, event_key, payload) VALUES
                    ('40000000-0000-4000-8000-000000000001', %s, 'shared', 'survivor'),
                    ('40000000-0000-4000-8000-000000000002', %s, 'shared', 'loser'),
                    ('40000000-0000-4000-8000-000000000003', %s, 'move', 'loser')
                """,
                (SURVIVOR_ID, LOSER_ID, LOSER_ID),
            )

            conn.execute(MIGRATION_PATH.read_text(encoding="utf-8"))

            column_type = conn.execute(
                """
                SELECT data_type
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'sessions'
                  AND column_name = 'machine_id'
                """
            ).fetchone()
            assert column_type == ("uuid",)
            assert conn.execute(
                """
                SELECT 1
                FROM pg_constraint
                WHERE conrelid = 'sessions'::regclass
                  AND contype = 'f'
                  AND confrelid = 'machines'::regclass
                """
            ).fetchone() == (1,)
            assert conn.execute(
                """
                SELECT index_state.indnullsnotdistinct
                FROM pg_index AS index_state
                WHERE index_state.indexrelid = 'idx_sessions_unique'::regclass
                """
            ).fetchone() == (True,)

            duplicates = conn.execute(
                "SELECT id, machine_id FROM sessions WHERE external_id = 'duplicate'"
            ).fetchall()
            assert duplicates == [(UUID(SURVIVOR_ID), None)]
            assert conn.execute(
                "SELECT parent_session_id FROM sessions WHERE id = %s",
                (CHILD_SESSION_ID,),
            ).fetchone() == (UUID(SURVIVOR_ID),)
            assert conn.execute(
                "SELECT session_id, variables FROM session_variables"
            ).fetchall() == [(UUID(SURVIVOR_ID), {"owner": "survivor"})]
            assert conn.execute(
                "SELECT session_id, event_key, payload FROM session_events ORDER BY event_key"
            ).fetchall() == [
                (UUID(SURVIVOR_ID), "move", "loser"),
                (UUID(SURVIVOR_ID), "shared", "survivor"),
            ]
            assert conn.execute(
                "SELECT machine_id FROM sessions WHERE id = %s",
                (CHILD_SESSION_ID,),
            ).fetchone() == (UUID(MACHINE_ID),)

    inventory_notice = next(message for message in notices if "FK inventory" in message)
    assert "session_variables" in inventory_notice
    assert "session_events" in inventory_notice
    assert "session_events_session_key_unique" in inventory_notice
    collision_notice = next(
        message for message in notices if "collision preflight ledger" in message
    )
    assert "parent_groups=1" in collision_notice
    assert "parent_losers=1" in collision_notice
    assert "child_delete=2" in collision_notice
    assert "child_repoint=1" in collision_notice
    print(f"migration-inventory: {inventory_notice}")
    print(f"collision-ledger: {collision_notice}")


def test_migration_rejects_unclassified_non_uuid_machine_id(
    postgres_database_url: str,
) -> None:
    with isolated_test_schema(postgres_database_url, "machgate") as schema_name:
        with psycopg.connect(postgres_database_url, autocommit=True) as conn:
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
            _create_pre_migration_schema(conn)
            conn.execute("INSERT INTO projects(id) VALUES (%s)", (PROJECT_ID,))
            conn.execute(
                """
                INSERT INTO sessions(
                    id, external_id, machine_id, source, project_id, created_at, updated_at
                ) VALUES (%s, 'unmapped', 'surprise-machine', 'codex', %s, NOW(), NOW())
                """,
                (SURVIVOR_ID, PROJECT_ID),
            )

            with pytest.raises(psycopg.errors.RaiseException, match="zero-unmapped preflight"):
                conn.execute(MIGRATION_PATH.read_text(encoding="utf-8"))
