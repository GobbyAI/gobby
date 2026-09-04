"""Handshake Postgres issuance fail-closed helpers."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import psycopg
import pytest

from gobby.runner_init.servers import issue_grant_postgres
from gobby.runtime_grants.handshake import HandshakeRejection, HandshakeService
from gobby.runtime_grants.schema import GrantPrincipal, PostgresDirect
from gobby.runtime_grants.service import DeploymentGrantContext, GrantService
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.managed_credential_types import SecretStore
from gobby.storage.managed_credentials import ManagedCredentialManager, RevocationOutcome
from gobby.utils.local_token import (
    AgentApiTokenClaims,
    issue_agent_api_token,
    issue_tool_api_token,
    verify_agent_api_token,
)
from tests.runtime_grants.support import (
    DEPLOYMENT_TOKEN,
    FENCING_EPOCH,
    GOLDEN_SECRET,
    StaticRuntime,
    config_snapshot,
    daemon_config,
)
from tests.storage.test_postgres_agent_authorization import (
    AUTH_SCHEMA,
    RUNTIME_ROLE,
    AuthorizationFixture,
    authorization_fixture,
)

__all__ = ["authorization_fixture"]

ManagedKind = Literal["agent_run", "tool_chat"]
_OPERATOR_TOKEN = "operator-token"


def _interactive(*, session_id: str | None) -> GrantPrincipal:
    return GrantPrincipal(
        kind="interactive",
        machine_id="machine-1",
        project_id=str(uuid4()),
        execution_id=None,
        session_id=session_id,
    )


def _issue(principal: GrantPrincipal, credentials: MagicMock) -> PostgresDirect:
    return issue_grant_postgres(
        principal,
        credentials=cast("ManagedCredentialManager", credentials),
        deployment_token="token",
        secrets=cast(SecretStore, object()),
        managed_bootstrap_dsn=lambda _path: "x",
    )


def _configure_interactive_issue(credentials: MagicMock) -> None:
    issued = credentials.issue_interactive.return_value
    issued.dsn = "postgresql://scoped"
    issued.role_name = "gobby_ix_test_2"
    issued.credential_generation = 2
    issued.expires_at.timestamp.return_value = 1_700_003_600


def _manager(
    fixture: AuthorizationFixture,
    runtime_root: Path,
) -> ManagedCredentialManager:
    database = PostgresHubDatabase(fixture.database_url, runtime_role=RUNTIME_ROLE)
    database.open()
    return ManagedCredentialManager(
        database=database,
        machine_id=fixture.machine_id,
        runtime_root=runtime_root,
        owns_database=True,
    )


def _claims(
    kind: ManagedKind,
    fixture: AuthorizationFixture,
    execution_id: UUID,
) -> AgentApiTokenClaims:
    if kind == "agent_run":
        token = issue_agent_api_token(
            _OPERATOR_TOKEN,
            agent_run_id=str(execution_id),
            session_id=str(fixture.session_id),
            project_id=str(fixture.project_id),
            machine_id=str(fixture.machine_id),
            timeout_seconds=7200,
        )
    else:
        token = issue_tool_api_token(
            _OPERATOR_TOKEN,
            managed_execution_id=str(execution_id),
            session_id=str(fixture.session_id),
            project_id=str(fixture.project_id),
            machine_id=str(fixture.machine_id),
            timeout_seconds=7200,
        )
    claims = verify_agent_api_token(token, _OPERATOR_TOKEN)
    assert claims is not None
    return claims


def _handshake(
    fixture: AuthorizationFixture,
    manager: ManagedCredentialManager,
) -> HandshakeService:
    def issue_postgres(principal: GrantPrincipal) -> PostgresDirect:
        return issue_grant_postgres(
            principal,
            credentials=manager,
            deployment_token="unused-managed-token",
            secrets=cast(SecretStore, object()),
            managed_bootstrap_dsn=str,
        )

    grants = GrantService(
        runtime=StaticRuntime(config_snapshot(daemon_config(), revision=3)),
        context=DeploymentGrantContext(
            token=DEPLOYMENT_TOKEN,
            fencing_epoch=FENCING_EPOCH,
            signing_secret=GOLDEN_SECRET,
        ),
        clock=lambda: int(time.time()),
    )
    return HandshakeService(
        grants=grants,
        local_machine_id=str(fixture.machine_id),
        operator_token=_OPERATOR_TOKEN,
        issue_postgres=issue_postgres,
        admitted_projects=frozenset({str(fixture.project_id)}),
        clock=lambda: int(time.time()),
    )


def _execution_id(kind: ManagedKind, fixture: AuthorizationFixture) -> UUID:
    return fixture.agent_run_id if kind == "agent_run" else uuid4()


def _issue_initial_binding(
    manager: ManagedCredentialManager,
    fixture: AuthorizationFixture,
    kind: ManagedKind,
    execution_id: UUID,
) -> int:
    credential = manager.issue(
        managed_execution_id=execution_id,
        owner_kind=kind,
        session_id=fixture.session_id,
        agent_run_id=fixture.agent_run_id if kind == "agent_run" else None,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    return credential.credential_generation


def _cleanup_managed_execution(
    manager: ManagedCredentialManager,
    fixture: AuthorizationFixture,
    execution_id: UUID,
) -> None:
    manager.revoke(execution_id, reason="test-cleanup")
    with psycopg.connect(fixture.database_url, autocommit=True) as admin:
        admin.execute(
            f"DELETE FROM {AUTH_SCHEMA}.principal_audit_events WHERE managed_execution_id = %s",
            (execution_id,),
        )
        admin.execute(
            f"DELETE FROM {AUTH_SCHEMA}.principal_bindings WHERE managed_execution_id = %s",
            (execution_id,),
        )


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["agent_run", "tool_chat"])
def test_managed_refresh_rotates_live_binding(
    kind: ManagedKind,
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = authorization_fixture
    execution_id = _execution_id(kind, fixture)
    runtime_root = tmp_path / kind
    manager = _manager(fixture, runtime_root)
    try:
        predecessor_generation = _issue_initial_binding(manager, fixture, kind, execution_id)
        revocations: list[tuple[UUID, int | None, str]] = []
        revoke = manager.revoke

        def record_revoke(
            managed_execution_id: UUID,
            *,
            generation: int | None = None,
            reason: str,
        ) -> RevocationOutcome:
            revocations.append((managed_execution_id, generation, reason))
            return revoke(managed_execution_id, generation=generation, reason=reason)

        monkeypatch.setattr(manager, "revoke", record_revoke)
        grant = _handshake(fixture, manager).issue_for_agent(
            _claims(kind, fixture, execution_id),
            machine_id=str(fixture.machine_id),
            project_id=str(fixture.project_id),
        )

        postgres = grant.capabilities.postgres
        assert isinstance(postgres, PostgresDirect)
        assert postgres.credential_generation > predecessor_generation
        bootstrap = json.loads((runtime_root / str(execution_id) / "bootstrap.json").read_text())
        assert bootstrap["credential_generation"] == postgres.credential_generation
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            bindings = admin.execute(
                f"SELECT credential_generation, revoked_at IS NOT NULL "
                f"FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s ORDER BY credential_generation",
                (execution_id,),
            ).fetchall()
        assert bindings == [
            (predecessor_generation, True),
            (postgres.credential_generation, False),
        ]
        assert (
            execution_id,
            predecessor_generation,
            "rotation-predecessor",
        ) in revocations
    finally:
        _cleanup_managed_execution(manager, fixture, execution_id)
        manager.close()


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["agent_run", "tool_chat"])
def test_managed_issue_without_binding_unchanged(
    kind: ManagedKind,
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    execution_id = _execution_id(kind, fixture)
    runtime_root = tmp_path / kind
    manager = _manager(fixture, runtime_root)
    try:
        grant = _handshake(fixture, manager).issue_for_agent(
            _claims(kind, fixture, execution_id),
            machine_id=str(fixture.machine_id),
            project_id=str(fixture.project_id),
        )

        postgres = grant.capabilities.postgres
        assert isinstance(postgres, PostgresDirect)
        assert postgres.credential_generation == 1
        bootstrap = json.loads((runtime_root / str(execution_id) / "bootstrap.json").read_text())
        assert bootstrap["credential_generation"] == 1
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            bindings = admin.execute(
                f"SELECT credential_generation, revoked_at IS NOT NULL "
                f"FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s",
                (execution_id,),
            ).fetchall()
        assert bindings == [(1, False)]
    finally:
        _cleanup_managed_execution(manager, fixture, execution_id)
        manager.close()


@pytest.mark.integration
@pytest.mark.parametrize("state", ["revoked", "expired"])
def test_managed_refresh_rejects_dead_binding(
    state: str,
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    execution_id = fixture.agent_run_id
    runtime_root = tmp_path / state
    manager = _manager(fixture, runtime_root)
    try:
        generation = _issue_initial_binding(manager, fixture, "agent_run", execution_id)
        if state == "revoked":
            manager.revoke(execution_id, generation=generation, reason="test-seed")
        else:
            with psycopg.connect(fixture.database_url, autocommit=True) as admin:
                admin.execute(
                    f"UPDATE {AUTH_SCHEMA}.principal_bindings "
                    "SET issued_at = NOW() - INTERVAL '1 hour', "
                    "expires_at = NOW() - INTERVAL '1 second' "
                    "WHERE managed_execution_id = %s",
                    (execution_id,),
                )
        bootstrap_path = runtime_root / str(execution_id) / "bootstrap.json"
        bootstrap_before = bootstrap_path.read_bytes() if bootstrap_path.exists() else None
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            bindings_before = admin.execute(
                f"SELECT credential_generation, role_name::text, revoked_at "
                f"FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s ORDER BY credential_generation",
                (execution_id,),
            ).fetchall()
            roles_before = admin.execute(
                "SELECT rolname FROM pg_roles WHERE rolname LIKE %s ORDER BY rolname",
                (f"gobby_agent_{execution_id.hex}_%",),
            ).fetchall()

        with pytest.raises(HandshakeRejection) as rejected:
            _handshake(fixture, manager).issue_for_agent(
                _claims("agent_run", fixture, execution_id),
                machine_id=str(fixture.machine_id),
                project_id=str(fixture.project_id),
            )

        assert rejected.value.code == "claims_mismatch"
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            bindings_after = admin.execute(
                f"SELECT credential_generation, role_name::text, revoked_at "
                f"FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s ORDER BY credential_generation",
                (execution_id,),
            ).fetchall()
            roles_after = admin.execute(
                "SELECT rolname FROM pg_roles WHERE rolname LIKE %s ORDER BY rolname",
                (f"gobby_agent_{execution_id.hex}_%",),
            ).fetchall()
        assert bindings_after == bindings_before
        assert roles_after == roles_before
        assert (
            bootstrap_path.read_bytes() if bootstrap_path.exists() else None
        ) == bootstrap_before
    finally:
        _cleanup_managed_execution(manager, fixture, execution_id)
        manager.close()


@pytest.mark.unit
def test_interactive_missing_session_id_issues_with_null_session() -> None:
    """A sessionless interactive caller issues with session_id None, never a fake id (#20899)."""
    credentials = MagicMock()
    _configure_interactive_issue(credentials)

    issued = _issue(_interactive(session_id=None), credentials)

    assert credentials.issue_interactive.call_args.kwargs["session_id"] is None
    assert issued.role_name == "gobby_ix_test_2"


@pytest.mark.unit
def test_issuance_errors_are_generic() -> None:
    credentials = MagicMock()
    credentials.issue_interactive.side_effect = RuntimeError("dsn=postgres://secret")
    with pytest.raises(HandshakeRejection) as rejected:
        _issue(_interactive(session_id=str(uuid4())), credentials)
    assert rejected.value.code == "credential_issuance_failed"
    assert rejected.value.message == "credential issuance failed"
    assert "secret" not in rejected.value.message
    assert "postgres://" not in rejected.value.message


@pytest.mark.unit
def test_issuance_failure_log_attributes_principal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    execution_id = str(uuid4())
    session_id = str(uuid4())
    principal = GrantPrincipal(
        kind="agent_run",
        machine_id="machine-1",
        project_id=str(uuid4()),
        execution_id=execution_id,
        session_id=session_id,
    )
    credentials = MagicMock()
    credentials.get_live_binding_generation.return_value = None
    credentials.issue.side_effect = RuntimeError("issuance unavailable")

    with (
        caplog.at_level(logging.ERROR, logger="gobby.runner_init.servers"),
        pytest.raises(HandshakeRejection, match="credential issuance failed"),
    ):
        _issue(principal, credentials)

    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "grant credential issuance failed"
    )
    assert record.__dict__["kind"] == "agent_run"
    assert record.__dict__["execution_id"] == execution_id
    assert record.__dict__["session_id"] == session_id


@pytest.mark.unit
def test_interactive_overlay_is_passed_to_issuer() -> None:
    credentials = MagicMock()
    _configure_interactive_issue(credentials)
    overlay = uuid4()
    principal = _interactive(session_id=str(uuid4())).model_copy(
        update={"code_overlay_project_id": str(overlay)}
    )
    _issue(principal, credentials)
    assert credentials.issue_interactive.call_args.kwargs["code_overlay_project_id"] == overlay


@pytest.mark.unit
def test_unregistered_overlay_is_claims_mismatch() -> None:
    from gobby.storage.managed_credentials import CredentialAuthorizationError

    credentials = MagicMock()
    credentials.issue_interactive.side_effect = CredentialAuthorizationError(
        "interactive overlay is not a registered isolation workspace of the project"
    )
    principal = _interactive(session_id=str(uuid4())).model_copy(
        update={"code_overlay_project_id": str(uuid4())}
    )
    with pytest.raises(HandshakeRejection) as rejected:
        _issue(principal, credentials)
    assert rejected.value.code == "claims_mismatch"
    assert "registered isolation workspace" in rejected.value.message
