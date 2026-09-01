"""Integration coverage for daemon-managed PostgreSQL credentials."""

from __future__ import annotations

import json
import os
import re
import stat
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

import gobby.storage.managed_credentials as managed_credentials_module
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.protocol import Row, Transaction
from gobby.storage.managed_credentials import (
    CredentialAuthorizationError,
    CredentialIssuanceError,
    ManagedCredentialManager,
    ManagedToolCredential,
)
from gobby.storage.secrets import SecretStore
from tests._timing import wait_for_condition
from tests.fixtures.postgres import TEST_USER_ID
from tests.storage.test_postgres_agent_authorization import (
    AUTH_SCHEMA,
    RUNTIME_ROLE,
    AuthorizationFixture,
    _as_runtime,
)

pytestmark = pytest.mark.integration


def test_validate_expiry_covers_spawn_timeouts_and_bounds_runaways() -> None:
    """Spawn-derived lifetimes (timeout + 5min) must pass; runaways must not."""
    issued_at = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)

    two_hour_run = issued_at + timedelta(seconds=7500)
    assert ManagedCredentialManager._validate_expiry(issued_at, two_hour_run) == two_hour_run

    runaway = issued_at + managed_credentials_module.MAX_ROLE_LIFETIME + timedelta(seconds=1)
    with pytest.raises(ValueError, match="exceeds 24 hours"):
        ManagedCredentialManager._validate_expiry(issued_at, runaway)


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

        active = manager.list_active()
        listed = next(item for item in active if item["managed_execution_id"] == str(execution_id))
        assert listed["owner_kind"] == "agent_run"
        assert listed["project_id"] == str(fixture.project_id)
        assert listed["login_capable"] is True
        assert "password" not in repr(listed).lower()
        assert "database_url" not in listed

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


