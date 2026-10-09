"""Managed rotation grants existing connections a bounded predecessor drain."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest

from gobby.storage.managed_credentials import CredentialIssuanceError
from tests.storage.test_managed_credentials import _manager
from tests.storage.test_postgres_agent_authorization import AUTH_SCHEMA, AuthorizationFixture

pytestmark = pytest.mark.integration


def _expire_drain(fixture: AuthorizationFixture, execution: UUID) -> None:
    with psycopg.connect(fixture.database_url, autocommit=True) as admin:
        admin.execute(
            f"UPDATE {AUTH_SCHEMA}.principal_bindings "
            "SET revocation_requested_at = NOW() - INTERVAL '1 minute', "
            "predecessor_drain_deadline = NOW() - INTERVAL '1 second' "
            "WHERE managed_execution_id = %s AND predecessor_drain_deadline IS NOT NULL",
            (execution,),
        )


def _age_current(fixture: AuthorizationFixture, execution: UUID, *, expired: bool) -> None:
    with psycopg.connect(fixture.database_url, autocommit=True) as admin:
        admin.execute(
            f"UPDATE {AUTH_SCHEMA}.principal_bindings "
            "SET issued_at = NOW() - CASE WHEN %s THEN INTERVAL '2 minutes' "
            "ELSE INTERVAL '61 minutes' END, "
            "expires_at = CASE WHEN %s THEN NOW() - INTERVAL '1 minute' ELSE expires_at END "
            "WHERE managed_execution_id = %s AND revocation_requested_at IS NULL",
            (expired, expired, execution),
        )


def test_rotation_keeps_holder_alive_until_predecessor_deadline(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    execution = uuid4()
    try:
        initial = manager.issue(
            managed_execution_id=execution,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=59),
        )
        initial_dsn = str(json.loads(initial.bootstrap_path.read_bytes())["database_url"])
        with psycopg.connect(initial_dsn, autocommit=True) as holder:
            assert holder.execute("SELECT 1").fetchone() == (1,)
            successor = manager.rotate(
                managed_execution_id=execution,
                expires_at=datetime.now(UTC) + timedelta(minutes=59),
            )
            assert successor.credential_generation == 2
            assert holder.execute("SELECT 1").fetchone() == (1,)
            manager.reconcile()
            assert holder.execute("SELECT 1").fetchone() == (1,)
            with psycopg.connect(initial_dsn, autocommit=True) as predecessor_login:
                assert predecessor_login.execute("SELECT 1").fetchone() == (1,)
            with psycopg.connect(fixture.database_url, autocommit=True) as admin:
                admin.execute(
                    f"UPDATE {AUTH_SCHEMA}.principal_bindings "
                    "SET issued_at = NOW() - INTERVAL '61 minutes', "
                    "expires_at = NOW() - INTERVAL '1 minute' "
                    "WHERE managed_execution_id = %s AND credential_generation = 1",
                    (execution,),
                )
            manager.reconcile()
            assert holder.execute("SELECT 1").fetchone() == (1,)
            with psycopg.connect(fixture.database_url, autocommit=True) as admin:
                deadline = admin.execute(
                    f"SELECT predecessor_drain_deadline - revocation_requested_at "
                    f"FROM {AUTH_SCHEMA}.principal_bindings "
                    "WHERE managed_execution_id = %s AND credential_generation = %s",
                    (execution, initial.credential_generation),
                ).fetchone()
                assert deadline is not None
                assert timedelta(0) < deadline[0] <= timedelta(minutes=5)
                admin.execute(
                    f"UPDATE {AUTH_SCHEMA}.principal_bindings "
                    "SET revocation_requested_at = NOW() - INTERVAL '1 minute', "
                    "predecessor_drain_deadline = NOW() - INTERVAL '1 second' "
                    "WHERE managed_execution_id = %s AND credential_generation = %s",
                    (execution, initial.credential_generation),
                )
            manager.reconcile()
            with pytest.raises(psycopg.OperationalError):
                holder.execute("SELECT 1")
        current_dsn = str(json.loads(successor.bootstrap_path.read_bytes())["database_url"])
        with psycopg.connect(current_dsn, autocommit=True) as current:
            assert current.execute("SELECT 1").fetchone() == (1,)
    finally:
        manager.revoke(execution, reason="test-cleanup")
        manager.close()


def test_periodic_cleanup_recovers_crash_and_defers_live_drain(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    execution = uuid4()

    class SimulatedCrash(BaseException):
        pass

    materialize = manager._materialize_bootstrap

    def crash_after_publication(
        *,
        managed_execution_id: UUID,
        role_name: str,
        generation: int,
        expires_at: datetime,
        scoped_dsn: str,
    ) -> Path:
        materialize(
            managed_execution_id=managed_execution_id,
            role_name=role_name,
            generation=generation,
            expires_at=expires_at,
            scoped_dsn=scoped_dsn,
        )
        raise SimulatedCrash

    try:
        initial = manager.issue(
            managed_execution_id=execution,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=59),
        )
        with monkeypatch.context() as patch:
            patch.setattr(manager, "_materialize_bootstrap", crash_after_publication)
            with pytest.raises(SimulatedCrash):
                manager.rotate(
                    managed_execution_id=execution,
                    expires_at=datetime.now(UTC) + timedelta(minutes=59),
                )
        assert manager._rotation_owner_is_live(execution)
        with pytest.raises(CredentialIssuanceError, match="lost binding race"):
            manager.rotate(
                managed_execution_id=execution,
                expires_at=datetime.now(UTC) + timedelta(minutes=59),
            )
        _age_current(fixture, execution, expired=False)
        assert manager.rotate_due() == []
        _expire_drain(fixture, execution)
        rotated = manager.rotate_due()
        assert [item.credential_generation for item in rotated] == [3]
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            predecessor = admin.execute(
                f"SELECT revoked_at FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s AND credential_generation = 1",
                (execution,),
            ).fetchone()
            assert predecessor is not None and predecessor[0] is not None
        assert json.loads(initial.bootstrap_path.read_bytes())["credential_generation"] == 3
    finally:
        manager.revoke(execution, reason="test-cleanup")
        manager.close()


@pytest.mark.parametrize("with_live_drain", [False, True])
def test_periodic_refresh_repairs_expired_current_before_revoke(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    with_live_drain: bool,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    execution = uuid4()
    try:
        manager.issue(
            managed_execution_id=execution,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=59),
        )
        if with_live_drain:
            manager.rotate(
                managed_execution_id=execution,
                expires_at=datetime.now(UTC) + timedelta(minutes=59),
            )
        _age_current(fixture, execution, expired=True)
        rotated = manager.rotate_due()
        assert [item.credential_generation for item in rotated] == [3 if with_live_drain else 2]
        assert manager._rotation_owner_is_live(execution)
    finally:
        manager.revoke(execution, reason="test-cleanup")
        manager.close()


def test_drain_deadline_does_not_outlive_successor_expiry(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    execution = uuid4()
    try:
        manager.issue(
            managed_execution_id=execution,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=59),
        )
        successor = manager.rotate(
            managed_execution_id=execution,
            expires_at=datetime.now(UTC) + timedelta(minutes=1),
        )
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            predecessor = admin.execute(
                f"SELECT predecessor_drain_deadline FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s AND credential_generation = 1",
                (execution,),
            ).fetchone()
            assert predecessor is not None and predecessor[0] <= successor.expires_at
    finally:
        manager.revoke(execution, reason="test-cleanup")
        manager.close()


def test_binding_state_api_is_machine_scoped_secret_free_and_runtime_only(
    authorization_fixture: AuthorizationFixture,
) -> None:
    fixture = authorization_fixture
    with psycopg.connect(fixture.database_url, autocommit=True) as admin:
        admin.execute("SET ROLE gobby_daemon_runtime")
        result = admin.execute(
            f"SELECT * FROM {AUTH_SCHEMA}.managed_binding_states(%s)",
            (fixture.machine_id,),
        )
        assert result.description is not None
        assert {column.name for column in result.description} == {
            "managed_execution_id",
            "role_name",
            "credential_generation",
            "owner_kind",
            "agent_run_id",
            "session_id",
            "project_id",
            "issuing_machine_id",
            "expires_at",
            "revocation_requested_at",
            "predecessor_drain_deadline",
            "login_capable",
            "code_overlay_project_id",
        }
        rows = result.fetchall()
        assert rows and all(row[7] == fixture.machine_id for row in rows)
        assert fixture.other_execution_id not in {row[0] for row in rows}
        admin.execute("RESET ROLE")
        for role in ("gobby_gcode_capability", fixture.role_name):
            allowed = admin.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                (role, f"{AUTH_SCHEMA}.managed_binding_states(uuid)"),
            ).fetchone()
            assert allowed == (False,)


@pytest.mark.parametrize("ambiguity", ["missing_request", "overlong_drain", "wrong_owner"])
def test_owner_guard_refuses_unproven_predecessor(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    ambiguity: str,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    execution = uuid4()
    try:
        initial = manager.issue(
            managed_execution_id=execution,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=59),
        )
        manager.rotate(
            managed_execution_id=execution,
            expires_at=datetime.now(UTC) + timedelta(minutes=59),
        )
        original = initial.bootstrap_path.read_bytes()
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(
                f"UPDATE {AUTH_SCHEMA}.principal_bindings "
                "SET revocation_requested_at = CASE WHEN %s = 'missing_request' "
                "THEN NULL ELSE revocation_requested_at END, "
                "predecessor_drain_deadline = CASE WHEN %s = 'overlong_drain' "
                "THEN revocation_requested_at + INTERVAL '10 minutes' "
                "ELSE predecessor_drain_deadline END, "
                "session_id = CASE WHEN %s = 'wrong_owner' THEN %s ELSE session_id END "
                "WHERE managed_execution_id = %s AND credential_generation = 1",
                (ambiguity, ambiguity, ambiguity, fixture.other_session_id, execution),
            )
        assert not manager._rotation_owner_is_live(execution)
        with pytest.raises(CredentialIssuanceError, match="lost binding race"):
            manager.rotate(
                managed_execution_id=execution,
                expires_at=datetime.now(UTC) + timedelta(minutes=59),
            )
        assert initial.bootstrap_path.read_bytes() == original
    finally:
        manager.revoke(execution, reason="test-cleanup")
        manager.close()


@pytest.mark.parametrize("reason", ["agent-exit", "security-revoke", "rotation-rollback"])
def test_explicit_revocation_still_terminates_holder_immediately(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    reason: str,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    execution = uuid4()
    try:
        initial = manager.issue(
            managed_execution_id=execution,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=59),
        )
        dsn = str(json.loads(initial.bootstrap_path.read_bytes())["database_url"])
        with psycopg.connect(dsn, autocommit=True) as holder:
            assert holder.execute("SELECT 1").fetchone() == (1,)
            outcome = manager.revoke(execution, reason=reason)
            assert outcome.completed
            with pytest.raises(psycopg.OperationalError):
                holder.execute("SELECT 1")
    finally:
        manager.revoke(execution, reason="test-cleanup")
        manager.close()
