"""Issuance binds every capability and secret to one ConfigRuntime revision."""

from __future__ import annotations

import threading

import pytest

from gobby.runtime_grants import DeploymentGrantContext, GrantBundle, GrantService
from gobby.runtime_grants.schema import FalkorDirect, GrantPrincipal, PostgresDirect, QdrantDirect
from tests.runtime_grants.support import (
    DEPLOYMENT_TOKEN,
    FENCING_EPOCH,
    GOLDEN_SECRET,
    SnapshotAfterCaptureRuntime,
    SuccessiveCaptureRuntime,
    revision_snapshot,
)


def _postgres(now: int) -> PostgresDirect:
    return PostgresDirect(
        mode="direct",
        dsn="postgresql://role:secret@127.0.0.1:5432/gobby",
        role_name="gobby_interactive_1",
        credential_generation=3,
        valid_until=now + 3_600,
    )


def _principal() -> GrantPrincipal:
    return GrantPrincipal(
        kind="interactive",
        machine_id="machine-1",
        project_id="project-1",
        execution_id=None,
        session_id=None,
    )


def _issue(
    runtime: SnapshotAfterCaptureRuntime | SuccessiveCaptureRuntime, *, now: int = 1_700_000_000
) -> GrantBundle:
    service = GrantService(
        runtime=runtime,
        context=DeploymentGrantContext(
            token=DEPLOYMENT_TOKEN,
            fencing_epoch=FENCING_EPOCH,
            signing_secret=GOLDEN_SECRET,
        ),
    )
    return service.issue(
        principal=_principal(), postgres=_postgres(now), now=now, ttl_seconds=3_600
    )


@pytest.mark.unit
def test_single_revision_per_grant() -> None:
    first = revision_snapshot(
        41,
        host="falkor-a.test",
        password="falkor-secret-a",
        qdrant_url="http://qdrant-a.test:6333",
        api_key="qdrant-secret-a",
    )
    second = revision_snapshot(
        42,
        host="falkor-b.test",
        password=None,
        qdrant_url="http://qdrant-b.test:6333",
        api_key="qdrant-secret-b",
        failed_rotation=True,
        desired_password="falkor-secret-rotated",
    )

    successive = SuccessiveCaptureRuntime(first, second)
    after_capture = SnapshotAfterCaptureRuntime(first, second)

    barrier = threading.Barrier(2)
    swapped = SnapshotAfterCaptureRuntime(first, second)

    def activate() -> None:
        barrier.wait()
        swapped.publish_second()

    racer = threading.Thread(target=activate)
    racer.start()
    barrier.wait()
    concurrent = _issue(swapped)
    racer.join()
    successive_grant = _issue(successive)
    after_capture_grant = _issue(after_capture)

    for grant in (successive_grant, after_capture_grant, concurrent):
        assert grant.config_revision == 41
        falkordb = grant.capabilities.falkordb
        qdrant = grant.capabilities.qdrant
        assert isinstance(falkordb, FalkorDirect)
        assert isinstance(qdrant, QdrantDirect)
        assert falkordb.host == "falkor-a.test"
        assert falkordb.password == "falkor-secret-a"
        assert qdrant.url == "http://qdrant-a.test:6333"
        assert qdrant.api_key == "qdrant-secret-a"
        dumped = grant.model_dump_canonical().decode()
        assert "falkor-b.test" not in dumped
        assert "falkor-secret-rotated" not in dumped
        assert "qdrant-secret-b" not in dumped

    assert successive.capture_count == 1
