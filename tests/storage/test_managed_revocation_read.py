"""Managed revocation is read from durable binding state, not process memory."""

import functools
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import psycopg
import pytest

from gobby.runner_init.servers import _grant_principal_revoked
from gobby.runtime_grants import GrantBundle, GrantRevocationStore
from gobby.runtime_grants.schema import GrantPrincipal, PostgresDirect
from gobby.storage.managed_credentials import ManagedCredentialManager
from tests.runtime_grants.test_revocation import _service
from tests.storage.test_managed_credentials import _manager
from tests.storage.test_postgres_agent_authorization import AUTH_SCHEMA, AuthorizationFixture


class _SpyCredentials:
    def __init__(self, *, answer: bool) -> None:
        self.answer = answer
        self.interactive_calls: list[dict[str, object]] = []
        self.managed_calls: list[dict[str, object]] = []

    def interactive_generation_revoked(
        self,
        *,
        deployment_token: str,
        project_id: UUID,
        generation: int,
    ) -> bool:
        self.interactive_calls.append(
            {
                "deployment_token": deployment_token,
                "project_id": project_id,
                "generation": generation,
            }
        )
        return self.answer

    def managed_generation_revoked(self, *, execution_id: UUID, generation: int) -> bool:
        self.managed_calls.append({"execution_id": execution_id, "generation": generation})
        return self.answer


def _grant(
    *,
    kind: str,
    generation: int,
    execution_id: UUID | None = None,
    project_id: UUID | None = None,
) -> GrantBundle:
    now = 1_700_000_000
    return _service().issue(
        principal=GrantPrincipal(
            kind=kind,
            machine_id="machine-1",
            project_id=str(project_id or uuid4()),
            execution_id=None if execution_id is None else str(execution_id),
            session_id="session-1",
        ),
        postgres=PostgresDirect(
            mode="direct",
            dsn="postgresql://role:secret@127.0.0.1:5432/gobby",
            role_name=f"gobby_{kind}_{generation}",
            credential_generation=generation,
            valid_until=now + 3_600,
        ),
        now=now,
        ttl_seconds=3_600,
    )


@pytest.mark.integration
def test_managed_generation_revoked_follows_revoked_at(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    execution = uuid4()
    try:
        live = manager.issue(
            managed_execution_id=execution,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=59),
        )
        assert live.credential_generation == 1
        assert manager.managed_generation_revoked(execution_id=execution, generation=1) is False

        successor = manager.rotate(
            managed_execution_id=execution,
            expires_at=datetime.now(UTC) + timedelta(minutes=59),
        )
        assert successor.credential_generation == 2
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            draining = admin.execute(
                f"SELECT revocation_requested_at IS NOT NULL, revoked_at IS NULL "
                f"FROM {AUTH_SCHEMA}.principal_bindings "
                "WHERE managed_execution_id = %s AND credential_generation = 1",
                (execution,),
            ).fetchone()
        assert draining == (True, True)

        assert manager.managed_generation_revoked(execution_id=execution, generation=1) is False
        assert manager.managed_generation_revoked(execution_id=execution, generation=2) is False

        assert manager.revoke(execution, reason="security-revoke").completed
        assert manager.managed_generation_revoked(execution_id=execution, generation=2) is True
        assert manager.managed_generation_revoked(execution_id=execution, generation=99) is True
    finally:
        manager.revoke(execution, reason="test-cleanup")
        manager.close()


@pytest.mark.integration
def test_principal_callback_reads_managed_revocation_from_the_database(
    authorization_fixture: AuthorizationFixture,
    tmp_path: Path,
) -> None:
    fixture = authorization_fixture
    manager = _manager(fixture, tmp_path)
    execution = uuid4()
    in_memory = GrantRevocationStore()
    manager.bind_grant_revocations(in_memory)
    try:
        live = manager.issue(
            managed_execution_id=execution,
            owner_kind="agent_run",
            session_id=fixture.session_id,
            agent_run_id=fixture.agent_run_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=59),
        )
        grant = _grant(
            kind="agent_run",
            generation=live.credential_generation,
            execution_id=execution,
        )
        assert _grant_principal_revoked(manager, grant) is False
        assert in_memory.is_revoked(grant) is False

        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            admin.execute(
                f"UPDATE {AUTH_SCHEMA}.principal_bindings SET revoked_at = NOW() "
                "WHERE managed_execution_id = %s AND credential_generation = %s",
                (execution, live.credential_generation),
            )

        assert _grant_principal_revoked(manager, grant) is True
        assert in_memory.is_revoked(grant) is False
        durable_reader = GrantRevocationStore(
            principal_revoked=functools.partial(_grant_principal_revoked, manager),
        )
        assert durable_reader.is_revoked(grant) is True
    finally:
        manager.revoke(execution, reason="test-cleanup")
        manager.close()


@pytest.mark.unit
@pytest.mark.parametrize("answer", [True, False])
def test_interactive_principal_answer_is_unchanged(answer: bool) -> None:
    project_id = uuid4()
    grant = _grant(kind="interactive", generation=3, project_id=project_id)
    spy = _SpyCredentials(answer=answer)

    assert _grant_principal_revoked(cast(ManagedCredentialManager, spy), grant) is answer
    assert spy.interactive_calls == [
        {
            "deployment_token": grant.deployment.token,
            "project_id": project_id,
            "generation": 3,
        }
    ]
    assert spy.managed_calls == []


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["agent_run", "tool_chat", "maintenance"])
def test_managed_principal_answer_comes_from_the_managed_read(kind: str) -> None:
    execution = uuid4()
    grant = _grant(kind=kind, generation=2, execution_id=execution)
    spy = _SpyCredentials(answer=True)

    assert _grant_principal_revoked(cast(ManagedCredentialManager, spy), grant) is True
    assert spy.managed_calls == [{"execution_id": execution, "generation": 2}]
    assert spy.interactive_calls == []


@pytest.mark.integration
def test_managed_principal_revoked_privileges_are_pinned(
    authorization_fixture: AuthorizationFixture,
) -> None:
    with psycopg.connect(authorization_fixture.database_url, autocommit=True) as admin:
        row = admin.execute(
            "SELECT pg_get_userbyid(p.proowner), p.prosecdef, "
            "EXISTS (SELECT 1 FROM aclexplode(p.proacl) acl "
            "WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'), "
            "has_function_privilege('gobby_daemon_runtime', p.oid, 'EXECUTE') "
            "FROM pg_proc p WHERE p.oid = to_regprocedure(%s)",
            (f"{AUTH_SCHEMA}.managed_principal_revoked(uuid,integer)",),
        ).fetchone()
    assert row is not None
    owner, security_definer, public_execute, runtime_execute = row
    assert owner == "gobby_agent_issuer"
    assert security_definer is True
    assert public_execute is False
    assert runtime_execute is True
