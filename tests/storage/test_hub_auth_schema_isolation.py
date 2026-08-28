"""Each non-public hub owns its agent-auth schema (#21148).

The auth functions are SECURITY DEFINER bodies naming hub tables explicitly.
Rendering them into one shared ``gobby_agent_auth`` let every per-schema apply
re-point ``heartbeat_daemon`` at the newest hub, so a daemon on an older test
schema failed its heartbeat once a sibling schema (or the migration test)
applied the baseline.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import psycopg
import pytest

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


def test_sibling_schema_apply_leaves_this_hubs_auth_functions_alone(
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

    manager = ManagedCredentialManager(
        database=postgres_db,
        machine_id=UUID(require_machine_id()),
        runtime_root=tmp_path,
    )
    manager.heartbeat()

    with isolated_test_schema(postgres_database_url, "sibling") as sibling:
        apply_schema(postgres_database_url, schema=sibling)
        sibling_auth = auth_schema_for(sibling)
        assert _schema_exists(postgres_database_url, sibling_auth)
        assert f"{sibling}.machines" in _heartbeat_body(postgres_database_url, sibling_auth)
        # The sibling apply must not have touched this hub's functions.
        assert _heartbeat_body(postgres_database_url, auth_schema) == body_before
        manager.heartbeat()

    assert not _schema_exists(postgres_database_url, sibling_auth)
    assert not _schema_exists(postgres_database_url, sibling)
