"""Migration coverage for retiring session summary revision history."""

from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from tests.fixtures.postgres import isolated_test_schema

pytestmark = pytest.mark.integration

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "crates/gcore/assets/schema/migrations/427_remove_session_summary_revisions.sql"
).read_text(encoding="utf-8")

_SESSION_ID = "11111111-1111-4111-8111-111111111111"
_REVISION_ID = "22222222-2222-4222-8222-222222222222"
_HANDOFF_ID = "33333333-3333-4333-8333-333333333333"
_DELIVERY_ID = "44444444-4444-4444-8444-444444444444"


def _create_pre_427_schema(connection: psycopg.Connection[tuple[object, ...]]) -> None:
    connection.execute(
        """
        CREATE TABLE sessions (
            id uuid PRIMARY KEY,
            summary_path text,
            summary_markdown text,
            summary_source_context_hash text,
            summary_generation_mode text,
            summary_generated_at timestamptz,
            handoff_markdown text,
            summary_revision_id uuid
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE session_summary_revisions (
            id uuid PRIMARY KEY,
            session_id uuid NOT NULL REFERENCES sessions(id),
            summary_markdown text NOT NULL,
            source_context_hash text,
            generation_mode text,
            generated_at timestamptz NOT NULL,
            UNIQUE (id, session_id)
        )
        """
    )
    connection.execute(
        """
        ALTER TABLE sessions
        ADD CONSTRAINT sessions_summary_revision_fk
        FOREIGN KEY (summary_revision_id, id)
        REFERENCES session_summary_revisions(id, session_id)
        DEFERRABLE INITIALLY DEFERRED
        """
    )
    connection.execute(
        """
        CREATE TABLE session_handoffs (
            id uuid PRIMARY KEY,
            session_id uuid NOT NULL REFERENCES sessions(id),
            handoff_markdown text NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE session_handoff_deliveries (
            id uuid PRIMARY KEY,
            handoff_id uuid NOT NULL REFERENCES session_handoffs(id),
            target_session_id uuid NOT NULL REFERENCES sessions(id)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO sessions (
            id,
            summary_path,
            summary_markdown,
            summary_source_context_hash,
            summary_generation_mode,
            summary_generated_at,
            handoff_markdown
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            _SESSION_ID,
            "/tmp/session-summary.md",
            "# Current summary",
            "context-hash",
            "agent_authored",
            "2026-09-05T12:00:00+00:00",
            "# Current handoff",
        ),
    )
    connection.execute(
        """
        INSERT INTO session_summary_revisions (
            id,
            session_id,
            summary_markdown,
            source_context_hash,
            generation_mode,
            generated_at
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            _REVISION_ID,
            _SESSION_ID,
            "# Current summary",
            "context-hash",
            "agent_authored",
            "2026-09-05T12:00:00+00:00",
        ),
    )
    connection.execute(
        "UPDATE sessions SET summary_revision_id = %s WHERE id = %s",
        (_REVISION_ID, _SESSION_ID),
    )
    connection.execute(
        "INSERT INTO session_handoffs VALUES (%s, %s, %s)",
        (_HANDOFF_ID, _SESSION_ID, "# Current handoff"),
    )
    connection.execute(
        "INSERT INTO session_handoff_deliveries VALUES (%s, %s, %s)",
        (_DELIVERY_ID, _HANDOFF_ID, _SESSION_ID),
    )


def test_retirement_preserves_current_state_and_repeats(postgres_database_url: str) -> None:
    with (
        isolated_test_schema(postgres_database_url, "summaryrevisionretire") as schema,
        psycopg.connect(postgres_database_url, autocommit=True) as connection,
    ):
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        _create_pre_427_schema(connection)

        for _ in range(2):
            connection.execute(_MIGRATION)

        summary = connection.execute(
            """
            SELECT
                summary_path,
                summary_markdown,
                summary_source_context_hash,
                summary_generation_mode,
                summary_generated_at::text,
                handoff_markdown
            FROM sessions
            WHERE id = %s
            """,
            (_SESSION_ID,),
        ).fetchone()
        assert summary == (
            "/tmp/session-summary.md",
            "# Current summary",
            "context-hash",
            "agent_authored",
            "2026-09-05 12:00:00+00",
            "# Current handoff",
        )
        retired_table = connection.execute(
            "SELECT to_regclass(%s)", (f"{schema}.session_summary_revisions",)
        ).fetchone()
        assert retired_table == (None,)
        revision_column = connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = 'sessions'
              AND column_name = 'summary_revision_id'
            """,
            (schema,),
        ).fetchall()
        assert revision_column == []
        handoffs = connection.execute(
            "SELECT id::text, session_id::text, handoff_markdown FROM session_handoffs"
        ).fetchall()
        assert handoffs == [(_HANDOFF_ID, _SESSION_ID, "# Current handoff")]
        deliveries = connection.execute(
            """
            SELECT id::text, handoff_id::text, target_session_id::text
            FROM session_handoff_deliveries
            """
        ).fetchall()
        assert deliveries == [(_DELIVERY_ID, _HANDOFF_ID, _SESSION_ID)]


def test_retirement_refuses_unexpected_dependencies_atomically(
    postgres_database_url: str,
) -> None:
    with (
        isolated_test_schema(postgres_database_url, "summaryrevisiondependency") as schema,
        psycopg.connect(postgres_database_url, autocommit=True) as connection,
    ):
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        _create_pre_427_schema(connection)
        connection.execute(
            "CREATE VIEW retained_revision_view AS SELECT id FROM session_summary_revisions"
        )

        with pytest.raises(psycopg.errors.DependentObjectsStillExist):
            connection.execute(_MIGRATION)

        revision = connection.execute("SELECT id::text FROM session_summary_revisions").fetchone()
        assert revision == (_REVISION_ID,)
        pointer = connection.execute(
            "SELECT summary_revision_id::text FROM sessions WHERE id = %s", (_SESSION_ID,)
        ).fetchone()
        assert pointer == (_REVISION_ID,)
        constraint = connection.execute(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'sessions'::regclass
              AND conname = 'sessions_summary_revision_fk'
            """
        ).fetchone()
        assert constraint == ("sessions_summary_revision_fk",)
        retained_view = connection.execute("SELECT id::text FROM retained_revision_view").fetchone()
        assert retained_view == (_REVISION_ID,)
