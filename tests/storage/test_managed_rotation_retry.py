"""A failed first handshake must not strand a live seat on its launch token."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest

from gobby.runner_init.servers import issue_grant_postgres
from gobby.runtime_grants.handshake import HandshakeRejection
from gobby.runtime_grants.launch import materialize_managed_launch
from gobby.runtime_grants.schema import GrantBundle, PostgresDirect
from gobby.storage.managed_credentials import ManagedCredential
from gobby.utils import local_token
from tests.runtime_grants.support import DEPLOYMENT_TOKEN
from tests.storage.test_managed_credential_recovery import (
    _dsn,
    _principal,
    _rewrite_recovery_grant,
)
from tests.storage.test_managed_credentials import _manager, _secret_store
from tests.storage.test_postgres_agent_authorization import AuthorizationFixture
from tests.storage.test_postgres_agent_authorization import (
    fresh_authorization_fixture as authorization_fixture,
)

pytestmark = pytest.mark.integration
__all__ = ["authorization_fixture"]


@pytest.mark.parametrize("failure", ["timeout", "same_execution_binding_race"])
def test_first_handshake_rotation_retries_and_renews_launch_token(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    store = _secret_store(fixture)
    signing_key = b"rotation-retry-test-key"
    monkeypatch.setattr(local_token, "read_managed_signing_key", lambda: signing_key)
    delays: list[float] = []
    # Observe backoff without making the regression test sleep.
    monkeypatch.setattr("time.sleep", delays.append)
    try:
        first = manager.issue(
            managed_execution_id=fixture.agent_run_id,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=59),
        )
        golden = Path(__file__).parents[1] / "runtime_grants/golden/direct_datastores.json"
        grant = GrantBundle.model_validate_json(golden.read_bytes()).model_copy(
            update={"principal": _principal(fixture)}
        )
        launch = materialize_managed_launch(
            grant,
            dest_dir=first.bootstrap_path.parent,
            signing_key=signing_key,
            deadline_seconds=3600,
        )
        original = local_token.read_managed_api_token(launch.env)
        assert original is not None
        original_claims = local_token.verify_agent_api_token(original, signing_key)
        assert isinstance(original_claims, local_token.AgentApiTokenClaims)
        manager.bind_launch_grant_rewriter(_rewrite_recovery_grant)
        rotate_once = manager._rotate_if_generation
        attempts = 0

        def transient_rotation(
            *,
            managed_execution_id: UUID,
            predecessor_generation: int,
            issued_at: datetime,
            expires_at: datetime,
        ) -> ManagedCredential | None:
            nonlocal attempts
            attempts += 1
            if attempts == 1 and failure == "timeout":
                raise TimeoutError("test: saturated credential pool")
            result = rotate_once(
                managed_execution_id=managed_execution_id,
                predecessor_generation=predecessor_generation,
                issued_at=issued_at,
                expires_at=expires_at,
            )
            # The competing request published the same execution's successor;
            # this request loses the generation CAS and must recover safely.
            if attempts == 1:
                assert result is not None
                return None
            return result

        direct: PostgresDirect | None = None
        rejection: str | None = None
        with patch.object(manager, "_rotate_if_generation", side_effect=transient_rotation):
            try:
                direct = issue_grant_postgres(
                    _principal(fixture),
                    credentials=manager,
                    deployment_token=DEPLOYMENT_TOKEN,
                    secrets=store,
                    managed_bootstrap_dsn=_dsn,
                )
            except HandshakeRejection as error:
                rejection = str(error)

        assert direct is not None, f"transient {failure} stranded the seat: {rejection}"
        assert failure != "timeout" or any(delay > 0 for delay in delays), (
            "transient pool failure must back off before retrying"
        )
        renewed = local_token.read_managed_api_token(launch.env)
        assert renewed is not None and renewed != original
        claims = local_token.verify_agent_api_token(renewed, signing_key)
        assert isinstance(claims, local_token.AgentApiTokenClaims)
        assert claims.iat < original_claims.exp
        assert claims.exp - claims.iat >= local_token.AGENT_TOKEN_MAX_TTL_SECONDS
        assert claims.agent_run_id == str(fixture.agent_run_id)
        rewritten = GrantBundle.model_validate_json(launch.grant_path.read_bytes())
        assert isinstance(rewritten.capabilities.postgres, PostgresDirect)
        assert rewritten.capabilities.postgres.dsn == direct.dsn
        assert rewritten.capabilities.postgres.credential_generation == direct.credential_generation
        assert direct.credential_generation == first.credential_generation + 1
    finally:
        manager.revoke(fixture.agent_run_id, reason="test-cleanup")
        manager.close()
        store.db.close()
