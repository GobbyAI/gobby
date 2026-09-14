"""Construction-time schema identity behavior for runtime grants."""

from __future__ import annotations

import pytest

from gobby.runtime_grants import DeploymentGrantContext, GrantBundle, GrantService
from gobby.runtime_grants import service as grant_service_module
from gobby.runtime_grants.schema import GrantPrincipal, PostgresDirect, SchemaIdentity
from gobby.storage.schema_contract import expected_schema_identity
from tests.runtime_grants.support import (
    DEPLOYMENT_TOKEN,
    FENCING_EPOCH,
    GOLDEN_SECRET,
    StaticRuntime,
    revision_snapshot,
)

NOW = 1_700_000_000
TTL_SECONDS = 3_600


def _service() -> GrantService:
    return GrantService(
        runtime=StaticRuntime(
            revision_snapshot(
                41,
                host="falkor.test",
                password="falkor-secret",
                qdrant_url="http://qdrant.test:6333",
                api_key="qdrant-secret",
            )
        ),
        context=DeploymentGrantContext(
            token=DEPLOYMENT_TOKEN,
            fencing_epoch=FENCING_EPOCH,
            signing_secret=GOLDEN_SECRET,
        ),
    )


def _issue(service: GrantService) -> GrantBundle:
    return service.issue(
        principal=GrantPrincipal(
            kind="interactive",
            machine_id="machine-1",
            project_id="project-1",
            execution_id=None,
            session_id=None,
        ),
        postgres=PostgresDirect(
            mode="direct",
            dsn="postgresql://role:secret@127.0.0.1:5432/gobby",
            role_name="gobby_interactive_1",
            credential_generation=3,
            valid_until=NOW + TTL_SECONDS,
        ),
        now=NOW,
        ttl_seconds=TTL_SECONDS,
    )


def test_issue_and_present_keep_construction_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_identity = expected_schema_identity()
    installed_identity = original_identity
    monkeypatch.setattr(
        grant_service_module,
        "installed_schema_identity",
        lambda: installed_identity,
    )

    existing_service = _service()
    grant_before_update = _issue(existing_service)

    updated_identity = {
        **original_identity,
        "baseline_version": int(original_identity["baseline_version"]) + 1,
    }
    installed_identity = updated_identity

    grant_after_update = _issue(existing_service)
    expected_original = SchemaIdentity.model_validate(original_identity)
    assert grant_before_update.schema_identity == expected_original
    assert grant_after_update.schema_identity == expected_original
    assert existing_service.present(grant_before_update, now=NOW) is grant_before_update
    assert existing_service.present(grant_after_update, now=NOW) is grant_after_update

    fresh_grant = _issue(_service())
    assert fresh_grant.schema_identity == SchemaIdentity.model_validate(updated_identity)
