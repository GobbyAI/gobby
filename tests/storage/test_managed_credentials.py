"""Integration coverage for daemon-managed PostgreSQL credentials."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

import gobby.storage.managed_credentials as managed_credentials_module
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.protocol import Row
from gobby.storage.managed_credentials import (
    CredentialAuthorizationError,
    CredentialIssuanceError,
    ManagedCredentialManager,
    ManagedToolCredential,
)
from tests.storage.test_postgres_agent_authorization import (
    AUTH_SCHEMA,
    RUNTIME_ROLE,
    AuthorizationFixture,
)

pytestmark = pytest.mark.integration
pytest_plugins = ("tests.storage.test_postgres_agent_authorization",)


def _manager(fixture: AuthorizationFixture, runtime_root: Path) -> ManagedCredentialManager:
    database = PostgresHubDatabase(fixture.database_url, runtime_role=RUNTIME_ROLE)
    database.open()
    return ManagedCredentialManager(
        database=database,
        machine_id=fixture.machine_id,
        runtime_root=runtime_root,
        owns_database=True,
    )


def test_issue_materializes_private_bootstrap_and_revoke_terminates_sessions(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    execution_id = uuid4()
    manager = _manager(fixture, tmp_path / "managed")
    try:
        credential = manager.issue(
            managed_execution_id=execution_id,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )

        assert re.fullmatch(rf"gobby_agent_{execution_id.hex}_1", credential.role_name)
        assert stat.S_IMODE(credential.bootstrap_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(credential.bootstrap_path.stat().st_mode) == 0o600
        assert "password" not in repr(credential).lower()

        bootstrap = json.loads(credential.bootstrap_path.read_text())
        scoped_dsn = cast(str, bootstrap["database_url"])
        parsed = conninfo_to_dict(scoped_dsn)
        assert parsed["user"] == credential.role_name
        assert parsed["password"]
        assert parsed["application_name"] == f"gobby-agent-{execution_id}"

        connection = psycopg.connect(scoped_dsn, autocommit=True)
        connection.execute("SELECT 1")
        outcome = manager.revoke(execution_id, reason="test-terminal")

        assert outcome.completed is True
        assert outcome.revoked_count == 1
        with pytest.raises(psycopg.OperationalError):
            connection.execute("SELECT 1")
        connection.close()
        assert not credential.bootstrap_path.exists()

        repeated = manager.revoke(execution_id, reason="test-repeat")
        assert repeated.completed is True
        assert repeated.revoked_count == 0

        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            sessions = admin.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE usename = %s",
                (credential.role_name,),
            ).fetchone()
            role = admin.execute("SELECT to_regrole(%s)", (credential.role_name,)).fetchone()
            function_definition = admin.execute(
                "SELECT pg_get_functiondef("
                "'gobby_agent_auth.revoke_principal(uuid, integer)'::regprocedure)"
            ).fetchone()
        assert sessions == (0,)
        assert role == (None,)
        assert function_definition is not None
        definition = function_definition[0]
        assert "pg_terminate_backend(pid, 5000)" in definition
        assert definition.index("SELECT count(*) INTO remaining_sessions") < definition.index(
            "DROP ROLE"
        )
        assert "'revoke_retry'" in definition
    finally:
        manager.close()


def test_bootstrap_failure_rolls_back_the_partially_created_role(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = authorization_fixture
    execution_id = uuid4()
    manager = _manager(fixture, tmp_path / "managed")

    def fail_materialization(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("synthetic bootstrap failure")

    monkeypatch.setattr(manager, "_materialize_bootstrap", fail_materialization)
    try:
        with pytest.raises(CredentialIssuanceError, match="managed credential issuance failed"):
            manager.issue(
                managed_execution_id=execution_id,
                owner_kind="agent_run",
                session_id=fixture.session_id,
                agent_run_id=fixture.agent_run_id,
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )

        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            binding = admin.execute(
                f"SELECT role_name, revoked_at FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s",
                (execution_id,),
            ).fetchone()
            assert binding is not None
            role = admin.execute("SELECT to_regrole(%s)", (binding[0],)).fetchone()
        assert binding[1] is not None
        assert role == (None,)
        assert not (tmp_path / "managed" / str(execution_id) / "bootstrap.json").exists()
    finally:
        manager.close()


class _UnavailableDatabase:
    conninfo = "postgresql://redacted.invalid/example"

    def fetchone(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Row | None:
        del sql, params
        raise psycopg.OperationalError("synthetic hub outage")

    def fetchall(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> list[Row]:
        del sql, params
        raise psycopg.OperationalError("synthetic hub outage")


class _TerminationTimeoutDatabase:
    conninfo = "postgresql://redacted.invalid/example"

    def fetchone(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Row | None:
        del sql, params
        return {"revoke_principal": -1}

    def fetchall(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> list[Row]:
        del sql, params
        return []


def test_hub_outage_removes_bootstrap_and_writes_secret_free_retry_record(
    tmp_path: Path,
) -> None:
    execution_id = uuid4()
    runtime_root = tmp_path / "managed"
    execution_root = runtime_root / str(execution_id)
    execution_root.mkdir(parents=True)
    bootstrap = execution_root / "bootstrap.json"
    bootstrap.write_text('{"database_url":"postgresql://role:secret@hub/db"}')
    os.chmod(bootstrap, 0o600)
    manager = ManagedCredentialManager(
        database=_UnavailableDatabase(),
        machine_id=uuid4(),
        runtime_root=runtime_root,
    )

    outcome = manager.revoke(execution_id, reason="hub-outage")

    assert outcome.completed is False
    assert outcome.retry_recorded is True
    assert not bootstrap.exists()
    retry_path = execution_root / "revocation-retry.json"
    retry_text = retry_path.read_text()
    assert stat.S_IMODE(retry_path.stat().st_mode) == 0o600
    assert str(execution_id) in retry_text
    assert "secret" not in retry_text
    assert "postgresql" not in retry_text


def test_termination_timeout_writes_durable_secret_free_retry_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    execution_id = uuid4()
    runtime_root = tmp_path / "managed"
    manager = ManagedCredentialManager(
        database=_TerminationTimeoutDatabase(),
        machine_id=uuid4(),
        runtime_root=runtime_root,
    )
    monkeypatch.setattr(
        managed_credentials_module,
        "REVOCATION_DRAIN_TIMEOUT_SECONDS",
        0.0,
    )

    outcome = manager.revoke(execution_id, reason="test-timeout")

    retry_path = runtime_root / str(execution_id) / "revocation-retry.json"
    assert outcome.completed is False
    assert outcome.retry_recorded is True
    assert outcome.failure_code == "active_sessions_remaining"
    assert stat.S_IMODE(retry_path.stat().st_mode) == 0o600
    retry_text = retry_path.read_text()
    assert json.loads(retry_text)["failure_code"] == "active_sessions_remaining"
    assert "password" not in retry_text.lower()
    assert "postgresql" not in retry_text.lower()


def test_tool_request_uses_authoritative_session_project_and_rejects_path_spoof(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path / "managed")
    authoritative_path = str(Path(f"/tmp/{fixture.project_id}").resolve())
    lease: ManagedToolCredential | None = None
    try:
        lease = manager.issue_tool_request(
            session_id=fixture.session_id,
            requested_project_path=authoritative_path,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        assert lease.project_id == fixture.project_id
        assert lease.project_path == authoritative_path
        assert lease.credential.bootstrap_path.exists()

        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            binding = admin.execute(
                f"SELECT owner_kind, session_id, project_id, agent_run_id "
                f"FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s",
                (lease.credential.managed_execution_id,),
            ).fetchone()
        assert binding == ("tool_chat", fixture.session_id, fixture.project_id, None)

        with pytest.raises(CredentialAuthorizationError, match="project path mismatch"):
            manager.issue_tool_request(
                session_id=fixture.session_id,
                requested_project_path=str(tmp_path / "spoofed-project"),
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )

        execution_id = lease.credential.managed_execution_id
        role_name = lease.credential.role_name
        outcome = manager.revoke(execution_id, reason="tool-request-finally")
        lease = None
        assert outcome.completed is True

        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            role = admin.execute("SELECT to_regrole(%s)", (role_name,)).fetchone()
            revoked = admin.execute(
                f"SELECT revoked_at IS NOT NULL FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s",
                (execution_id,),
            ).fetchone()
        assert role == (None,)
        assert revoked == (True,)
    finally:
        if lease is not None:
            manager.revoke(
                lease.credential.managed_execution_id,
                reason="test-cleanup",
            )
        manager.close()


def test_rotation_race_creates_one_successor_and_drains_the_predecessor(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    execution_id = uuid4()
    runtime_root = tmp_path / "managed"
    manager = _manager(fixture, runtime_root)
    try:
        manager.issue(
            managed_execution_id=execution_id,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(
                f"UPDATE {AUTH_SCHEMA}.principal_bindings "
                "SET issued_at = NOW() - INTERVAL '46 minutes', "
                "expires_at = NOW() + INTERVAL '10 minutes' "
                "WHERE managed_execution_id = %s",
                (execution_id,),
            )

        def rotate() -> int:
            contender = _manager(fixture, runtime_root)
            try:
                return len(contender.rotate_due())
            finally:
                contender.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: rotate(), range(2)))

        assert sorted(results) == [0, 1]
        bootstrap = json.loads((runtime_root / str(execution_id) / "bootstrap.json").read_text())
        assert bootstrap["credential_generation"] == 2
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            bindings = admin.execute(
                f"SELECT credential_generation, revoked_at IS NOT NULL, "
                "predecessor_drain_deadline - revocation_requested_at <= INTERVAL '5 minutes' "
                f"FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s ORDER BY credential_generation",
                (execution_id,),
            ).fetchall()
        assert bindings == [(1, True, True), (2, False, None)]
    finally:
        manager.revoke(execution_id, reason="test-cleanup")
        manager.close()


def test_restart_reconcile_revokes_expired_role_with_a_live_connection(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    execution_id = uuid4()
    runtime_root = tmp_path / "managed"
    first_daemon = _manager(fixture, runtime_root)
    credential = first_daemon.issue(
        managed_execution_id=execution_id,
        owner_kind="agent_run",
        session_id=fixture.session_id,
        agent_run_id=fixture.agent_run_id,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    bootstrap = json.loads(credential.bootstrap_path.read_text())
    connection = psycopg.connect(cast(str, bootstrap["database_url"]), autocommit=True)
    connection.execute("SELECT 1")
    first_daemon.close()

    with psycopg.connect(fixture.database_url, autocommit=True) as admin:
        admin.execute(
            f"UPDATE {AUTH_SCHEMA}.principal_bindings "
            "SET issued_at = NOW() - INTERVAL '59 minutes', "
            "expires_at = NOW() - INTERVAL '1 second' "
            "WHERE managed_execution_id = %s",
            (execution_id,),
        )

    restarted_daemon = _manager(fixture, runtime_root)
    try:
        reconciled = restarted_daemon.reconcile()
        assert reconciled >= 1
        with pytest.raises(psycopg.OperationalError):
            connection.execute("SELECT 1")
        connection.close()
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            sessions = admin.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE usename = %s",
                (credential.role_name,),
            ).fetchone()
            role = admin.execute("SELECT to_regrole(%s)", (credential.role_name,)).fetchone()
        assert sessions == (0,)
        assert role == (None,)
    finally:
        restarted_daemon.close()


def test_other_daemon_waits_for_expired_lease_then_recovers_terminal_and_orphan_roles(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    execution_id = uuid4()
    other_machine_id = uuid4()
    owner = _manager(fixture, tmp_path / "owner")
    with psycopg.connect(fixture.database_url, autocommit=True) as admin:
        admin.execute(
            "INSERT INTO public.machines (id, hostname) VALUES (%s, 'other-daemon-test')",
            (other_machine_id,),
        )
    other_database = PostgresHubDatabase(fixture.database_url, runtime_role=RUNTIME_ROLE)
    other_database.open()
    other = ManagedCredentialManager(
        database=other_database,
        machine_id=other_machine_id,
        runtime_root=tmp_path / "other",
        owns_database=True,
    )
    orphan_role = f"gobby_agent_{uuid4().hex}_1"
    try:
        credential = owner.issue(
            managed_execution_id=execution_id,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(orphan_role)))
            admin.execute(
                "UPDATE public.agent_runs SET status = 'error' WHERE id = %s",
                (fixture.agent_run_id,),
            )

        other.reconcile()
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            assert admin.execute("SELECT to_regrole(%s)", (credential.role_name,)).fetchone() != (
                None,
            )
            admin.execute(
                f"UPDATE {AUTH_SCHEMA}.daemon_registry "
                "SET heartbeat_at = NOW() - INTERVAL '2 minutes', "
                "lease_expires_at = NOW() - INTERVAL '1 second' "
                "WHERE machine_id = %s",
                (fixture.machine_id,),
            )

        other.reconcile()
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            assert admin.execute("SELECT to_regrole(%s)", (credential.role_name,)).fetchone() == (
                None,
            )
            assert admin.execute("SELECT to_regrole(%s)", (orphan_role,)).fetchone() == (None,)
            admin.execute(
                "UPDATE public.agent_runs SET status = 'pending' WHERE id = %s",
                (fixture.agent_run_id,),
            )
    finally:
        owner.revoke(execution_id, reason="test-cleanup")
        owner.close()
        other.close()
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute("DELETE FROM public.machines WHERE id = %s", (other_machine_id,))
