"""Issuance binds every capability and secret to one ConfigRuntime revision."""

from __future__ import annotations

import pytest

from gobby.config.ai import GenerationEndpointConfig
from gobby.runtime_grants import DeploymentGrantContext, GrantBundle, GrantService
from gobby.runtime_grants.schema import (
    AIDaemonCapability,
    AIUnavailableCapability,
    BrokeredCapability,
    FalkorDirect,
    GrantPrincipal,
    PostgresDirect,
    QdrantDirect,
)
from tests.runtime_grants.support import (
    DEPLOYMENT_TOKEN,
    FENCING_EPOCH,
    GOLDEN_SECRET,
    SnapshotAfterCaptureRuntime,
    SuccessiveCaptureRuntime,
    config_snapshot,
    daemon_config,
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
    during_capture = SnapshotAfterCaptureRuntime(first, second, publish_during_capture=True)

    successive_grant = _issue(successive)
    after_capture_grant = _issue(after_capture)
    during_capture_grant = _issue(during_capture)

    for grant in (successive_grant, after_capture_grant, during_capture_grant):
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


@pytest.mark.unit
def test_local_falkor_host_without_password_is_direct() -> None:
    snapshot = revision_snapshot(
        41,
        host="127.0.0.1",
        password=None,
        qdrant_url="http://127.0.0.1:6333",
        api_key=None,
    )
    grant = _issue(SuccessiveCaptureRuntime(snapshot, snapshot))
    falkordb = grant.capabilities.falkordb
    assert isinstance(falkordb, FalkorDirect)
    assert falkordb.host == "127.0.0.1"
    assert falkordb.password == ""


@pytest.mark.unit
def test_local_qdrant_url_without_api_key_is_direct() -> None:
    snapshot = revision_snapshot(
        41,
        host="falkor-a.test",
        password="falkor-secret-a",
        qdrant_url="http://127.0.0.1:6333",
        api_key=None,
    )
    grant = _issue(SuccessiveCaptureRuntime(snapshot, snapshot))
    qdrant = grant.capabilities.qdrant
    assert isinstance(qdrant, QdrantDirect)
    assert qdrant.url == "http://127.0.0.1:6333"
    assert qdrant.api_key == ""


@pytest.mark.unit
def test_remote_falkor_host_without_password_is_not_direct() -> None:
    snapshot = revision_snapshot(
        41,
        host="falkor-a.test",
        password=None,
        qdrant_url="http://127.0.0.1:6333",
        api_key=None,
    )
    grant = _issue(SuccessiveCaptureRuntime(snapshot, snapshot))
    falkordb = grant.capabilities.falkordb
    assert not isinstance(falkordb, FalkorDirect)
    assert isinstance(falkordb, BrokeredCapability)
    assert falkordb.operations


@pytest.mark.unit
def test_remote_qdrant_url_without_api_key_is_not_direct() -> None:
    snapshot = revision_snapshot(
        41,
        host="127.0.0.1",
        password=None,
        qdrant_url="http://qdrant-a.test:6333",
        api_key=None,
    )
    grant = _issue(SuccessiveCaptureRuntime(snapshot, snapshot))
    qdrant = grant.capabilities.qdrant
    assert not isinstance(qdrant, QdrantDirect)
    assert isinstance(qdrant, BrokeredCapability)
    assert qdrant.operations


@pytest.mark.unit
def test_unresolved_falkor_password_is_brokered() -> None:
    snapshot = revision_snapshot(
        41,
        host="falkor-a.test",
        password=None,
        qdrant_url="http://127.0.0.1:6333",
        api_key=None,
        failed_rotation=True,
    )
    grant = _issue(SuccessiveCaptureRuntime(snapshot, snapshot))
    falkordb = grant.capabilities.falkordb
    assert isinstance(falkordb, BrokeredCapability)
    assert falkordb.operations


@pytest.mark.unit
def test_issue_expires_at_never_exceeds_postgres_valid_until() -> None:
    now = 1_700_000_000
    snapshot = revision_snapshot(
        41,
        host="127.0.0.1",
        password=None,
        qdrant_url="http://127.0.0.1:6333",
        api_key=None,
    )
    service = GrantService(
        runtime=SuccessiveCaptureRuntime(snapshot, snapshot),
        context=DeploymentGrantContext(
            token=DEPLOYMENT_TOKEN,
            fencing_epoch=FENCING_EPOCH,
            signing_secret=GOLDEN_SECRET,
        ),
    )
    postgres = PostgresDirect(
        mode="direct",
        dsn="postgresql://role:secret@127.0.0.1:5432/gobby",
        role_name="gobby_interactive_1",
        credential_generation=3,
        valid_until=now + 10,
    )
    grant = service.issue(principal=_principal(), postgres=postgres, now=now, ttl_seconds=3_600)
    assert grant.expires_at == now + 10


@pytest.mark.unit
def test_vision_and_audio_follow_configured_bindings() -> None:
    now = 1_700_000_000
    config = daemon_config()
    config = config.model_copy(
        update={
            "ai": config.ai.model_copy(
                update={
                    "generation": config.ai.generation.model_copy(
                        update={
                            "endpoints": {
                                "vision": GenerationEndpointConfig(
                                    api_base="http://127.0.0.1:9/v1",
                                    model="vision-model",
                                    probed_model="vision-model",
                                    input_modalities=["text", "image"],
                                )
                            }
                        }
                    )
                }
            ),
            "voice": config.voice.model_copy(update={"enabled": True, "stt_enabled": True}),
        }
    )
    snapshot = config_snapshot(config, revision=41)
    grant = _issue(SuccessiveCaptureRuntime(snapshot, snapshot), now=now)
    assert isinstance(grant.capabilities.vision_extract, AIDaemonCapability)
    assert isinstance(grant.capabilities.audio_transcribe, AIDaemonCapability)
    assert isinstance(grant.capabilities.text_generate, AIDaemonCapability)


@pytest.mark.unit
def test_text_generate_stays_daemon_when_embeddings_model_missing() -> None:
    now = 1_700_000_000
    config = daemon_config(embedding_model="")
    snapshot = config_snapshot(config, revision=41)
    grant = _issue(SuccessiveCaptureRuntime(snapshot, snapshot), now=now)
    assert isinstance(grant.capabilities.embed, AIUnavailableCapability)
    assert isinstance(grant.capabilities.text_generate, AIDaemonCapability)


@pytest.mark.unit
def test_vision_grant_follows_probe_evidence() -> None:
    now = 1_700_000_000
    image_config = daemon_config()
    image_config = image_config.model_copy(
        update={
            "ai": image_config.ai.model_copy(
                update={
                    "generation": image_config.ai.generation.model_copy(
                        update={
                            "endpoints": {
                                "vision": GenerationEndpointConfig(
                                    api_base="http://127.0.0.1:9/v1",
                                    model="vision-model",
                                    probed_model="vision-model",
                                    input_modalities=["text", "image"],
                                )
                            }
                        }
                    )
                }
            )
        }
    )
    image_grant = _issue(
        SuccessiveCaptureRuntime(
            config_snapshot(image_config, revision=41),
            config_snapshot(image_config, revision=41),
        ),
        now=now,
    )
    assert isinstance(image_grant.capabilities.vision_extract, AIDaemonCapability)

    degraded_config = image_config.model_copy(
        update={
            "ai": image_config.ai.model_copy(
                update={
                    "generation": image_config.ai.generation.model_copy(
                        update={
                            "endpoints": {
                                "vision": GenerationEndpointConfig(
                                    api_base="http://127.0.0.1:9/v1",
                                    model="vision-model",
                                    probed_model="vision-model",
                                    input_modalities=["text"],
                                )
                            }
                        }
                    )
                }
            )
        }
    )
    degraded_grant = _issue(
        SuccessiveCaptureRuntime(
            config_snapshot(degraded_config, revision=42),
            config_snapshot(degraded_config, revision=42),
        ),
        now=now,
    )
    assert isinstance(degraded_grant.capabilities.vision_extract, AIUnavailableCapability)

    cleared_config = image_config.model_copy(
        update={
            "ai": image_config.ai.model_copy(
                update={
                    "generation": image_config.ai.generation.model_copy(
                        update={
                            "endpoints": {
                                "vision": GenerationEndpointConfig(
                                    api_base="http://127.0.0.1:9/v1",
                                    model="vision-model",
                                )
                            }
                        }
                    )
                }
            )
        }
    )
    cleared_grant = _issue(
        SuccessiveCaptureRuntime(
            config_snapshot(cleared_config, revision=43),
            config_snapshot(cleared_config, revision=43),
        ),
        now=now,
    )
    assert isinstance(cleared_grant.capabilities.vision_extract, AIUnavailableCapability)