def test_issue_maintenance_creates_mnt_role_and_revokes(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    execution_id = uuid4()
    manager = _manager(fixture, tmp_path / "managed")
    try:
        issued = manager.issue_maintenance(
            managed_execution_id=execution_id,
            project_id=fixture.project_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )

        assert issued.credential.role_name == f"gobby_mnt_{execution_id.hex}_1"
        assert issued.dsn
        parsed = conninfo_to_dict(issued.dsn)
        assert parsed["user"] == issued.credential.role_name
        assert not issued.credential.role_name.startswith("gobby_ix_")

        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            owner = admin.execute(
                """SELECT owner_kind FROM gobby_agent_auth.principal_bindings
                   WHERE managed_execution_id = %s""",
                (execution_id,),
            ).fetchone()
        assert owner == ("maintenance",)

        connection = psycopg.connect(issued.dsn, autocommit=True)
        connection.execute("SELECT 1")
        outcome = manager.revoke(execution_id, reason="test-maintenance")
        assert outcome.completed is True
        with pytest.raises(psycopg.OperationalError):
            connection.execute("SELECT 1")
        connection.close()
    finally:
        manager.close()


def test_issue_maintenance_records_registered_overlay_claim(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    """A registered worktree's derived overlay id lands on the binding (#20889)."""
    from uuid import uuid5

    from gobby.code_index.models import CODE_INDEX_UUID_NAMESPACE

    fixture = authorization_fixture
    execution_id = uuid4()
    worktree_id = uuid4()
    worktree_path = f"/tmp/gobby-overlay-{execution_id.hex}"
    overlay_id = uuid5(CODE_INDEX_UUID_NAMESPACE, worktree_path)
    with psycopg.connect(fixture.database_url, autocommit=True) as admin:
        admin.execute(
            """INSERT INTO public.worktrees (
                   id, project_id, machine_id, branch_name, worktree_path
               ) VALUES (%s, %s, %s, %s, %s)""",
            (worktree_id, fixture.project_id, fixture.machine_id, "overlay-test", worktree_path),
        )
    manager = _manager(fixture, tmp_path / "managed")
    try:
        issued = manager.issue_maintenance(
            managed_execution_id=execution_id,
            project_id=fixture.project_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            code_overlay_project_id=overlay_id,
        )
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            bound = admin.execute(
                """SELECT code_overlay_project_id FROM gobby_agent_auth.principal_bindings
                   WHERE managed_execution_id = %s""",
                (execution_id,),
            ).fetchone()
        assert bound == (overlay_id,)
        manager.revoke(execution_id, reason="test-maintenance-overlay")
        del issued
    finally:
        manager.close()
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute("DELETE FROM public.worktrees WHERE id = %s", (worktree_id,))


def test_issue_maintenance_rejects_unregistered_overlay_claim(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path / "managed")
    try:
        with pytest.raises(CredentialIssuanceError):
            manager.issue_maintenance(
                managed_execution_id=uuid4(),
                project_id=fixture.project_id,
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
                code_overlay_project_id=uuid4(),
            )
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

    def transaction(self) -> AbstractContextManager[Transaction]:
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

    def transaction(self) -> AbstractContextManager[Transaction]:
        raise AssertionError("revocation must not open a hub transaction")


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = authorization_fixture
    monkeypatch.setattr(
        "gobby.storage.workspace_machine_scope.require_machine_id",
        lambda: str(fixture.machine_id),
    )
    manager = _manager(fixture, tmp_path / "managed")
    authoritative_path = str(Path(f"/tmp/checkout-{fixture.machine_id}").resolve())
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


def test_rotation_sweep_leaves_interactive_principals_to_their_own_path(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    """The generic sweep must not rotate a principal that owns a rotation path.

    rotate_interactive_principal drains the predecessor before creating the
    successor, carries the deployment token, mints a `gobby_ix_*` role and
    stores the password. rotate_principal does none of that, so a binding it
    converts is unusable -- and its token-less successor collides with the
    predecessor on uq_interactive_principal_active, which is what failed
    reconciliation at every daemon start.
    """
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path / "managed")
    store = _secret_store(fixture)
    token = "5weep5weep5weep0"
    agent_execution_id = uuid4()
    try:
        interactive = manager.issue_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        manager.issue(
            managed_execution_id=agent_execution_id,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        aged = [agent_execution_id, interactive.managed_execution_id]
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(
                f"UPDATE {AUTH_SCHEMA}.principal_bindings "
                "SET issued_at = NOW() - INTERVAL '46 minutes', "
                "expires_at = NOW() + INTERVAL '10 minutes' "
                "WHERE managed_execution_id = ANY(%s)",
                (aged,),
            )
            due = admin.execute(
                f"SELECT managed_execution_id FROM {AUTH_SCHEMA}.principals_due_for_rotation(%s)",
                (fixture.machine_id,),
            ).fetchall()

        due_ids = {UUID(str(row[0])) for row in due}
        assert agent_execution_id in due_ids
        assert interactive.managed_execution_id not in due_ids

        rotated = {str(credential.managed_execution_id) for credential in manager.rotate_due()}

        assert str(agent_execution_id) in rotated
        assert str(interactive.managed_execution_id) not in rotated
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            surviving = admin.execute(
                "SELECT role_name, deployment_token, credential_generation "
                f"FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s AND revoked_at IS NULL",
                (interactive.managed_execution_id,),
            ).fetchall()
        assert surviving == [(interactive.role_name, token, interactive.credential_generation)]
    finally:
        manager.revoke(agent_execution_id, reason="test-cleanup")
        manager.revoke_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            reason="test-cleanup",
        )
        manager.close()
        store.db.close()


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
            "INSERT INTO public.machines (id, hostname, owner_user_id) "
            "VALUES (%s, 'other-daemon-test', %s)",
            (other_machine_id, TEST_USER_ID),
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


def _secret_store(fixture: AuthorizationFixture) -> SecretStore:
    database = PostgresHubDatabase(fixture.database_url, runtime_role=RUNTIME_ROLE)
    database.open()
    return SecretStore(database)


def test_interactive_overlay_binds_registered_worktree_only(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path / "managed")
    store = _secret_store(fixture)
    token = "0ver1ay0ver1ay00"
    worktree_path = f"/tmp/{fixture.project_id}-overlay"
    with psycopg.connect(fixture.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO public.worktrees (id, project_id, machine_id, branch_name, worktree_path) "
            "VALUES (%s, %s, %s, 'overlay', %s)",
            (uuid4(), fixture.project_id, fixture.machine_id, worktree_path),
        )
        overlay_row = conn.execute(
            "SELECT gobby_agent_auth.code_index_project_id(%s)", (worktree_path,)
        ).fetchone()
        assert overlay_row is not None
        overlay_id = UUID(str(overlay_row[0]))
    try:
        main = manager.issue_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        overlay = manager.issue_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
            code_overlay_project_id=overlay_id,
        )
        reused = manager.issue_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
            code_overlay_project_id=overlay_id,
        )
        assert main.code_overlay_project_id is None
        assert overlay.code_overlay_project_id == overlay_id
        assert overlay.role_name != main.role_name
        assert reused.reused is True
        assert reused.role_name == overlay.role_name
        with psycopg.connect(overlay.dsn) as conn:
            conn.execute("INSERT INTO code_indexed_projects(id) VALUES (%s)", (overlay_id,))
            conn.commit()
        rotated = manager.rotate_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
            code_overlay_project_id=overlay_id,
        )
        assert rotated.code_overlay_project_id == overlay_id
        assert rotated.role_name not in {main.role_name, overlay.role_name}
        with pytest.raises(CredentialAuthorizationError):
            manager.issue_interactive(
                deployment_token=token,
                project_id=fixture.project_id,
                session_id=fixture.session_id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                secret_store=store,
                code_overlay_project_id=uuid4(),
            )
    finally:
        manager.revoke_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            reason="test-cleanup",
        )
        for generation in (overlay.credential_generation, rotated.credential_generation):
            manager.revoke_interactive(
                deployment_token=token,
                project_id=fixture.project_id,
                generation=generation,
                reason="test-cleanup",
            )
        manager.close()
        store.db.close()
        with psycopg.connect(fixture.database_url, autocommit=True) as conn:
            conn.execute("DELETE FROM public.code_indexed_projects WHERE id = %s", (overlay_id,))
            conn.execute("DELETE FROM public.worktrees WHERE worktree_path = %s", (worktree_path,))


