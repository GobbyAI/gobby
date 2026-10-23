"""Recovery of live seats after principal revocation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click
import psycopg
import pytest

from gobby.cli.hub_backup._stores import drain_ephemeral_principals
from gobby.runner_init.servers import issue_grant_postgres
from gobby.runtime_grants.handshake import HandshakeRejection
from gobby.runtime_grants.launch import materialize_managed_launch, rewrite_managed_grant_file
from gobby.runtime_grants.schema import GrantBundle, GrantPrincipal, PostgresDirect
from gobby.storage.managed_credentials import ManagedCredential
from gobby.utils import local_token
from tests.runtime_grants.support import DEPLOYMENT_TOKEN, FENCING_EPOCH, GOLDEN_SECRET
from tests.storage.test_managed_credentials import _manager, _secret_store
from tests.storage.test_postgres_agent_authorization import AUTH_SCHEMA, AuthorizationFixture
from tests.storage.test_postgres_agent_authorization import (
    fresh_authorization_fixture as authorization_fixture,
)

pytestmark = pytest.mark.integration
__all__ = ["authorization_fixture"]


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


@pytest.mark.parametrize("revocation", ["explicit", "expired_restart"])
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
        if revocation == "explicit":
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


def test_backup_drain_refuses_live_runs_before_revoking_any_binding(
    authorization_fixture: AuthorizationFixture,
) -> None:
    fixture = authorization_fixture
    with psycopg.connect(fixture.database_url, autocommit=True) as admin:
        with pytest.raises(click.ClickException, match="live agent runs"):
            drain_ephemeral_principals(fixture.database_url)
        rows = admin.execute(
            f"SELECT revoked_at, revocation_requested_at FROM {AUTH_SCHEMA}.principal_bindings "
            "WHERE managed_execution_id IN (%s, %s)",
            (fixture.execution_id, fixture.other_execution_id),
        ).fetchall()
        assert rows == [(None, None), (None, None)]
