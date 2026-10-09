"""Capability renewal stays bounded by the managed principal's live owner."""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from gobby.runtime_grants.launch import materialize_managed_launch, rewrite_managed_grant_file
from gobby.runtime_grants.schema import GrantPrincipal
from gobby.storage.hub.protocol import Row
from gobby.storage.managed_credential_types import CredentialIssuanceError
from gobby.storage.managed_credentials import ManagedCredential
from gobby.utils import local_token
from tests.runtime_grants.support import DEPLOYMENT_TOKEN
from tests.runtime_grants.test_launch import _grant
from tests.storage.test_managed_credentials import _manager
from tests.storage.test_postgres_agent_authorization import AUTH_SCHEMA, AuthorizationFixture

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("owner_state", ["live", "expired", "ended_run"])
def test_renewal_is_gated_by_live_owner(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner_state: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    execution = uuid4()
    now = int(datetime.now(UTC).timestamp())
    clock = now - 23 * 3600
    key = b"isolated-test-renewal-key"
    monkeypatch.setattr("gobby.utils.local_token.time.time", lambda: clock)
    monkeypatch.setattr(local_token, "read_managed_signing_key", lambda: key)
    try:
        initial = manager.issue(
            managed_execution_id=execution,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        grant = _grant().model_copy(
            update={
                "principal": GrantPrincipal(
                    kind="agent_run",
                    machine_id=str(fixture.machine_id),
                    project_id=str(fixture.project_id),
                    execution_id=str(execution),
                    session_id=str(fixture.session_id),
                )
            }
        )
        launch = materialize_managed_launch(
            grant, dest_dir=initial.bootstrap_path.parent, signing_key=key, deadline_seconds=86400
        )
        original_token = launch.env["GOBBY_AGENT_API_TOKEN"]
        clock = now

        def rewrite(credential: ManagedCredential, dsn: str) -> None:
            rewrite_managed_grant_file(
                launch.grant_path,
                managed_execution_id=str(execution),
                scoped_dsn=dsn,
                role_name=credential.role_name,
                credential_generation=credential.credential_generation,
                issued_at=credential.issued_at,
                expires_at=credential.expires_at,
                deployment_token=DEPLOYMENT_TOKEN,
                fencing_epoch=1,
                signing_secret="test-grant-key",
            )

        manager.bind_launch_grant_rewriter(rewrite)
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(
                f"UPDATE {AUTH_SCHEMA}.principal_bindings "
                "SET issued_at = NOW() - INTERVAL '46 minutes' WHERE managed_execution_id = %s",
                (execution,),
            )
            if owner_state == "expired":
                admin.execute(
                    "UPDATE sessions SET status = 'expired' WHERE id = %s", (fixture.session_id,)
                )
            elif owner_state == "ended_run":
                admin.execute(
                    "UPDATE agent_runs SET status = 'success' WHERE id = %s",
                    (fixture.agent_run_id,),
                )
        rotated = manager.rotate_due()
        current = json.loads(launch.grant_path.read_bytes())["managed_api_token"]
        clock = now + 2 * 3600
        assert local_token.verify_agent_api_token(original_token, key) is None
        if owner_state == "live":
            assert [item.managed_execution_id for item in rotated] == [execution]
            assert current != original_token
            assert local_token.verify_agent_api_token(current, key) is not None
        else:
            assert rotated == []
            assert current == original_token
            assert local_token.verify_agent_api_token(current, key) is None
            assert manager.get_live_binding_generation(execution) == initial.credential_generation
        assert current not in caplog.text and original_token not in caplog.text
    finally:
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(
                "UPDATE sessions SET status = 'active' WHERE id = %s", (fixture.session_id,)
            )
            admin.execute(
                "UPDATE agent_runs SET status = 'pending' WHERE id = %s", (fixture.agent_run_id,)
            )
        manager.revoke(execution, reason="test-cleanup")
        manager.close()


@pytest.mark.parametrize("owner_state", ["live", "expired_session", "ended_run"])
def test_live_owner_can_refresh_an_expired_postgres_binding(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    owner_state: str,
) -> None:
    """Postgres expiry must not turn an authenticated live seat into an ended owner."""
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    execution = uuid4()
    try:
        initial = manager.issue(
            managed_execution_id=execution,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(
                f"UPDATE {AUTH_SCHEMA}.principal_bindings "
                "SET issued_at = NOW() - INTERVAL '61 minutes', "
                "expires_at = NOW() - INTERVAL '1 minute' "
                "WHERE managed_execution_id = %s",
                (execution,),
            )
            if owner_state == "expired_session":
                admin.execute(
                    "UPDATE sessions SET status = 'expired' WHERE id = %s", (fixture.session_id,)
                )
            elif owner_state == "ended_run":
                admin.execute(
                    "UPDATE agent_runs SET status = 'success' WHERE id = %s",
                    (fixture.agent_run_id,),
                )
        if owner_state != "live":
            original_bootstrap = initial.bootstrap_path.read_bytes()
            with pytest.raises(CredentialIssuanceError, match="lost binding race"):
                manager.rotate(
                    managed_execution_id=execution,
                    expires_at=datetime.now(UTC) + timedelta(minutes=59),
                )
            assert initial.bootstrap_path.read_bytes() == original_bootstrap
            assert manager.get_live_binding_generation(execution) == initial.credential_generation
            return
        refreshed = manager.rotate(
            managed_execution_id=execution,
            expires_at=datetime.now(UTC) + timedelta(minutes=59),
        )
        assert refreshed.credential_generation == initial.credential_generation + 1
        assert manager.get_live_binding_generation(execution) == refreshed.credential_generation
        assert json.loads(refreshed.bootstrap_path.read_bytes())["credential_generation"] == 2
    finally:
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(
                "UPDATE sessions SET status = 'active' WHERE id = %s", (fixture.session_id,)
            )
            admin.execute(
                "UPDATE agent_runs SET status = 'pending' WHERE id = %s", (fixture.agent_run_id,)
            )
        manager.revoke(execution, reason="test-cleanup")
        manager.close()


def test_refresh_recovers_after_predecessor_drain_clears(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    execution = uuid4()
    expiry = datetime.now(UTC) + timedelta(minutes=59)
    try:
        initial = manager.issue(
            managed_execution_id=execution,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=expiry,
        )
        # Pause a competing rotation after its SQL successor exists, before it
        # publishes the envelope and revokes the predecessor. Refresh must wait.
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            successor = admin.execute(
                f"SELECT credential_generation FROM {AUTH_SCHEMA}.rotate_principal_if_generation"
                "(%s, %s, %s, %s)",
                (execution, initial.credential_generation, expiry, "isolated-drain-password"),
            ).fetchone()
        assert successor == (2,)
        predecessor_bootstrap = initial.bootstrap_path.read_bytes()
        with pytest.raises(CredentialIssuanceError, match="lost binding race"):
            manager.rotate(managed_execution_id=execution, expires_at=expiry)
        assert initial.bootstrap_path.read_bytes() == predecessor_bootstrap
        manager.revoke(
            execution, generation=initial.credential_generation, reason="rotation-predecessor"
        )
        refreshed = manager.rotate(managed_execution_id=execution, expires_at=expiry)
        assert refreshed.credential_generation == 3
        assert manager.get_live_binding_generation(execution) == 3
    finally:
        manager.revoke(execution, reason="test-cleanup")
        manager.close()


@pytest.mark.parametrize("binding_count", [0, 2])
def test_invalid_binding_count_refuses_without_retry_storm(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binding_count: int,
    caplog: pytest.LogCaptureFixture,
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
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        original = manager._database.fetchall
        predecessor_bootstrap = initial.bootstrap_path.read_bytes()

        def bindings(
            query: str,
            params: Sequence[Any] | Mapping[str, Any] = (),
        ) -> list[Row]:
            if "list_active_principals" in query:
                return [
                    {"managed_execution_id": execution, "expires_at": initial.expires_at}
                ] * binding_count
            return list(original(query, params))

        monkeypatch.setattr(manager._database, "fetchall", bindings)
        result: ManagedCredential | None = manager._rotate_if_generation(
            managed_execution_id=execution,
            predecessor_generation=initial.credential_generation,
            issued_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=59),
        )
        assert result is None
        assert initial.bootstrap_path.read_bytes() == predecessor_bootstrap
        assert "managed credential rotation failed" not in caplog.text.lower()
    finally:
        manager.revoke(execution, reason="test-cleanup")
        manager.close()