def test_interactive_binding_uniqueness(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path / "managed")
    store = _secret_store(fixture)
    token_a = "aaaaaaaaaaaaaaaa"
    token_b = "bbbbbbbbbbbbbbbb"
    try:
        first = manager.issue_interactive(
            deployment_token=token_a,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        reused = manager.issue_interactive(
            deployment_token=token_a,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        other_project = manager.issue_interactive(
            deployment_token=token_a,
            project_id=fixture.other_project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        other_deploy = manager.issue_interactive(
            deployment_token=token_b,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        assert first.reused is False
        assert reused.reused is True
        assert reused.role_name == first.role_name
        assert reused.dsn == first.dsn
        assert other_project.role_name != first.role_name
        assert other_deploy.role_name != first.role_name
        with psycopg.connect(first.dsn) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        manager.revoke_interactive(
            deployment_token=token_a,
            project_id=fixture.project_id,
            reason="test-cleanup",
        )
        manager.revoke_interactive(
            deployment_token=token_a,
            project_id=fixture.other_project_id,
            reason="test-cleanup",
        )
        manager.revoke_interactive(
            deployment_token=token_b,
            project_id=fixture.project_id,
            reason="test-cleanup",
        )
        manager.close()
        store.db.close()


@pytest.mark.slow
def test_interactive_reuse_refreshes_expired_role(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path / "managed-expired")
    store = _secret_store(fixture)
    token = "dddddddddddddddd"
    try:
        first = manager.issue_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=2),
            secret_store=store,
        )

        def expired_dsn_rejected() -> bool:
            try:
                with psycopg.connect(first.dsn) as conn:
                    conn.execute("SELECT 1")
            except psycopg.OperationalError:
                return True
            return False

        wait_for_condition(
            expired_dsn_rejected,
            timeout=5.0,
            description="expired interactive role rejected",
        )
        refreshed = manager.issue_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        assert refreshed.reused is True
        assert refreshed.role_name == first.role_name
        with psycopg.connect(refreshed.dsn) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        manager.revoke_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            reason="test-cleanup",
        )
        manager.close()
        store.db.close()


def test_sessionless_operator_handshake_persists_null_session_id(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    """A sessionless operator handshake issues through the real chain and stores NULL (#20899).

    End-to-end: HandshakeService.issue_for_operator (what the daemon route
    calls) -> issue_grant_postgres -> ManagedCredentialManager.issue_interactive
    -> real principal_bindings row, with no mocks in the issuance path.
    """
    import time

    from gobby.runner_init.servers import issue_grant_postgres
    from gobby.runtime_grants.handshake import HandshakeService
    from gobby.runtime_grants.schema import GrantPrincipal, PostgresDirect
    from gobby.runtime_grants.service import DeploymentGrantContext, GrantService
    from tests.runtime_grants.support import (
        DEPLOYMENT_TOKEN,
        FENCING_EPOCH,
        GOLDEN_SECRET,
        StaticRuntime,
        config_snapshot,
        daemon_config,
    )

    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path / "managed-handshake")
    store = _secret_store(fixture)
    token = "b3b3b3b3b3b3b3b3"
    issued_directs: list[PostgresDirect] = []

    def issue_postgres(principal: GrantPrincipal) -> PostgresDirect:
        direct = issue_grant_postgres(
            principal,
            credentials=manager,
            deployment_token=token,
            secrets=store,
            managed_bootstrap_dsn=str,
        )
        issued_directs.append(direct)
        return direct

    grants = GrantService(
        runtime=StaticRuntime(config_snapshot(daemon_config(), revision=3)),
        context=DeploymentGrantContext(
            token=DEPLOYMENT_TOKEN,
            fencing_epoch=FENCING_EPOCH,
            signing_secret=GOLDEN_SECRET,
        ),
        clock=lambda: int(time.time()),
    )
    service = HandshakeService(
        grants=grants,
        local_machine_id=str(fixture.machine_id),
        operator_token="operator-token",
        issue_postgres=issue_postgres,
        admitted_projects=frozenset({str(fixture.project_id)}),
        clock=lambda: int(time.time()),
    )
    try:
        grant = service.issue_for_operator(
            machine_id=str(fixture.machine_id),
            project_id=str(fixture.project_id),
            session_id=None,
        )
        assert grant.principal.session_id is None
        assert len(issued_directs) == 1
        with psycopg.connect(issued_directs[0].dsn) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            row = admin.execute(
                f"SELECT session_id FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE deployment_token = %s AND project_id = %s AND revoked_at IS NULL",
                (token, fixture.project_id),
            ).fetchone()
        assert row == (None,)
    finally:
        manager.revoke_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            reason="test-cleanup",
        )
        manager.close()
        store.db.close()


