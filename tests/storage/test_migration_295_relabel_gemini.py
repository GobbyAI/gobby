"""Behavioral tests for migration 295 (relabel_gemini_sessions).

These exercise the migration SQL against real persisted rows rather than only
asserting framework registration: a stale ``source='gemini'`` row in both the
``sessions`` and ``session_stop_signals`` tables must be relabeled to
``'unknown'``, the migration must be idempotent, and the storage read path must
return the relabeled row without raising.
"""

import importlib
import importlib.resources

import pytest

from gobby.hooks.events import SessionSource, parse_session_source
from gobby.storage.hub.postgres import PostgresHubDatabase

pytestmark = pytest.mark.integration

# Canonical project placeholder seeded into every fresh worker schema.
_CANONICAL_PROJECT_ID = "00000000-0000-0000-0000-000000000000"

_MIGRATION_FILENAME = "295_relabel_gemini_sessions.postgres.sql"


def _migration_sql() -> str:
    resource = (
        importlib.resources.files("gobby.storage")
        .joinpath("migrations")
        .joinpath(_MIGRATION_FILENAME)
    )
    return resource.read_text(encoding="utf-8")


def _run_migration_295(db: PostgresHubDatabase) -> None:
    """Execute migration 295's SQL statements in a single transaction."""
    module = importlib.import_module("gobby.storage.migrations")
    statements = [
        statement.strip()
        for statement in module._split_statements_respecting_dollar_quotes(_migration_sql())
        if statement.strip()
    ]
    assert statements, "migration 295 should contain at least one statement"
    with db.transaction() as txn:
        for statement in statements:
            txn.execute(statement)


def _seed_gemini_rows(db: PostgresHubDatabase, session_id: str) -> None:
    with db.transaction() as txn:
        txn.execute(
            """
            INSERT INTO sessions (id, external_id, machine_id, source, project_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (session_id, f"ext-{session_id}", "machine-1", "gemini", _CANONICAL_PROJECT_ID),
        )
        txn.execute(
            """
            INSERT INTO session_stop_signals (session_id, source, requested_at)
            VALUES (%s, %s, NOW())
            """,
            (session_id, "gemini"),
        )


def test_migration_295_relabels_gemini_rows_to_unknown(
    postgres_db: PostgresHubDatabase,
) -> None:
    """Stale gemini rows in both source tables become 'unknown'."""
    session_id = "sess-migration-295"
    _seed_gemini_rows(postgres_db, session_id)

    _run_migration_295(postgres_db)

    session_row = postgres_db.fetchone("SELECT source FROM sessions WHERE id = %s", (session_id,))
    signal_row = postgres_db.fetchone(
        "SELECT source FROM session_stop_signals WHERE session_id = %s", (session_id,)
    )

    assert session_row is not None
    assert signal_row is not None
    assert session_row["source"] == "unknown"
    assert signal_row["source"] == "unknown"


def test_migration_295_is_idempotent(postgres_db: PostgresHubDatabase) -> None:
    """Re-running the migration leaves already-relabeled rows untouched."""
    session_id = "sess-migration-295-idempotent"
    _seed_gemini_rows(postgres_db, session_id)

    _run_migration_295(postgres_db)
    _run_migration_295(postgres_db)

    session_row = postgres_db.fetchone("SELECT source FROM sessions WHERE id = %s", (session_id,))
    signal_row = postgres_db.fetchone(
        "SELECT source FROM session_stop_signals WHERE session_id = %s", (session_id,)
    )

    assert session_row is not None and session_row["source"] == "unknown"
    assert signal_row is not None and signal_row["source"] == "unknown"


def test_migration_295_preserves_non_gemini_sources(
    postgres_db: PostgresHubDatabase,
) -> None:
    """Only gemini rows are touched; other sources are left intact."""
    session_id = "sess-migration-295-claude"
    with postgres_db.transaction() as txn:
        txn.execute(
            """
            INSERT INTO sessions (id, external_id, machine_id, source, project_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (session_id, f"ext-{session_id}", "machine-1", "claude", _CANONICAL_PROJECT_ID),
        )

    _run_migration_295(postgres_db)

    session_row = postgres_db.fetchone("SELECT source FROM sessions WHERE id = %s", (session_id,))
    assert session_row is not None
    assert session_row["source"] == "claude"


def test_relabeled_session_read_path_resolves_to_unknown(
    postgres_db: PostgresHubDatabase,
) -> None:
    """The storage read path returns the relabeled row and it parses cleanly.

    This is the real crash surface: consumers parse the stored ``source`` via
    ``parse_session_source``. After relabeling, the value is ``'unknown'`` which
    must resolve to ``SessionSource.UNKNOWN`` without raising.
    """
    session_id = "sess-migration-295-readpath"
    _seed_gemini_rows(postgres_db, session_id)
    _run_migration_295(postgres_db)

    rows = postgres_db.fetchall("SELECT source FROM sessions WHERE id = %s", (session_id,))
    assert len(rows) == 1
    stored_source = rows[0]["source"]
    assert parse_session_source(stored_source) is SessionSource.UNKNOWN
