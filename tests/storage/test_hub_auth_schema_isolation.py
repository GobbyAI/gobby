"""Each non-public hub owns its agent-auth schema (#21148).

The auth functions are SECURITY DEFINER bodies naming hub tables explicitly.
Rendering them into one shared ``gobby_agent_auth`` let every per-schema apply
re-point ``heartbeat_daemon`` at the newest hub, so a daemon on an older test
schema failed its heartbeat once a sibling schema (or the migration test)
applied the baseline — and dropping that sibling then left the shared
functions pointing at tables that no longer existed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql

from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.managed_credential_types import auth_schema_for, resolve_auth_schema
from gobby.storage.managed_credentials import ManagedCredentialManager
from gobby.storage.schema_contract import apply_schema
from gobby.utils.machine_id import require_machine_id
from tests.fixtures.postgres import isolated_test_schema

pytestmark = pytest.mark.integration


def _heartbeat_body(url: str, auth_schema: str) -> str:
    with psycopg.connect(url) as conn:
        row = conn.execute(
            """
            SELECT p.prosrc
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = %s AND p.proname = 'heartbeat_daemon'
            """,
            (auth_schema,),
        ).fetchone()
    assert row is not None, f"{auth_schema}.heartbeat_daemon is missing"
    return str(row[0])


def _schema_exists(url: str, name: str) -> bool:
    with psycopg.connect(url) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                (name,),
            ).fetchone()
            is not None
        )


def _drop_schemas(url: str, *names: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        for name in names:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


def _seed_agent_run(db: PostgresHubDatabase, machine_id: UUID) -> tuple[UUID, UUID]:
    """Insert the project/session/run rows an agent-principal issue requires."""
    project_id, session_id, run_id = uuid4(), uuid4(), uuid4()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            (project_id, f"auth-isolation-{project_id}"),
        )
        conn.execute(
            """
            INSERT INTO sessions (id, external_id, machine_id, source, project_id)
            VALUES (%s, %s, %s, 'codex', %s)
            """,
            (session_id, f"auth-isolation-{session_id}", machine_id, project_id),
        )
        conn.execute(
            """
            INSERT INTO agent_runs (id, parent_session_id, machine_id, provider, prompt)
            VALUES (%s, %s, %s, 'codex', 'auth isolation')
            """,
            (run_id, session_id, machine_id),
        )
        conn.execute("UPDATE sessions SET agent_run_id = %s WHERE id = %s", (run_id, session_id))
    return session_id, run_id


def _issue(manager: ManagedCredentialManager, session_id: UUID, run_id: UUID) -> UUID:
    execution_id = uuid4()
    credential = manager.issue(
        managed_execution_id=execution_id,
        owner_kind="agent_run",
        session_id=session_id,
        agent_run_id=run_id,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert credential.role_name.startswith("gobby_agent_")
    return execution_id


def test_sibling_schema_apply_and_drop_leave_this_hubs_auth_functions_alone(
    postgres_database_url: str,
    postgres_schema: str,
    postgres_db: PostgresHubDatabase,
    tmp_path: Path,
) -> None:
    auth_schema = auth_schema_for(postgres_schema)
    assert auth_schema == f"{postgres_schema}_agent_auth"
    assert resolve_auth_schema(postgres_db) == auth_schema

    body_before = _heartbeat_body(postgres_database_url, auth_schema)
    assert f"{postgres_schema}.machines" in body_before
    assert "public.machines" not in body_before

    machine_id = UUID(require_machine_id())
    manager = ManagedCredentialManager(
        database=postgres_db, machine_id=machine_id, runtime_root=tmp_path
    )
    manager.heartbeat()
    session_id, run_id = _seed_agent_run(postgres_db, machine_id)
    issued: list[UUID] = []
    try:
        issued.append(_issue(manager, session_id, run_id))

        with isolated_test_schema(postgres_database_url, "sibling") as sibling:
            apply_schema(postgres_database_url, schema=sibling)
            sibling_auth = auth_schema_for(sibling)
            assert _schema_exists(postgres_database_url, sibling_auth)
            assert f"{sibling}.machines" in _heartbeat_body(postgres_database_url, sibling_auth)
            # The sibling apply must not have touched this hub's functions.
            assert _heartbeat_body(postgres_database_url, auth_schema) == body_before
            manager.heartbeat()
            issued.append(_issue(manager, session_id, run_id))

            # A sibling that disappears while this hub is live (the migration
            # test dropping its schemas mid-run) must leave every auth entry
            # point of the surviving hub callable.
            _drop_schemas(postgres_database_url, sibling_auth, sibling)
            assert not _schema_exists(postgres_database_url, sibling_auth)
            manager.heartbeat()
            assert manager.reconcile() >= 0
            issued.append(_issue(manager, session_id, run_id))
            assert _heartbeat_body(postgres_database_url, auth_schema) == body_before

        assert not _schema_exists(postgres_database_url, sibling)
        assert len(manager.list_active()) == 3
    finally:
        for execution_id in issued:
            manager.revoke(execution_id, reason="auth-isolation-test")