def test_interactive_issue_without_session_persists_null_session_id(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    """An interactive issue with no session id stores NULL, never a minted one (#20899)."""
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path / "managed-nosession")
    store = _secret_store(fixture)
    token = "a2a2a2a2a2a2a2a2"
    try:
        issued = manager.issue_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=None,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        with psycopg.connect(issued.dsn) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            row = admin.execute(
                f"SELECT session_id FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s",
                (issued.managed_execution_id,),
            ).fetchone()
        assert row == (None,)
    finally:
        manager.revoke_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            reason="test-cleanup",
        )
        manager.close()
        store.db.close()


def test_interactive_issue_rolls_generation_past_credential_age_bound(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    """An aged binding rolls to a new generation instead of tripping the 24h guard (#20894)."""
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path / "managed-aged")
    store = _secret_store(fixture)
    token = "f1f1f1f1f1f1f1f1"
    try:
        first = manager.issue_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        # Age the binding past the 24h credential bound the way the live
        # dead-end arose: issued yesterday, expiry already behind us. Both
        # columns move together so the lifetime guard accepts the backdate.
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(
                f"UPDATE {AUTH_SCHEMA}.principal_bindings "
                "SET issued_at = NOW() - INTERVAL '25 hours', "
                "expires_at = NOW() - INTERVAL '1 hour' "
                "WHERE managed_execution_id = %s",
                (first.managed_execution_id,),
            )

        renewal_started = datetime.now(UTC)
        renewed = manager.issue_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )

        assert renewed.reused is False
        assert renewed.credential_generation == first.credential_generation + 1
        assert renewed.role_name != first.role_name
        with psycopg.connect(renewed.dsn) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            bindings = admin.execute(
                "SELECT credential_generation, predecessor_drain_deadline IS NOT NULL, "
                "revocation_requested_at IS NOT NULL, issued_at "
                f"FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE deployment_token = %s AND project_id = %s AND revoked_at IS NULL "
                "ORDER BY credential_generation",
                (token, fixture.project_id),
            ).fetchall()
            audit = admin.execute(
                f"SELECT event_type FROM {AUTH_SCHEMA}.principal_audit_events "
                "WHERE role_name = %s AND credential_generation = %s",
                (renewed.role_name, renewed.credential_generation),
            ).fetchall()
        assert [row[:3] for row in bindings] == [
            (first.credential_generation, True, True),
            (renewed.credential_generation, False, False),
        ]
        predecessor_issued_at, successor_issued_at = (row[3] for row in bindings)
        assert successor_issued_at > predecessor_issued_at
        # Fresh issued_at, tolerating sub-second skew between the Python and
        # Postgres clocks.
        assert successor_issued_at >= renewal_started - timedelta(seconds=5)
        assert audit == [("rotate",)]
    finally:
        manager.revoke_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            reason="test-cleanup",
        )
        manager.close()
        store.db.close()


