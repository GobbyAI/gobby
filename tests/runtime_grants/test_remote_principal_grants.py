"""Remote interactive principals receive Brokered datastores (code-index-family 1.6).

A principal whose machine differs from the hub's gets no direct Postgres
credential. Its grant carries Postgres Brokered with only the node_code_index
operation, and Falkor and Qdrant Brokered whatever the hub's config says.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from gobby.config.runtime_models import ConfigSnapshot
from gobby.runtime_grants import DeploymentGrantContext, GrantService
from gobby.runtime_grants.handshake import GRANT_TTL_SECONDS, HandshakeService
from gobby.runtime_grants.schema import (
    BrokeredCapability,
    BrokerOperation,
    GrantPrincipal,
    PostgresDirect,
)
from gobby.runtime_grants.service import EMBED_OPERATION, FALKOR_OPERATIONS, QDRANT_OPERATIONS
from gobby.storage.schema_contract import expected_schema_identity
from tests.runtime_grants.support import (
    DEPLOYMENT_TOKEN,
    FENCING_EPOCH,
    GOLDEN_SECRET,
    StaticRuntime,
    revision_snapshot,
)

HUB_MACHINE = "hub-machine"
REMOTE_MACHINE = "remote-machine"
PROJECT_ID = "project-1"
NOW = 1_700_000_000
NODE_OPERATION = BrokerOperation(name="node_code_index", method="POST", path="/api/code-index/node")
GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "remote_interactive_brokered.json"

# Loopback Falkor and Qdrant are Direct on the hub under today's rules.
LOOPBACK_SNAPSHOT = revision_snapshot(
    41,
    host="127.0.0.1",
    password=None,
    qdrant_url="http://127.0.0.1:6333",
    api_key=None,
)
# Non-loopback Falkor and Qdrant without secrets are Brokered under today's rules.
REMOTE_HOSTS_SNAPSHOT = revision_snapshot(
    41,
    host="falkor-a.test",
    password=None,
    qdrant_url="http://qdrant-a.test:6333",
    api_key=None,
)


@pytest.fixture(autouse=True)
def golden_schema_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the installed schema identity to the checked-in golden identity."""
    monkeypatch.setattr(
        "gobby.runtime_grants.service.installed_schema_identity", expected_schema_identity
    )


class PostgresSpy:
    """Stands in for the production issue_postgres and records each call."""

    def __init__(self, *, valid_until: int = NOW + 3_600) -> None:
        self.calls: list[GrantPrincipal] = []
        self._valid_until = valid_until

    def __call__(self, principal: GrantPrincipal) -> PostgresDirect:
        self.calls.append(principal)
        return PostgresDirect(
            mode="direct",
            dsn="postgresql://role:secret@127.0.0.1:5432/gobby",
            role_name="gobby_interactive_1",
            credential_generation=3,
            valid_until=self._valid_until,
        )


def _handshake(
    snapshot: ConfigSnapshot,
    issue_postgres: Callable[[GrantPrincipal], PostgresDirect],
) -> HandshakeService:
    grants = GrantService(
        runtime=StaticRuntime(snapshot),
        context=DeploymentGrantContext(
            token=DEPLOYMENT_TOKEN,
            fencing_epoch=FENCING_EPOCH,
            signing_secret=GOLDEN_SECRET,
        ),
    )
    return HandshakeService(
        grants=grants,
        local_machine_id=HUB_MACHINE,
        issue_postgres=issue_postgres,
        admitted_projects=frozenset({PROJECT_ID}),
        clock=lambda: NOW,
    )


@pytest.mark.unit
def test_remote_interactive_principal_gets_brokered_datastores() -> None:
    spy = PostgresSpy()
    handshake = _handshake(LOOPBACK_SNAPSHOT, spy)

    remote = handshake.issue_for_operator(
        machine_id=REMOTE_MACHINE,
        forwarded_machine_id=REMOTE_MACHINE,
        project_id=PROJECT_ID,
        session_id=None,
    )

    assert spy.calls == [], "a remote interactive principal must not mint a Postgres credential"
    assert remote.capabilities.postgres == BrokeredCapability(operations=(NODE_OPERATION,))
    assert remote.capabilities.falkordb == BrokeredCapability(operations=FALKOR_OPERATIONS)
    assert remote.capabilities.qdrant == BrokeredCapability(operations=QDRANT_OPERATIONS)
    assert remote.capabilities.broker_operations == (
        EMBED_OPERATION,
        *FALKOR_OPERATIONS,
        *QDRANT_OPERATIONS,
    )

    local = handshake.issue_for_operator(
        machine_id=HUB_MACHINE,
        forwarded_machine_id=HUB_MACHINE,
        project_id=PROJECT_ID,
        session_id=None,
    )

    assert len(spy.calls) == 1
    assert local.capabilities.postgres.mode == "direct"
    assert local.capabilities.falkordb.mode == "direct"
    assert local.capabilities.qdrant.mode == "direct"


@pytest.mark.unit
def test_brokered_grant_ttl_ignores_valid_until() -> None:
    handshake = _handshake(LOOPBACK_SNAPSHOT, PostgresSpy(valid_until=NOW + 10))

    grant = handshake.issue_for_operator(
        machine_id=REMOTE_MACHINE,
        forwarded_machine_id=REMOTE_MACHINE,
        project_id=PROJECT_ID,
        session_id=None,
    )

    assert grant.capabilities.postgres.mode == "brokered"
    assert grant.expires_at == NOW + GRANT_TTL_SECONDS


@pytest.mark.unit
def test_remote_issuance_reproduces_the_golden() -> None:
    handshake = _handshake(REMOTE_HOSTS_SNAPSHOT, PostgresSpy())

    grant = handshake.issue_for_operator(
        machine_id=REMOTE_MACHINE,
        forwarded_machine_id=REMOTE_MACHINE,
        project_id=PROJECT_ID,
        session_id=None,
    )

    assert grant.capabilities.postgres.mode == "brokered"
    assert GOLDEN_PATH.is_file(), f"missing golden vector {GOLDEN_PATH}"
    assert grant.model_dump_canonical() == GOLDEN_PATH.read_bytes()
