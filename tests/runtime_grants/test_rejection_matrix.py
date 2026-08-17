"""Typed rejection matrix for presented v2 grants."""

from __future__ import annotations

import pytest

from gobby.runtime_grants import (
    DeploymentGrantContext,
    ExpiredGrant,
    GrantBundle,
    GrantRejection,
    GrantService,
    InvalidGrantSignature,
    RequiredCapability,
    RevokedGrant,
    StaleEpochGrant,
    WrongApiContractGrant,
    WrongCapabilityGrant,
    WrongDeploymentGrant,
    WrongSchemaGrant,
    sign_grant,
)
from gobby.runtime_grants.schema import (
    GrantPrincipal,
    PostgresDirect,
)
from tests.runtime_grants.support import (
    DEPLOYMENT_TOKEN,
    FENCING_EPOCH,
    GOLDEN_SECRET,
    StaticRuntime,
    revision_snapshot,
)


def _service(revision: int = 41) -> GrantService:
    snapshot = revision_snapshot(
        revision,
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
    )


def _issue(service: GrantService, *, now: int = 1_700_000_000, ttl: int = 3_600) -> GrantBundle:
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
            valid_until=now + ttl,
        ),
        now=now,
        ttl_seconds=ttl,
    )


@pytest.mark.unit
def test_each_rejection_class_is_typed() -> None:
    service = _service()
    grant = _issue(service)
    now = grant.issued_at + 10

    expired = sign_grant(grant.model_copy(update={"expires_at": now - 1}), GOLDEN_SECRET)
    bad_signature = grant.model_copy(update={"signature": "00" * 32})
    wrong_deployment = sign_grant(
        grant.model_copy(
            update={"deployment": grant.deployment.model_copy(update={"token": "0123456789abcdef"})}
        ),
        GOLDEN_SECRET,
    )
    wrong_schema = sign_grant(
        grant.model_copy(
            update={
                "schema_identity": grant.schema_identity.model_copy(
                    update={"latest_version": grant.schema_identity.latest_version + 1}
                )
            }
        ),
        GOLDEN_SECRET,
    )
    wrong_contract = sign_grant(grant.model_copy(update={"api_contract": 99}), GOLDEN_SECRET)
    stale_epoch = sign_grant(
        grant.model_copy(
            update={"deployment": grant.deployment.model_copy(update={"fencing_epoch": 1})}
        ),
        GOLDEN_SECRET,
    )

    revoked_service = _service()
    revoked_grant = _issue(revoked_service)
    revoked_service.revoke(revoked_grant)

    cases: list[tuple[GrantBundle, type[GrantRejection], str]] = [
        (expired, ExpiredGrant, "expired"),
        (bad_signature, InvalidGrantSignature, "invalid_signature"),
        (wrong_deployment, WrongDeploymentGrant, "wrong_deployment"),
        (wrong_schema, WrongSchemaGrant, "wrong_schema"),
        (wrong_contract, WrongApiContractGrant, "wrong_api_contract"),
        (stale_epoch, StaleEpochGrant, "stale_epoch"),
    ]
    codes: set[str] = set()
    for presented, error_type, code in cases:
        with pytest.raises(error_type) as captured:
            service.present(presented, now=now)
        assert captured.value.code == code
        codes.add(code)

    with pytest.raises(RevokedGrant) as revoked:
        revoked_service.present(revoked_grant, now=now)
    assert revoked.value.code == "revoked"
    codes.add(revoked.value.code)

    fresh = _service()
    live = _issue(fresh)
    fresh.present(live, now=live.issued_at + 10)
    with pytest.raises(WrongCapabilityGrant) as missing:
        fresh.present(
            live,
            now=live.issued_at + 10,
            required=RequiredCapability(name="vision_extract", mode="daemon"),
        )
    assert missing.value.code == "wrong_capability"
    codes.add(missing.value.code)

    assert codes == {
        "expired",
        "invalid_signature",
        "wrong_deployment",
        "wrong_schema",
        "wrong_api_contract",
        "wrong_capability",
        "stale_epoch",
        "revoked",
    }
    assert (
        len(
            {
                ExpiredGrant,
                InvalidGrantSignature,
                WrongDeploymentGrant,
                WrongSchemaGrant,
                WrongApiContractGrant,
                WrongCapabilityGrant,
                StaleEpochGrant,
                RevokedGrant,
            }
        )
        == 8
    )