def test_concurrent_interactive_issue_waits_for_sealed_material(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent issuer must block until the first issuer's material commits.

    Regression for handshake rejections with ``interactive credential material is
    missing``: the second issuer reused a freshly inserted binding before its
    sealed material row existed.
    """
    fixture = authorization_fixture
    store = _secret_store(fixture)
    first_manager = _manager(fixture, tmp_path / "managed-a")
    second_manager = _manager(fixture, tmp_path / "managed-b")
    token = "dddddddddddddddd"
    binding_inserted = threading.Event()
    release_store = threading.Event()
    original_store = first_manager._store_interactive_password

    def delayed_store(*args: Any, **kwargs: Any) -> None:
        binding_inserted.set()
        assert release_store.wait(timeout=10)
        original_store(*args, **kwargs)

    monkeypatch.setattr(first_manager, "_store_interactive_password", delayed_store)
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                first_manager.issue_interactive,
                deployment_token=token,
                project_id=fixture.project_id,
                session_id=fixture.session_id,
                expires_at=expires_at,
                secret_store=store,
            )
            assert binding_inserted.wait(timeout=10)
            second = pool.submit(
                second_manager.issue_interactive,
                deployment_token=token,
                project_id=fixture.project_id,
                session_id=fixture.session_id,
                expires_at=expires_at,
                secret_store=store,
            )
            with pytest.raises(TimeoutError):
                second.result(timeout=1.0)
            release_store.set()
            issued = first.result(timeout=10)
            reused = second.result(timeout=10)
        assert issued.reused is False
        assert reused.reused is True
        assert reused.dsn == issued.dsn
        assert _material_generations(fixture, token) == {issued.credential_generation}
    finally:
        release_store.set()
        second_manager.revoke_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            reason="test-cleanup",
        )
        first_manager.close()
        second_manager.close()
        store.db.close()


def test_interactive_reuse_after_restart(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    store = _secret_store(fixture)
    first_manager = _manager(fixture, tmp_path / "managed-a")
    token = "cccccccccccccccc"
    try:
        first = first_manager.issue_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        first_manager.close()
        restarted = _manager(fixture, tmp_path / "managed-b")
        try:
            reused = restarted.issue_interactive(
                deployment_token=token,
                project_id=fixture.project_id,
                session_id=fixture.session_id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                secret_store=store,
            )
            assert reused.reused is True
            assert reused.dsn == first.dsn
            assert reused.credential_generation == first.credential_generation
            rotated = restarted.rotate_interactive(
                deployment_token=token,
                project_id=fixture.project_id,
                session_id=fixture.session_id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                secret_store=store,
            )
            assert rotated.credential_generation == first.credential_generation + 1
            assert rotated.dsn != first.dsn
        finally:
            restarted.revoke_interactive(
                deployment_token=token,
                project_id=fixture.project_id,
                reason="test-cleanup",
            )
            restarted.close()
    finally:
        store.db.close()


def test_rotation_drains_predecessor_generations(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path / "managed")
    store = _secret_store(fixture)
    token = "dddddddddddddddd"
    try:
        first = manager.issue_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        manager.remember_interactive_grant_expiry(
            deployment_token=token,
            project_id=fixture.project_id,
            generation=first.credential_generation,
            expires_at=datetime.now(UTC) + timedelta(minutes=20),
        )
        rotated = manager.rotate_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        assert (
            token,
            fixture.project_id,
            None,
            first.credential_generation,
        ) not in manager._interactive_grant_expiry
        with psycopg.connect(first.dsn) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
        with psycopg.connect(rotated.dsn) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
        manager.revoke_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            generation=first.credential_generation,
            reason="explicit-revoke",
        )
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(first.dsn)
        assert manager.interactive_generation_revoked(
            deployment_token=token,
            project_id=fixture.project_id,
            generation=first.credential_generation,
        )
        assert _material_generations(fixture, token) == {rotated.credential_generation}
    finally:
        manager.revoke_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            reason="test-cleanup",
        )
        manager.close()
        store.db.close()


def test_credential_material_ciphertext_at_rest(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path / "managed")
    store = _secret_store(fixture)
    token = "eeeeeeeeeeeeeeee"
    try:
        issued = manager.issue_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        password = conninfo_to_dict(issued.dsn).get("password")
        assert isinstance(password, str) and password
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            rows = admin.execute(
                f"SELECT ciphertext, aad_identity, credential_generation "
                f"FROM {AUTH_SCHEMA}.interactive_credential_material "
                "WHERE deployment_token = %s AND machine_id = %s AND project_id = %s",
                (token, fixture.machine_id, fixture.project_id),
            ).fetchall()
        assert len(rows) == 1
        ciphertext, aad_identity, generation = rows[0]
        assert generation == issued.credential_generation
        assert password not in str(ciphertext)
        assert issued.dsn not in str(ciphertext)
        assert str(fixture.machine_id) in str(aad_identity)
        assert str(fixture.project_id) in str(aad_identity)
        assert token in str(aad_identity)
        manager.rotate_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            generations = {
                row[0]
                for row in admin.execute(
                    f"SELECT credential_generation "
                    f"FROM {AUTH_SCHEMA}.interactive_credential_material "
                    "WHERE deployment_token = %s AND machine_id = %s AND project_id = %s",
                    (token, fixture.machine_id, fixture.project_id),
                ).fetchall()
            }
        assert generations == {
            issued.credential_generation,
            issued.credential_generation + 1,
        }
    finally:
        manager.revoke_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            reason="test-cleanup",
        )
        manager.close()
        store.db.close()


def _material_generations(
    fixture: AuthorizationFixture,
    deployment_token: str,
) -> set[int]:
    with psycopg.connect(fixture.database_url, autocommit=True) as admin:
        return {
            row[0]
            for row in admin.execute(
                f"SELECT credential_generation "
                f"FROM {AUTH_SCHEMA}.interactive_credential_material "
                "WHERE deployment_token = %s AND machine_id = %s AND project_id = %s",
                (deployment_token, fixture.machine_id, fixture.project_id),
            ).fetchall()
        }


def test_reuse_after_rotation_rollback_loads_predecessor_material(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path / "managed")
    store = _secret_store(fixture)
    token = "ffffffffffffffff"
    first = None
    try:
        first = manager.issue_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        rotated = manager.rotate_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        assert _material_generations(fixture, token) == {
            first.credential_generation,
            rotated.credential_generation,
        }
        outcome = manager.revoke_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            generation=rotated.credential_generation,
            reason="rotation-rollback",
        )
        assert outcome.completed
        assert _material_generations(fixture, token) == {first.credential_generation}
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(
                f"UPDATE {AUTH_SCHEMA}.principal_bindings "
                "SET predecessor_drain_deadline = NULL, revocation_requested_at = NULL "
                "WHERE owner_kind = 'interactive' AND deployment_token = %s "
                "AND issuing_machine_id = %s AND project_id = %s "
                "AND credential_generation = %s",
                (
                    token,
                    fixture.machine_id,
                    fixture.project_id,
                    first.credential_generation,
                ),
            )
        reused = manager.issue_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        assert reused.reused is True
        assert reused.credential_generation == first.credential_generation
        with psycopg.connect(reused.dsn) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        # Generation-less lookup picks the highest (revoked) generation, so the
        # restored predecessor must be revoked explicitly.
        if first is not None:
            manager.revoke_interactive(
                deployment_token=token,
                project_id=fixture.project_id,
                generation=first.credential_generation,
                reason="test-cleanup",
            )
        manager.close()
        store.db.close()


def test_store_skips_revoked_generation(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path / "managed")
    store = _secret_store(fixture)
    token = "abababababababab"
    try:
        issued = manager.issue_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        manager.revoke_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            generation=issued.credential_generation,
            reason="test-revoke",
        )
        assert _material_generations(fixture, token) == set()
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(
                f"SELECT {AUTH_SCHEMA}.replace_interactive_credential_material("
                "%s, %s, %s, %s, %s, %s)",
                (
                    token,
                    fixture.machine_id,
                    fixture.project_id,
                    issued.credential_generation,
                    "late-store-ciphertext",
                    "late-store-aad",
                ),
            )
        assert _material_generations(fixture, token) == set()
    finally:
        manager.close()
        store.db.close()


def _role_exists(database_url: str, role_name: str) -> bool:
    with psycopg.connect(database_url, autocommit=True) as admin:
        return admin.execute("SELECT to_regrole(%s)", (role_name,)).fetchone() != (None,)


def _drop_role_if_exists(database_url: str, role_name: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as admin:
        if admin.execute("SELECT to_regrole(%s)", (role_name,)).fetchone() != (None,):
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role_name)))


def _plant_login_role(database_url: str, role_name: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute("SET ROLE gobby_agent_issuer")
        try:
            admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role_name)))
        finally:
            admin.execute("RESET ROLE")


def test_hash_format_ix_orphan_does_not_42710_on_issue(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path / "orphan-hash-issue")
    store = _secret_store(fixture)
    token = "orphanhash000001"
    planted: str | None = None
    try:
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            row = admin.execute(
                f"SELECT {AUTH_SCHEMA}.interactive_role_name(%s, %s, %s, %s)",
                (token, fixture.machine_id, fixture.project_id, 1),
            ).fetchone()
            assert row is not None
            planted = str(row[0])
        _plant_login_role(fixture.database_url, planted)
        issued = manager.issue_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        assert issued.reused is False
        assert issued.role_name == planted
        with psycopg.connect(issued.dsn) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        manager.revoke_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            reason="test-cleanup",
        )
        manager.close()
        store.db.close()
        if planted is not None:
            _drop_role_if_exists(fixture.database_url, planted)


def test_reconcile_reaps_slug_and_mnt_orphans(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path / "orphan-slug-mnt")
    slug_role = "gobby_ix_tokentok_deadbeef_cafed00d_1"
    mnt_role = f"gobby_mnt_{uuid4().hex}_1"
    try:
        _plant_login_role(fixture.database_url, slug_role)
        _plant_login_role(fixture.database_url, mnt_role)
        manager.reconcile()
        assert not _role_exists(fixture.database_url, slug_role)
        assert not _role_exists(fixture.database_url, mnt_role)
    finally:
        _drop_role_if_exists(fixture.database_url, slug_role)
        _drop_role_if_exists(fixture.database_url, mnt_role)
        manager.close()


def test_reconcile_spares_bound_ix_and_unmatched_names(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path / "orphan-spare")
    store = _secret_store(fixture)
    token = "orphanspare000001"
    decoy = "gobby_ix_test"
    try:
        issued = manager.issue_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            session_id=fixture.session_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            secret_store=store,
        )
        _plant_login_role(fixture.database_url, decoy)
        manager.reconcile()
        assert _role_exists(fixture.database_url, issued.role_name)
        assert _role_exists(fixture.database_url, decoy)
    finally:
        manager.revoke_interactive(
            deployment_token=token,
            project_id=fixture.project_id,
            reason="test-cleanup",
        )
        _drop_role_if_exists(fixture.database_url, decoy)
        manager.close()
        store.db.close()


def test_drain_reaps_hash_format_ix_orphan(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path / "orphan-drain")
    planted: str | None = None
    try:
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            row = admin.execute(
                f"SELECT {AUTH_SCHEMA}.interactive_role_name(%s, %s, %s, %s)",
                ("orphandrain000001", fixture.machine_id, fixture.project_id, 1),
            ).fetchone()
            assert row is not None
            planted = str(row[0])
        _plant_login_role(fixture.database_url, planted)
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            _as_runtime(admin, f"SELECT {AUTH_SCHEMA}.drain_ephemeral_principals()", ())
            assert admin.execute("SELECT to_regrole(%s)", (planted,)).fetchone() == (None,)
    finally:
        manager.close()
        if planted is not None:
            _drop_role_if_exists(fixture.database_url, planted)


def test_issue_tool_request_accepts_registered_overlay_without_primary(  # tdd-red window
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = authorization_fixture
    overlay = tmp_path / "overlay-wt"
    overlay.mkdir()
    overlay_path = str(overlay.resolve())
    monkeypatch.setattr(
        "gobby.storage.workspace_machine_scope.require_machine_id",
        lambda: str(fixture.machine_id),
    )
    with psycopg.connect(fixture.database_url, autocommit=True) as admin:
        admin.execute(
            "DELETE FROM public.project_checkouts WHERE machine_id = %s AND project_id = %s",
            (fixture.machine_id, fixture.project_id),
        )
        admin.execute(
            """
            INSERT INTO public.worktrees (
                id, project_id, machine_id, branch_name, worktree_path
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (uuid4(), fixture.project_id, fixture.machine_id, "overlay", overlay_path),
        )
    manager = _manager(fixture, tmp_path / "managed")
    lease: ManagedToolCredential | None = None
    error: CredentialAuthorizationError | None = None
    try:
        try:
            lease = manager.issue_tool_request(
                session_id=fixture.session_id,
                requested_project_path=overlay_path,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        except CredentialAuthorizationError as exc:
            error = exc
        assert error is None
        assert lease is not None
        assert lease.project_id == fixture.project_id
        assert Path(lease.project_path).resolve() == overlay.resolve()
    finally:
        if lease is not None:
            manager.revoke(
                lease.credential.managed_execution_id,
                reason="overlay-tool-request-finally",
            )
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(
                "DELETE FROM public.worktrees WHERE project_id = %s",
                (fixture.project_id,),
            )
        manager.close()


def test_issue_tool_request_resolves_symlinked_overlay_to_registered_path(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symlink to a registered overlay authorizes; the lease carries the registered path."""
    fixture = authorization_fixture
    overlay = tmp_path / "overlay-real"
    overlay.mkdir()
    link = tmp_path / "overlay-link"
    link.symlink_to(overlay, target_is_directory=True)
    registered_path = os.path.realpath(overlay)
    monkeypatch.setattr(
        "gobby.storage.workspace_machine_scope.require_machine_id",
        lambda: str(fixture.machine_id),
    )
    with psycopg.connect(fixture.database_url, autocommit=True) as admin:
        admin.execute(
            """
            INSERT INTO public.worktrees (
                id, project_id, machine_id, branch_name, worktree_path
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (uuid4(), fixture.project_id, fixture.machine_id, "overlay-link", registered_path),
        )
    manager = _manager(fixture, tmp_path / "managed")
    lease: ManagedToolCredential | None = None
    try:
        lease = manager.issue_tool_request(
            session_id=fixture.session_id,
            requested_project_path=str(link),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        assert lease.project_id == fixture.project_id
        assert lease.project_path == registered_path
    finally:
        if lease is not None:
            manager.revoke(
                lease.credential.managed_execution_id,
                reason="symlink-overlay-finally",
            )
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(
                "DELETE FROM public.worktrees WHERE project_id = %s",
                (fixture.project_id,),
            )
        manager.close()


def test_issue_tool_request_rejects_unregistered_overlay(  # tdd-red window
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = authorization_fixture
    monkeypatch.setattr(
        "gobby.storage.workspace_machine_scope.require_machine_id",
        lambda: str(fixture.machine_id),
    )
    manager = _manager(fixture, tmp_path / "managed")
    error: CredentialAuthorizationError | None = None
    try:
        try:
            manager.issue_tool_request(
                session_id=fixture.session_id,
                requested_project_path=str((tmp_path / "unregistered").resolve()),
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        except CredentialAuthorizationError as exc:
            error = exc
        assert error is not None
    finally:
        manager.close()


def test_issue_tool_request_rejects_foreign_session_machine(  # tdd-red window
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = authorization_fixture
    monkeypatch.setattr(
        "gobby.storage.workspace_machine_scope.require_machine_id",
        lambda: str(fixture.machine_id),
    )
    manager = _manager(fixture, tmp_path / "managed")
    authoritative_path = str(Path(f"/tmp/{fixture.project_id}").resolve())
    error: CredentialAuthorizationError | None = None
    lease: ManagedToolCredential | None = None
    try:
        try:
            lease = manager.issue_tool_request(
                session_id=fixture.other_session_id,
                requested_project_path=authoritative_path,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        except CredentialAuthorizationError as exc:
            error = exc
        assert error is not None
        assert lease is None
    finally:
        if lease is not None:
            manager.revoke(
                lease.credential.managed_execution_id,
                reason="foreign-machine-finally",
            )
        manager.close()
