"""Recovery of live seats after principal revocation."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import psycopg
import pytest

from gobby.cli.hub_backup._stores import drain_ephemeral_principals
from gobby.runner_init.servers import issue_grant_postgres
from gobby.runtime_grants.handshake import HandshakeRejection
from gobby.runtime_grants.launch import materialize_managed_launch, rewrite_managed_grant_file
from gobby.runtime_grants.schema import GrantBundle, GrantPrincipal, PostgresDirect
from gobby.storage.managed_credential_types import OwnerKind
from gobby.storage.managed_credentials import (
    CredentialAuthorizationError,
    ManagedCredential,
    ManagedCredentialManager,
)
from gobby.utils import local_token
from tests.runtime_grants.support import DEPLOYMENT_TOKEN, FENCING_EPOCH, GOLDEN_SECRET
from tests.storage.test_managed_credentials import _manager, _secret_store
from tests.storage.test_postgres_agent_authorization import (
    AUTH_SCHEMA,
    AuthorizationFixture,
    _as_runtime,
)
from tests.storage.test_postgres_agent_authorization import (
    fresh_authorization_fixture as authorization_fixture,
)

pytestmark = pytest.mark.integration
__all__ = ["authorization_fixture"]


@pytest.fixture(autouse=True)
def recovery_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_token, "read_managed_signing_key", lambda: b"recovery-test-key")


def _dsn(path: object) -> str:
    return str(json.loads(Path(str(path)).read_bytes())["database_url"])


def _principal(fixture: AuthorizationFixture) -> GrantPrincipal:
    return GrantPrincipal(
        kind="agent_run",
        execution_id=str(fixture.agent_run_id),
        session_id=str(fixture.session_id),
        project_id=str(fixture.project_id),
        machine_id=str(fixture.machine_id),
    )


@pytest.mark.parametrize("revocation", ["forced", "system", "expired_restart"])
def test_live_revoked_run_re_handshakes_at_next_generation(
    authorization_fixture: AuthorizationFixture, tmp_path: Path, revocation: str
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    store = _secret_store(fixture)
    execution_id = fixture.agent_run_id
    try:
        first = manager.issue(
            managed_execution_id=execution_id,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=execution_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        previous = manager.rotate(
            managed_execution_id=execution_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        assert previous.credential_generation == first.credential_generation + 1
        if revocation == "forced":
            manager.force_revoke(execution_id)
        elif revocation == "system":
            manager.revoke(execution_id, reason="test-drain")
        else:
            with psycopg.connect(fixture.database_url, autocommit=True) as admin:
                admin.execute(
                    f"UPDATE {AUTH_SCHEMA}.principal_bindings SET issued_at = NOW() - "
                    "INTERVAL '2 minutes', expires_at = NOW() - "
                    "INTERVAL '1 minute', predecessor_drain_deadline = NULL "
                    "WHERE managed_execution_id = %s",
                    (execution_id,),
                )
            manager.reconcile()
        assert manager.get_live_binding_generation(execution_id) is None
        if revocation == "forced":
            with pytest.raises(HandshakeRejection):
                issue_grant_postgres(
                    _principal(fixture),
                    credentials=manager,
                    deployment_token=DEPLOYMENT_TOKEN,
                    secrets=store,
                    managed_bootstrap_dsn=_dsn,
                )
            with psycopg.connect(fixture.database_url, autocommit=True) as admin:
                assert admin.execute(
                    f"SELECT max(credential_generation) FROM {AUTH_SCHEMA}.principal_bindings "
                    "WHERE managed_execution_id = %s",
                    (execution_id,),
                ).fetchone() == (previous.credential_generation,)
            return
        direct = issue_grant_postgres(
            _principal(fixture),
            credentials=manager,
            deployment_token=DEPLOYMENT_TOKEN,
            secrets=store,
            managed_bootstrap_dsn=_dsn,
        )
        assert direct.credential_generation == previous.credential_generation + 1
        assert _dsn(previous.bootstrap_path) == direct.dsn
        with psycopg.connect(direct.dsn, autocommit=True) as agent:
            assert agent.execute("SELECT current_user").fetchone() == (direct.role_name,)
    finally:
        manager.revoke(execution_id, reason="test-cleanup")
        manager.close()
        store.db.close()


def test_revoked_ended_run_cannot_re_handshake(
    authorization_fixture: AuthorizationFixture, tmp_path: Path
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    store = _secret_store(fixture)
    try:
        manager.issue(
            managed_execution_id=fixture.agent_run_id,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        manager.revoke(fixture.agent_run_id, reason="test-ended")
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(
                "UPDATE agent_runs SET status = 'success' WHERE id = %s", (fixture.agent_run_id,)
            )
        with pytest.raises(HandshakeRejection):
            issue_grant_postgres(
                _principal(fixture),
                credentials=manager,
                deployment_token=DEPLOYMENT_TOKEN,
                secrets=store,
                managed_bootstrap_dsn=_dsn,
            )
        assert manager.get_live_binding_generation(fixture.agent_run_id) is None
    finally:
        manager.revoke(fixture.agent_run_id, reason="test-cleanup")
        manager.close()
        store.db.close()


def test_restart_sweep_recovers_revoked_run_and_renews_api_capability(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    signing_key = b"recovery-test-key"
    monkeypatch.setattr(local_token, "read_managed_signing_key", lambda: signing_key)
    try:
        first = manager.issue(
            managed_execution_id=fixture.agent_run_id,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        golden = Path(__file__).parents[1] / "runtime_grants/golden/direct_datastores.json"
        grant = GrantBundle.model_validate_json(golden.read_bytes()).model_copy(
            update={
                "principal": _principal(fixture),
            }
        )
        now = int(datetime.now(UTC).timestamp())
        with monkeypatch.context() as old_clock:
            old_clock.setattr("gobby.utils.local_token.time.time", lambda: now - 7200)
            launch = materialize_managed_launch(
                grant,
                dest_dir=first.bootstrap_path.parent,
                signing_key=signing_key,
                deadline_seconds=1,
            )
        old_token = local_token.read_managed_api_token(launch.env)
        assert old_token is not None
        assert local_token.verify_agent_api_token(old_token, signing_key) is None
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(
                f"SELECT {AUTH_SCHEMA}.revoke_principal(%s, NULL)", (fixture.agent_run_id,)
            )
        manager.close()
        manager = _manager(fixture, tmp_path)

        def rewrite(credential: ManagedCredential, scoped_dsn: str) -> None:
            rewrite_managed_grant_file(
                launch.grant_path,
                managed_execution_id=str(credential.managed_execution_id),
                scoped_dsn=scoped_dsn,
                role_name=credential.role_name,
                credential_generation=credential.credential_generation,
                issued_at=credential.issued_at,
                expires_at=credential.expires_at,
                deployment_token=DEPLOYMENT_TOKEN,
                fencing_epoch=FENCING_EPOCH,
                signing_secret=GOLDEN_SECRET,
            )

        manager.bind_launch_grant_rewriter(rewrite)
        recovered = manager.rotate_due()
        manager.reconcile()
        assert [item.credential_generation for item in recovered] == [2]
        renewed = local_token.read_managed_api_token(launch.env)
        assert renewed is not None and renewed != old_token
        claims = local_token.verify_agent_api_token(renewed, signing_key)
        assert isinstance(claims, local_token.AgentApiTokenClaims)
        assert claims.agent_run_id == str(fixture.agent_run_id)
        rewritten = GrantBundle.model_validate_json(launch.grant_path.read_bytes())
        assert isinstance(rewritten.capabilities.postgres, PostgresDirect)
        assert rewritten.capabilities.postgres.credential_generation == 2
        assert rewritten.capabilities.postgres.dsn == _dsn(first.bootstrap_path)
        with psycopg.connect(rewritten.capabilities.postgres.dsn, autocommit=True) as agent:
            assert agent.execute("SELECT current_user").fetchone() == (recovered[0].role_name,)
    finally:
        manager.revoke(fixture.agent_run_id, reason="test-cleanup")
        manager.close()


def test_backup_drain_preserves_live_run_and_drains_ended_run(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    try:
        credential = _launch_recovery_run(fixture, manager)
        grant_path = credential.bootstrap_path.parent / "grant.json"
        before = grant_path.read_bytes()
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(
                "UPDATE agent_runs SET status = 'success' WHERE id = %s",
                (fixture.other_agent_run_id,),
            )
            drain_ephemeral_principals(fixture.database_url)
            live = admin.execute(
                f"SELECT revoked_at, revocation_requested_at FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s",
                (fixture.agent_run_id,),
            ).fetchone()
            ended = admin.execute("SELECT to_regrole(%s)", (fixture.other_role_name,)).fetchone()
        assert live == (None, None)
        assert ended == (None,)
        assert grant_path.read_bytes() == before
        with psycopg.connect(_dsn(credential.bootstrap_path), autocommit=True) as agent:
            assert agent.execute("SELECT current_user").fetchone() == (credential.role_name,)
    finally:
        manager.revoke(fixture.agent_run_id, reason="test-cleanup")
        manager.close()


def _launch_recovery_run(
    fixture: AuthorizationFixture, manager: ManagedCredentialManager
) -> ManagedCredential:
    credential = manager.issue(
        managed_execution_id=fixture.agent_run_id,
        owner_kind="agent_run",
        session_id=fixture.session_id,
        agent_run_id=fixture.agent_run_id,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    golden = Path(__file__).parents[1] / "runtime_grants/golden/direct_datastores.json"
    grant = GrantBundle.model_validate_json(golden.read_bytes()).model_copy(
        update={"principal": _principal(fixture)}
    )
    materialize_managed_launch(
        grant,
        dest_dir=credential.bootstrap_path.parent,
        signing_key=b"recovery-test-key",
        deadline_seconds=1,
    )
    return credential


def _rewrite_recovery_grant(credential: ManagedCredential, scoped_dsn: str) -> None:
    rewrite_managed_grant_file(
        credential.bootstrap_path.parent / "grant.json",
        managed_execution_id=str(credential.managed_execution_id),
        scoped_dsn=scoped_dsn,
        role_name=credential.role_name,
        credential_generation=credential.credential_generation,
        issued_at=credential.issued_at,
        expires_at=credential.expires_at,
        deployment_token=DEPLOYMENT_TOKEN,
        fencing_epoch=FENCING_EPOCH,
        signing_secret=GOLDEN_SECRET,
    )


def test_force_revoke_survives_rehandshake_and_startup_sweep(
    authorization_fixture: AuthorizationFixture, tmp_path: Path
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    store = _secret_store(fixture)
    credential = _launch_recovery_run(fixture, manager)
    grant_path = credential.bootstrap_path.parent / "grant.json"
    original_grant = grant_path.read_bytes()
    manager.bind_launch_grant_rewriter(_rewrite_recovery_grant)
    try:
        assert manager.force_revoke(fixture.agent_run_id).completed
        with pytest.raises(HandshakeRejection):
            issue_grant_postgres(
                _principal(fixture),
                credentials=manager,
                deployment_token=DEPLOYMENT_TOKEN,
                secrets=store,
                managed_bootstrap_dsn=_dsn,
            )
        manager.close()
        manager = _manager(fixture, tmp_path)
        manager.bind_launch_grant_rewriter(_rewrite_recovery_grant)
        assert manager.rotate_due() == []
        manager.reconcile()
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            assert admin.execute(
                f"SELECT count(*) FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s AND revoked_at IS NULL",
                (fixture.agent_run_id,),
            ).fetchone() == (0,)
        assert grant_path.read_bytes() == original_grant
        assert manager.release_forced_revocation(fixture.agent_run_id)
        direct = issue_grant_postgres(
            _principal(fixture),
            credentials=manager,
            deployment_token=DEPLOYMENT_TOKEN,
            secrets=store,
            managed_bootstrap_dsn=_dsn,
        )
        assert direct.credential_generation == 2
        with psycopg.connect(direct.dsn, autocommit=True) as agent:
            assert agent.execute("SELECT current_user").fetchone() == (direct.role_name,)
    finally:
        manager.revoke(fixture.agent_run_id, reason="test-cleanup")
        manager.close()
        store.db.close()


def test_pending_forced_revocation_is_not_completed_and_reminted(
    authorization_fixture: AuthorizationFixture, tmp_path: Path
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    store = _secret_store(fixture)
    _launch_recovery_run(fixture, manager)
    manager.bind_launch_grant_rewriter(_rewrite_recovery_grant)
    try:
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            _as_runtime(
                admin, f"SELECT {AUTH_SCHEMA}.force_revoke_principal(%s)", (fixture.agent_run_id,)
            )
            # Preserve a pending row after cleanup, as a failed revocation retry
            # would. The SQL-only crash regression below never edits bindings.
            admin.execute(
                f"UPDATE {AUTH_SCHEMA}.principal_bindings "
                "SET revocation_requested_at = NOW(), revoked_at = NULL "
                "WHERE managed_execution_id = %s",
                (fixture.agent_run_id,),
            )
        with pytest.raises(HandshakeRejection):
            issue_grant_postgres(
                _principal(fixture),
                credentials=manager,
                deployment_token=DEPLOYMENT_TOKEN,
                secrets=store,
                managed_bootstrap_dsn=_dsn,
            )
        assert manager.rotate_due() == []
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            assert admin.execute(
                f"SELECT max(credential_generation) FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s",
                (fixture.agent_run_id,),
            ).fetchone() == (1,)
    finally:
        manager.revoke(fixture.agent_run_id, reason="test-cleanup")
        manager.close()
        store.db.close()


@pytest.mark.parametrize("binding_shape", ["current", "draining_pair"])
def test_force_revoke_sql_alone_cuts_live_access(
    authorization_fixture: AuthorizationFixture, tmp_path: Path, binding_shape: str
) -> None:
    fixture = authorization_fixture
    execution_id = fixture.agent_run_id
    manager = _manager(fixture, tmp_path)
    store = _secret_store(fixture)
    credential = _launch_recovery_run(fixture, manager)
    old_dsns = [_dsn(credential.bootstrap_path)]
    manager.bind_launch_grant_rewriter(_rewrite_recovery_grant)
    expiry = datetime.now(UTC) + timedelta(minutes=30)
    try:
        if binding_shape == "draining_pair":
            credential = manager.rotate(managed_execution_id=execution_id, expires_at=expiry)
            old_dsns.append(_dsn(credential.bootstrap_path))
            reusable = manager.reuse_current_during_drain(
                managed_execution_id=execution_id,
                owner_kind="agent_run",
                session_id=fixture.session_id,
                project_id=fixture.project_id,
            )
            assert reusable is not None
            assert reusable.credential_generation == 2
        assert manager.get_live_binding_generation(execution_id) == credential.credential_generation
        grant_path = credential.bootstrap_path.parent / "grant.json"
        original_grant = grant_path.read_bytes()
        with psycopg.connect(old_dsns[-1], autocommit=True) as active:
            assert active.execute("SELECT 1").fetchone() == (1,)
            with psycopg.connect(fixture.database_url, autocommit=True) as admin:
                # No Python revocation follows this call, as if the CLI exited
                # immediately after the database transaction committed.
                _as_runtime(
                    admin, f"SELECT {AUTH_SCHEMA}.force_revoke_principal(%s)", (execution_id,)
                )
                assert admin.execute(
                    "SELECT bool_and(pb.revoked_at IS NOT NULL OR "
                    "(pb.revocation_requested_at IS NOT NULL AND NOT COALESCE(r.rolcanlogin, FALSE))) "
                    f"FROM {AUTH_SCHEMA}.principal_bindings pb "
                    "LEFT JOIN pg_roles r ON r.rolname = pb.role_name "
                    "WHERE pb.managed_execution_id = %s",
                    (execution_id,),
                ).fetchone() == (True,)
            with pytest.raises(psycopg.OperationalError):
                active.execute("SELECT 1")
        for old_dsn in old_dsns:
            with pytest.raises(psycopg.OperationalError):
                psycopg.connect(old_dsn, autocommit=True).close()
        manager.close()
        manager = _manager(fixture, tmp_path)
        manager.bind_launch_grant_rewriter(_rewrite_recovery_grant)
        assert manager.get_live_binding_generation(execution_id) is None
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                _as_runtime(
                    admin,
                    f"SELECT * FROM {AUTH_SCHEMA}.rotate_principal(%s, %s, %s)",
                    (execution_id, expiry, "test-password"),
                )
        with pytest.raises(CredentialAuthorizationError):
            manager.issue(
                managed_execution_id=execution_id,
                owner_kind="agent_run",
                session_id=fixture.session_id,
                agent_run_id=execution_id,
                expires_at=expiry,
            )
        with pytest.raises(HandshakeRejection):
            issue_grant_postgres(
                _principal(fixture),
                credentials=manager,
                deployment_token=DEPLOYMENT_TOKEN,
                secrets=store,
                managed_bootstrap_dsn=_dsn,
            )
        with pytest.raises(CredentialAuthorizationError):
            manager.rotate(managed_execution_id=execution_id, expires_at=expiry)
        assert (
            manager.reuse_current_during_drain(
                managed_execution_id=execution_id,
                owner_kind="agent_run",
                session_id=fixture.session_id,
                project_id=fixture.project_id,
            )
            is None
        )
        assert manager.rotate_due() == []
        manager.reconcile()
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            assert admin.execute(
                f"SELECT max(credential_generation), bool_and(revoked_at IS NOT NULL) "
                f"FROM {AUTH_SCHEMA}.principal_bindings WHERE managed_execution_id = %s",
                (execution_id,),
            ).fetchone() == (credential.credential_generation, True)
        assert grant_path.read_bytes() == original_grant
        assert manager.release_forced_revocation(execution_id)
        direct = issue_grant_postgres(
            _principal(fixture),
            credentials=manager,
            deployment_token=DEPLOYMENT_TOKEN,
            secrets=store,
            managed_bootstrap_dsn=_dsn,
        )
        assert direct.credential_generation == credential.credential_generation + 1
        with psycopg.connect(direct.dsn, autocommit=True) as agent:
            assert agent.execute("SELECT current_user").fetchone() == (direct.role_name,)
    finally:
        manager.revoke(execution_id, reason="test-cleanup")
        manager.close()
        store.db.close()


def test_forced_retry_cuts_every_generation_before_shared_early_return(
    authorization_fixture: AuthorizationFixture, tmp_path: Path
) -> None:
    fixture = authorization_fixture
    execution_id = fixture.agent_run_id
    manager = _manager(fixture, tmp_path)
    predecessor = _launch_recovery_run(fixture, manager)
    predecessor_dsn = _dsn(predecessor.bootstrap_path)
    manager.bind_launch_grant_rewriter(_rewrite_recovery_grant)
    current = manager.rotate(
        managed_execution_id=execution_id,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    current_dsn = _dsn(current.bootstrap_path)
    try:
        with (
            psycopg.connect(predecessor_dsn, autocommit=True) as held,
            psycopg.connect(current_dsn, autocommit=True) as active,
            psycopg.connect(fixture.database_url, autocommit=True) as admin,
        ):
            held_pid = held.info.backend_pid
            current_pid = active.info.backend_pid
            originals: list[str] = []
            try:
                # Inject a timeout only for the real predecessor backend in both
                # SQL functions. Other backends must still terminate normally.
                for signature in ("force_revoke_principal(uuid)", "revoke_principal(uuid,integer)"):
                    row = admin.execute(
                        "SELECT pg_get_functiondef(%s::regprocedure)",
                        (f"{AUTH_SCHEMA}.{signature}",),
                    ).fetchone()
                    assert row is not None
                    definition = str(row[0])
                    originals.append(definition)
                    injected = definition.replace(
                        "pg_terminate_backend(pid, 5000)",
                        f"CASE WHEN pid = {held_pid} THEN FALSE "
                        "ELSE pg_terminate_backend(pid, 5000) END",
                    )
                    admin.execute(injected)
                assert (
                    _as_runtime(
                        admin,
                        f"SELECT {AUTH_SCHEMA}.force_revoke_principal(%s)",
                        (execution_id,),
                    )
                    == -1
                )
                assert held.execute("SELECT 1").fetchone() == (1,)
                assert admin.execute(
                    "SELECT r.rolcanlogin FROM pg_roles r "
                    f"JOIN {AUTH_SCHEMA}.principal_bindings pb ON pb.role_name = r.rolname "
                    "WHERE pb.managed_execution_id = %s ORDER BY pb.credential_generation",
                    (execution_id,),
                ).fetchall() == [(False,), (False,)]
                for stale_dsn in (predecessor_dsn, current_dsn):
                    with pytest.raises(psycopg.OperationalError):
                        psycopg.connect(stale_dsn, autocommit=True).close()
                assert admin.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE pid = %s",
                    (current_pid,),
                ).fetchone() == (0,)
                with pytest.raises(psycopg.OperationalError):
                    active.execute("SELECT 1")
            finally:
                # Restore the isolated database before retry or fixture teardown.
                for definition in reversed(originals):
                    admin.execute(definition)
            # Terminate the formerly held backend, then let a fresh transaction
            # observe its departure before finishing the drop bookkeeping.
            _as_runtime(admin, f"SELECT {AUTH_SCHEMA}.force_revoke_principal(%s)", (execution_id,))
            assert admin.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (held_pid,)
            ).fetchone() == (0,)
            assert (
                _as_runtime(
                    admin,
                    f"SELECT {AUTH_SCHEMA}.force_revoke_principal(%s)",
                    (execution_id,),
                )
                >= 0
            )
            assert admin.execute(
                f"SELECT bool_and(revoked_at IS NOT NULL) FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s",
                (execution_id,),
            ).fetchone() == (True,)
    finally:
        manager.revoke(execution_id, reason="test-cleanup")
        manager.close()


def test_forced_retry_record_replays_force(
    authorization_fixture: AuthorizationFixture, tmp_path: Path
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    _launch_recovery_run(fixture, manager)
    try:
        with patch.object(
            manager._database,
            "fetchone",
            side_effect=psycopg.OperationalError("isolated forced-call failure"),
        ):
            outcome = manager.force_revoke(fixture.agent_run_id)
        assert not outcome.completed
        assert outcome.retry_recorded
        manager.close()
        manager = _manager(fixture, tmp_path)
        manager.reconcile()
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            assert admin.execute(
                f"SELECT EXISTS (SELECT 1 FROM {AUTH_SCHEMA}.forced_revocations "
                "WHERE managed_execution_id = %s)",
                (fixture.agent_run_id,),
            ).fetchone() == (True,)
            assert admin.execute(
                f"SELECT bool_and(revoked_at IS NOT NULL) FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s",
                (fixture.agent_run_id,),
            ).fetchone() == (True,)
    finally:
        manager.revoke(fixture.agent_run_id, reason="test-cleanup")
        manager.close()


def test_force_revoke_rejects_generation_bound_calls(
    authorization_fixture: AuthorizationFixture, tmp_path: Path
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    try:
        with pytest.raises(ValueError, match="generation"):
            manager.revoke(fixture.agent_run_id, generation=1, reason="operator-forced")
    finally:
        manager.close()


def test_forced_handshake_refusal_is_authorization_without_traceback(
    authorization_fixture: AuthorizationFixture, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    store = _secret_store(fixture)
    try:
        manager.force_revoke(fixture.agent_run_id)
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            for _ in range(2):
                with pytest.raises(HandshakeRejection) as rejected:
                    issue_grant_postgres(
                        _principal(fixture),
                        credentials=manager,
                        deployment_token=DEPLOYMENT_TOKEN,
                        secrets=store,
                        managed_bootstrap_dsn=_dsn,
                    )
                assert rejected.value.code == "claims_mismatch"
        assert not any(record.exc_info for record in caplog.records)
    finally:
        manager.close()
        store.db.close()


def test_force_revoke_before_first_binding_blocks_issue(
    authorization_fixture: AuthorizationFixture, tmp_path: Path
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    try:
        assert manager.force_revoke(fixture.agent_run_id).completed
        with pytest.raises(CredentialAuthorizationError):
            manager.issue(
                managed_execution_id=fixture.agent_run_id,
                owner_kind="agent_run",
                session_id=fixture.session_id,
                agent_run_id=fixture.agent_run_id,
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            assert admin.execute(
                f"SELECT count(*) FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s",
                (fixture.agent_run_id,),
            ).fetchone() == (0,)
    finally:
        manager.revoke(fixture.agent_run_id, reason="test-cleanup")
        manager.close()


@pytest.mark.parametrize("recovery", ["handshake", "sweep"])
def test_incomplete_revocation_recovers_live_run(
    authorization_fixture: AuthorizationFixture, tmp_path: Path, recovery: str
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    store = _secret_store(fixture)
    try:
        first = _launch_recovery_run(fixture, manager)
        manager.bind_launch_grant_rewriter(_rewrite_recovery_grant)
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(
                f"UPDATE {AUTH_SCHEMA}.principal_bindings SET revocation_requested_at = NOW() "
                "WHERE managed_execution_id = %s",
                (fixture.agent_run_id,),
            )
        if recovery == "handshake":
            direct = issue_grant_postgres(
                _principal(fixture),
                credentials=manager,
                deployment_token=DEPLOYMENT_TOKEN,
                secrets=store,
                managed_bootstrap_dsn=_dsn,
            )
            assert direct.credential_generation == first.credential_generation + 1
        else:
            recovered = manager.rotate_due()
            assert [item.credential_generation for item in recovered] == [2]
        with psycopg.connect(_dsn(first.bootstrap_path), autocommit=True) as agent:
            assert agent.execute("SELECT current_user").fetchone() == (
                f"gobby_agent_{fixture.agent_run_id.hex}_2",
            )
    finally:
        manager.revoke(fixture.agent_run_id, reason="test-cleanup")
        manager.close()
        store.db.close()


def test_recovery_sweep_continues_when_handshake_wins_issuance_race(
    authorization_fixture: AuthorizationFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = authorization_fixture
    other = replace(
        fixture, session_id=fixture.other_session_id, agent_run_id=fixture.other_agent_run_id
    )
    manager = _manager(fixture, tmp_path)
    try:
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(
                "UPDATE sessions SET machine_id = %s WHERE id = %s",
                (fixture.machine_id, other.session_id),
            )
            admin.execute(
                "UPDATE agent_runs SET machine_id = %s WHERE id = %s",
                (fixture.machine_id, other.agent_run_id),
            )
        for owner in (fixture, other):
            _launch_recovery_run(owner, manager)
            with psycopg.connect(fixture.database_url, autocommit=True) as admin:
                admin.execute(
                    f"SELECT {AUTH_SCHEMA}.revoke_principal(%s, NULL)", (owner.agent_run_id,)
                )
        manager.bind_launch_grant_rewriter(_rewrite_recovery_grant)
        original_issue = manager.issue
        handshake_winner: list[UUID] = []

        def racing_issue(
            *,
            managed_execution_id: UUID,
            owner_kind: OwnerKind,
            session_id: UUID,
            agent_run_id: UUID | None,
            expires_at: datetime,
            requested_project_path: str | None = None,
        ) -> ManagedCredential:
            if not handshake_winner:
                original_issue(
                    managed_execution_id=managed_execution_id,
                    owner_kind=owner_kind,
                    session_id=session_id,
                    agent_run_id=agent_run_id,
                    expires_at=expires_at,
                    requested_project_path=requested_project_path,
                )
                handshake_winner.append(managed_execution_id)
            return original_issue(
                managed_execution_id=managed_execution_id,
                owner_kind=owner_kind,
                session_id=session_id,
                agent_run_id=agent_run_id,
                expires_at=expires_at,
                requested_project_path=requested_project_path,
            )

        monkeypatch.setattr(manager, "issue", racing_issue)
        recovered = manager.rotate_due()
        assert len(recovered) == 1
        assert {item.managed_execution_id for item in recovered} == (
            {fixture.agent_run_id, other.agent_run_id} - set(handshake_winner)
        )
        assert recovered[0].credential_generation == 2
    finally:
        for owner in (fixture, other):
            manager.revoke(owner.agent_run_id, reason="test-cleanup")
        manager.close()
