"""Shared grant revocation is visible to later GrantService instances."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from gobby.runtime_grants import (
    DeploymentGrantContext,
    GrantBundle,
    GrantRevocationStore,
    GrantService,
    RevokedGrant,
)
from gobby.runtime_grants.schema import GrantPrincipal, PostgresDirect
from gobby.storage.managed_credentials import ManagedCredentialManager
from tests.runtime_grants.support import (
    DEPLOYMENT_TOKEN,
    FENCING_EPOCH,
    GOLDEN_SECRET,
    StaticRuntime,
    revision_snapshot,
)

pytestmark = pytest.mark.unit


class _RevokeDatabase:
    def __init__(self, execution_id: UUID) -> None:
        self.execution_id = execution_id

    @property
    def conninfo(self) -> str:
        return "postgresql://gobby_test@127.0.0.1/gobby_test"

    def fetchone(self, sql: str, params: Any = ()) -> dict[str, object] | None:
        if "lookup_interactive_principal" in sql:
            return {"managed_execution_id": self.execution_id, "revoked_at": None}
        if "revoke_principal" in sql:
            return {"revoke_principal": 1}
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self, sql: str, params: Any = ()) -> list[dict[str, object]]:
        return []


def _service(revocations: GrantRevocationStore | None = None) -> GrantService:
    snapshot = revision_snapshot(
        41,
        host="falkor-a.test",
        password="falkor-secret-a",
        qdrant_url="http://qdrant-a.test:6333",
        api_key="qdrant-secret-a",
    )
    return GrantService(
        runtime=StaticRuntime(snapshot),
        context=DeploymentGrantContext(
            token=DEPLOYMENT_TOKEN,
            fencing_epoch=FENCING_EPOCH,
            signing_secret=GOLDEN_SECRET,
        ),
        revocations=revocations or GrantRevocationStore(),
    )


def _issue(
    service: GrantService,
    *,
    kind: str = "interactive",
    execution_id: str | None = None,
    project_id: str = "project-1",
    now: int = 1_700_000_000,
) -> GrantBundle:
    return service.issue(
        principal=GrantPrincipal(
            kind=kind,
            machine_id="machine-1",
            project_id=project_id,
            execution_id=execution_id,
            session_id="session-1",
        ),
        postgres=PostgresDirect(
            mode="direct",
            dsn="postgresql://role:secret@127.0.0.1:5432/gobby",
            role_name="gobby_interactive_1",
            credential_generation=3,
            valid_until=now + 3_600,
        ),
        now=now,
        ttl_seconds=3_600,
    )


def test_revocation_is_visible_to_a_later_grant_service() -> None:
    store = GrantRevocationStore()
    issuer = _service(store)
    grant = _issue(issuer)
    issuer.revoke(grant)

    presenter = _service(store)
    with pytest.raises(RevokedGrant) as captured:
        presenter.present(grant, now=grant.issued_at + 10)
    assert captured.value.code == "revoked"


def test_fresh_store_does_not_inherit_another_service_revoke() -> None:
    issuer = _service()
    grant = _issue(issuer)
    issuer.revoke(grant)

    with pytest.raises(RevokedGrant):
        issuer.present(grant, now=grant.issued_at + 10)
    later = _service()
    assert later.present(grant, now=grant.issued_at + 10) is grant


def test_interactive_principal_revoke_rejects_without_checksum() -> None:
    store = GrantRevocationStore()
    grant = _issue(_service(store))
    store.revoke_interactive(
        deployment_token=DEPLOYMENT_TOKEN,
        project_id="project-1",
        generation=3,
    )
    with pytest.raises(RevokedGrant):
        _service(store).present(grant, now=grant.issued_at + 10)


def test_execution_revoke_rejects_matching_agent_grant_only() -> None:
    store = GrantRevocationStore()
    execution = str(uuid4())
    other = str(uuid4())
    matching = _issue(_service(store), kind="agent_run", execution_id=execution)
    neighbor = _issue(_service(store), kind="agent_run", execution_id=other)
    store.revoke_execution(execution, 3)

    with pytest.raises(RevokedGrant):
        _service(store).present(matching, now=matching.issued_at + 10)
    assert _service(store).present(neighbor, now=neighbor.issued_at + 10) is neighbor


def test_durable_principal_callback_rejects_after_new_service() -> None:
    revoked = {"yes": False}

    def _principal_revoked(grant: GrantBundle) -> bool:
        return revoked["yes"]

    store = GrantRevocationStore(principal_revoked=_principal_revoked)
    grant = _issue(_service(store))
    assert _service(store).present(grant, now=grant.issued_at + 10) is grant
    revoked["yes"] = True
    with pytest.raises(RevokedGrant):
        _service(store).present(grant, now=grant.issued_at + 10)


def test_credential_revoke_is_the_production_revocation_path(tmp_path: Path) -> None:
    execution_id = uuid4()
    project_id = uuid4()
    store = GrantRevocationStore()
    manager = ManagedCredentialManager(
        database=cast(Any, _RevokeDatabase(execution_id)),
        machine_id=uuid4(),
        runtime_root=tmp_path,
    )
    manager.bind_grant_revocations(store)
    outcome = manager.revoke(execution_id, generation=3, reason="operator-forced")
    assert outcome.completed is True
    assert outcome.revoked_count == 1

    grant = _issue(_service(store), kind="agent_run", execution_id=str(execution_id))
    with pytest.raises(RevokedGrant):
        _service(store).present(grant, now=grant.issued_at + 10)

    interactive = manager.revoke_interactive(
        deployment_token=DEPLOYMENT_TOKEN,
        project_id=project_id,
        generation=3,
        reason="operator-forced",
    )
    assert interactive.completed is True
    presented = _issue(_service(store), project_id=str(project_id))
    with pytest.raises(RevokedGrant):
        _service(store).present(presented, now=presented.issued_at + 10)
