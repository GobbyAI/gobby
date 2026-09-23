"""Construction-time schema identity behavior for runtime grants."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from gobby.agents.code_index import _signed_grant_from_credential
from gobby.config.runtime_models import ConfigSnapshot
from gobby.runtime_grants import DeploymentGrantContext, GrantBundle, GrantService
from gobby.runtime_grants import service as grant_service_module
from gobby.runtime_grants.schema import GrantPrincipal, PostgresDirect, SchemaIdentity
from gobby.runtime_grants.signing import signature_matches
from gobby.storage.managed_credential_types import ManagedCredential
from gobby.storage.schema_contract import expected_schema_identity
from tests.runtime_grants.support import (
    DEPLOYMENT_TOKEN,
    FENCING_EPOCH,
    GOLDEN_SECRET,
    StaticRuntime,
    config_snapshot,
    daemon_config,
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


@pytest.mark.parametrize(
    ("snapshot", "expected_modes"),
    [
        pytest.param(
            config_snapshot(
                daemon_config(
                    falkor_host="127.0.0.1",
                    falkor_password=None,
                    qdrant_url="http://127.0.0.1:6333",
                    qdrant_api_key=None,
                ),
                revision=51,
            ),
            ("direct", "direct", "daemon"),
            id="loopback-direct",
        ),
        pytest.param(
            config_snapshot(
                daemon_config(
                    falkor_host="falkor.remote.test",
                    falkor_password=None,
                    qdrant_url="http://qdrant.remote.test:6333",
                    qdrant_api_key=None,
                ),
                revision=52,
            ),
            ("brokered", "brokered", "daemon"),
            id="remote-brokered",
        ),
        pytest.param(
            config_snapshot(
                daemon_config(
                    falkor_host="",
                    falkor_password=None,
                    qdrant_url=None,
                    qdrant_api_key=None,
                    embedding_model="",
                ),
                revision=53,
            ),
            ("unavailable", "unavailable", "unavailable"),
            id="unconfigured-unavailable",
        ),
    ],
)
def test_launch_grant_capabilities_match_runtime_snapshot(
    tmp_path: Path,
    snapshot: ConfigSnapshot,
    expected_modes: tuple[str, str, str],
) -> None:
    execution_id = uuid4()
    bootstrap_path = tmp_path / "bootstrap.json"
    bootstrap_path.write_text(
        (
            '{"managed_execution_id":"'
            f"{execution_id}"
            '","database_url":"postgresql://role:secret@127.0.0.1:5432/gobby"}'
        ),
        encoding="utf-8",
    )
    credential = ManagedCredential(
        managed_execution_id=execution_id,
        role_name="gobby_agent_role_1",
        credential_generation=1,
        issued_at=datetime.fromtimestamp(NOW, tz=UTC),
        expires_at=datetime.fromtimestamp(NOW + TTL_SECONDS, tz=UTC),
        bootstrap_path=bootstrap_path,
    )
    context = DeploymentGrantContext(
        token=DEPLOYMENT_TOKEN,
        fencing_epoch=FENCING_EPOCH,
        signing_secret=GOLDEN_SECRET,
    )

    launch_grant = _signed_grant_from_credential(
        credential,
        machine_id="machine-1",
        project_id="project-1",
        session_id="session-1",
        context=context,
        config_snapshot=snapshot,
    )
    runtime_grant = GrantService(runtime=StaticRuntime(snapshot), context=context).issue(
        principal=GrantPrincipal(
            kind="agent_run",
            machine_id="machine-1",
            project_id="project-1",
            execution_id=str(execution_id),
            session_id="session-1",
        ),
        postgres=launch_grant.capabilities.postgres,
        now=NOW,
        ttl_seconds=TTL_SECONDS,
    )

    assert launch_grant.config_revision == snapshot.revision
    assert signature_matches(launch_grant, GOLDEN_SECRET)
    assert launch_grant.principal.kind == "agent_run"
    assert launch_grant.principal.execution_id == str(execution_id)
    assert launch_grant.capabilities == runtime_grant.capabilities
    assert (
        launch_grant.capabilities.falkordb.mode,
        launch_grant.capabilities.qdrant.mode,
        launch_grant.capabilities.embed.mode,
    ) == expected_modes
