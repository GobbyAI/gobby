"""Validate-first YAML configuration replacement contract."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

import pytest
import yaml
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from gobby.config._loading import _mask_reference_values
from gobby.config.app import DaemonConfig
from gobby.config.documents import ConfigDocumentsService
from gobby.config.registry import CONFIG_REGISTRY, DYNAMIC_SEGMENT_CODEC_VECTORS
from gobby.config.runtime import (
    ApplyFailure,
    ConfigSnapshot,
    RuntimeSecretBinding,
)
from gobby.config.secret_mask import MASKED_SECRET
from gobby.config.values import ConfigValuesError
from gobby.servers.routes.configuration_context import ConfigurationRouteContext
from gobby.servers.routes.configuration_import_export import register_import_export_routes
from gobby.servers.routes.configuration_templates import register_template_routes
from gobby.storage.config_mutations import (
    ConfigConflictError,
    ConfigMutationResult,
    ConfigPatch,
    ConfigRevisionExhaustedError,
    EmbeddingConfigMutationBlocked,
    SecretUpdate,
)
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.config_store import flatten_config
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit

_CANDIDATE_REPOSITORY = ConfigRepository(
    cast(Any, object()),
    secret_store=cast(SecretStore, object()),
)


def _snapshot(
    revision: int,
    *,
    desired: DaemonConfig | None = None,
    desired_values: Mapping[str, object] | None = None,
    desired_secrets: Mapping[str, str] | None = None,
    pending_restart_keys: frozenset[str] = frozenset(),
    failed_live_keys: Mapping[str, ApplyFailure] | None = None,
    desired_overrides: Mapping[str, object] | None = None,
) -> ConfigSnapshot:
    bindings = {
        key: RuntimeSecretBinding(f"$secret:{key}", plaintext, f"fingerprint-{key}")
        for key, plaintext in (desired_secrets or {}).items()
    }
    values = dict(desired_values or {})
    # The sparse desired_values maps in these tests model stored rows, which
    # the real runtime also exposes as overrides; export reads them from
    # desired_overrides (#20692). Pass desired_overrides to model a snapshot
    # whose materialized values exceed the stored rows.
    overrides = dict(desired_overrides) if desired_overrides is not None else values
    return ConfigSnapshot(
        revision=revision,
        desired=desired or DaemonConfig(),
        active=desired or DaemonConfig(),
        row_revisions=dict.fromkeys(values, revision),
        pending_restart_keys=pending_restart_keys,
        failed_live_keys=failed_live_keys or {},
        desired_values=values,
        active_values=values,
        desired_overrides=overrides,
        active_overrides=overrides,
        desired_bindings=bindings,
        active_bindings=bindings,
    )


class _Runtime:
    def __init__(
        self,
        snapshot: ConfigSnapshot,
        *,
        reconciled: ConfigSnapshot | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.reconciled = reconciled

    async def reconcile_local_commit(self, revision: int) -> ConfigSnapshot:
        if self.reconciled is None:
            raise AssertionError(f"unexpected reconciliation for revision {revision}")
        assert self.reconciled.revision == revision
        self.snapshot = self.reconciled
        return self.reconciled


class _Mutations:
    def __init__(
        self,
        *,
        revision: int,
        rows: Mapping[str, object] | None = None,
        secrets: Mapping[str, str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.rows = dict(rows or {})
        self.secrets = dict(secrets or {})
        self.revision = revision
        self.error = error
        self.calls: list[tuple[str, int, ConfigPatch]] = []

    def replace_namespace(
        self,
        *,
        namespace: str,
        expected_revision: int,
        patch: ConfigPatch,
    ) -> ConfigMutationResult:
        self.calls.append((namespace, expected_revision, patch))
        if self.error is not None:
            raise self.error
        before = dict(self.rows)
        self.rows = {key: value for key, value in self.rows.items() if not _is_daemon_key(key)}
        self.rows.update(patch.values)
        self.revision += 1
        changed = frozenset(
            key for key in set(before) | set(self.rows) if before.get(key) != self.rows.get(key)
        )
        return ConfigMutationResult(self.revision, changed)


class _Context:
    def __init__(self, service: ConfigDocumentsService) -> None:
        self.service = service

    def get_config_documents_service(self) -> ConfigDocumentsService:
        return self.service


async def _inline[T](operation: Callable[[], T]) -> T:
    return operation()


def _candidate(overrides: dict[str, object]) -> DaemonConfig:
    return _CANDIDATE_REPOSITORY.runtime_candidate(overrides, {})


def _is_daemon_key(key: str) -> bool:
    try:
        return CONFIG_REGISTRY.resolve(key).source_path is not None
    except KeyError:
        return False


def _service(
    snapshot: ConfigSnapshot,
    *,
    reconciled: ConfigSnapshot | None = None,
    mutations: _Mutations | None = None,
    resolve_secret: Callable[[str], str | None] | None = None,
) -> tuple[ConfigDocumentsService, _Runtime, _Mutations]:
    runtime = _Runtime(snapshot, reconciled=reconciled)
    writer = mutations or _Mutations(revision=snapshot.revision)
    service = ConfigDocumentsService(
        runtime=runtime,
        mutations=writer,
        runtime_candidate=_candidate,
        resolve_secret=resolve_secret or (lambda _name: None),
        run_blocking=_inline,
    )
    return service, runtime, writer


def _client(service: ConfigDocumentsService) -> TestClient:
    app = FastAPI()
    router = APIRouter(prefix="/api/config")
    context = cast(ConfigurationRouteContext, _Context(service))
    register_template_routes(router, context)
    register_import_export_routes(router, context)
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize(
    ("method", "path"),
    (("PUT", "/api/config/template"), ("POST", "/api/config/import")),
)
def test_yaml_paths_reject_unprobed_responses_endpoint(method: str, path: str) -> None:
    service, _runtime, mutations = _service(_snapshot(2))
    content = yaml.safe_dump(
        {
            "ai": {
                "generation": {
                    "endpoints": {
                        "neo": {
                            "wire_api": "responses",
                            "api_base": "https://neo.example/v1",
                            "model": "neo-model",
                        }
                    }
                }
            }
        }
    )

    response = _client(service).request(
        method,
        path,
        json={"expected_revision": 2, "content": content},
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "probe_required"
    assert error["path"][0] == "content"
    assert "YAML" in error["action"]
    assert mutations.calls == []


@pytest.mark.parametrize(
    ("method", "path"),
    (("PUT", "/api/config/template"), ("POST", "/api/config/import")),
)
def test_yaml_paths_accept_unchanged_probed_responses_endpoint(method: str, path: str) -> None:
    api_key = "ai.generation.endpoints.neo.api_key"
    reference = "$secret:neo_api_key"
    plaintext = "neo-secret"
    desired = DaemonConfig(
        ai={
            "generation": {
                "endpoints": {
                    "neo": {
                        "wire_api": "responses",
                        "api_base": "https://neo.example/v1",
                        "model": "neo-model",
                        "api_key": plaintext,
                    }
                }
            }
        }
    )
    values = {
        "ai.generation.endpoints.neo.wire_api": "responses",
        "ai.generation.endpoints.neo.api_base": "https://neo.example/v1",
        "ai.generation.endpoints.neo.model": "neo-model",
        api_key: reference,
    }
    service, _runtime, mutations = _service(
        _snapshot(
            2,
            desired=desired,
            desired_values=values,
            desired_secrets={api_key: plaintext},
        ),
        reconciled=_snapshot(
            3,
            desired=desired,
            desired_values=values,
            desired_secrets={api_key: plaintext},
        ),
    )
    content = yaml.safe_dump(
        {
            "ai": {
                "generation": {
                    "endpoints": {
                        "neo": {
                            "wire_api": "responses",
                            "api_base": "https://neo.example/v1",
                            "model": "neo-model",
                            "api_key": MASKED_SECRET,
                        }
                    }
                }
            },
            "websocket": {"ping_interval": 17.0},
        }
    )

    response = _client(service).request(
        method,
        path,
        json={"expected_revision": 2, "content": content},
    )

    assert response.status_code == 200
    assert len(mutations.calls) == 1
    assert mutations.calls[0][2].values[api_key] == reference


@pytest.mark.asyncio
async def test_invalid_document_has_no_side_effects() -> None:
    mutations = _Mutations(
        revision=7,
        rows={"daemon_sandbox.enabled": False},
        secrets={"existing": "keep-me"},
    )
    before = (dict(mutations.rows), dict(mutations.secrets), mutations.revision)
    service, _runtime, _writer = _service(_snapshot(7), mutations=mutations)

    with pytest.raises(ConfigValuesError) as error:
        await service.replace_yaml(
            expected_revision=7,
            content="unknown:\n  setting: true\n",
        )

    assert error.value.code == "validation_error"
    assert mutations.calls == []
    assert (mutations.rows, mutations.secrets, mutations.revision) == before


def test_daemon_replacement_is_scoped_and_atomic() -> None:
    key = "websocket.ping_interval"
    service, _runtime, mutations = _service(
        _snapshot(4),
        reconciled=_snapshot(5, desired_values={key: 22.0}),
    )

    response = _client(service).post(
        "/api/config/import",
        json={"expected_revision": 4, "content": "websocket:\n  ping_interval: 22.0\n"},
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 5
    assert mutations.calls == [("daemon", 4, ConfigPatch(values={key: 22.0}))]
    assert mutations.revision == 5


@pytest.mark.asyncio
async def test_omissions_restore_only_daemon_defaults() -> None:
    daemon_key = "websocket.ping_interval"
    supplemental_key = "ui.theme"
    domain_key = "prompts.global.review"
    mutations = _Mutations(
        revision=9,
        rows={daemon_key: 22.0, supplemental_key: "dark", domain_key: "keep"},
    )
    service, _runtime, _writer = _service(
        _snapshot(9, desired_values={daemon_key: 22.0}),
        reconciled=_snapshot(10),
        mutations=mutations,
    )

    result = await service.replace_yaml(expected_revision=9, content="{}\n")

    assert result["revision"] == 10
    assert daemon_key not in mutations.rows
    assert mutations.rows == {supplemental_key: "dark", domain_key: "keep"}
    assert mutations.calls[0][2] == ConfigPatch()


def test_masked_export_round_trip() -> None:
    key = "ai.generation.endpoints.openrouter.api_key"
    reference = "$secret:openrouter_api_key"
    plaintext = "never-export-this"
    values = {
        "ai.generation.endpoints.openrouter.api_base": "https://openrouter.example/v1",
        key: reference,
        "ai.generation.endpoints.openrouter.model": "model-a",
    }
    snapshot = _snapshot(
        3,
        desired_values=values,
        desired_secrets={key: plaintext},
    )
    service, _runtime, mutations = _service(
        snapshot,
        reconciled=_snapshot(
            4,
            desired_values=values,
            desired_secrets={key: plaintext},
        ),
    )
    client = _client(service)

    exported = client.get("/api/config/template")
    content = exported.json()["content"]
    imported = client.put(
        "/api/config/template",
        json={"expected_revision": 3, "content": content},
    )

    assert exported.status_code == 200
    assert exported.json()["revision"] == 3
    assert MASKED_SECRET in content
    assert reference not in content
    assert plaintext not in content
    assert imported.status_code == 200
    assert mutations.calls[0][2] == ConfigPatch(values=values)


def test_default_projection_with_unset_secrets_round_trips() -> None:
    values = _CANDIDATE_REPOSITORY._complete_values({})
    desired = _CANDIDATE_REPOSITORY.runtime_candidate(values, {})
    snapshot = _snapshot(0, desired=desired, desired_values=values)
    service, _runtime, _mutations = _service(
        snapshot,
        reconciled=_snapshot(1, desired=desired, desired_values=values),
    )
    client = _client(service)

    exported = client.get("/api/config/template")
    document = yaml.safe_load(exported.json()["content"])
    imported = client.put(
        "/api/config/template",
        json={"expected_revision": 0, "content": exported.json()["content"]},
    )

    assert document["databases"]["falkordb"]["password"] is None
    assert document["databases"]["qdrant"]["api_key"] is None
    assert imported.status_code == 200


@pytest.mark.asyncio
async def test_document_plaintext_secret_uses_general_category() -> None:
    key = "databases.qdrant.api_key"
    service, _runtime, mutations = _service(_snapshot(0), reconciled=_snapshot(1))

    await service.replace_yaml(
        expected_revision=0,
        content="databases:\n  qdrant:\n    api_key: qdrant-secret\n",
    )

    assert mutations.calls[0][2].secrets[key].category == "general"


def test_masking_warns_for_unresolvable_export_key(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="gobby.config._loading"):
        masked = _mask_reference_values({"unknown_export_key": "value"})

    assert masked == {"unknown_export_key": "value"}
    assert "Cannot resolve exported configuration key unknown_export_key" in caplog.text


def test_export_omits_materialized_defaults() -> None:
    """Export emits only stored overrides, never materialized defaults (#20692)."""
    stored = {"memory_backup.backup_path": ".gobby/test-memories.jsonl"}
    materialized = {**stored, "session_summary.profile": "feature_low"}
    service, _runtime, _mutations = _service(
        _snapshot(3, desired_values=materialized, desired_overrides=stored),
        reconciled=_snapshot(4),
    )

    response = _client(service).post("/api/config/export")
    document = cast(dict[str, Any], yaml.safe_load(response.json()["content"]))

    assert response.status_code == 200
    assert document == {"memory_backup": {"backup_path": ".gobby/test-memories.jsonl"}}


def test_export_masks_reference_key_even_when_stored_value_is_plaintext() -> None:
    key = "ai.generation.endpoints.openrouter.api_key"
    plaintext = "legacy-plaintext-must-never-export"
    values = {
        "ai.generation.endpoints.openrouter.api_base": "https://openrouter.example/v1",
        key: plaintext,
        "ai.generation.endpoints.openrouter.model": "model-a",
    }
    service, _runtime, mutations = _service(
        _snapshot(3, desired_values=values),
        reconciled=_snapshot(4),
    )
    client = _client(service)

    response = client.get("/api/config/template")
    content = response.json()["content"]
    imported = client.put(
        "/api/config/template",
        json={"expected_revision": 3, "content": content},
    )

    assert response.status_code == 200
    assert MASKED_SECRET in content
    assert plaintext not in content
    assert imported.status_code == 200
    assert mutations.calls[0][2].secrets[key] == SecretUpdate(
        plaintext,
        category="general",
    )


_VOICE_KEY = "voice.openai_compatible_audio"


def _voice_binding(api_key: str | None) -> dict[str, object]:
    return {
        "provider": "speaches",
        "url": "http://localhost:8080/v1",
        "model": "whisper-large-v3",
        "api_key": api_key,
    }


def test_export_masks_voice_binding_api_keys() -> None:
    reference = "$secret:SPEACHES_KEY"
    service, _runtime, _mutations = _service(
        _snapshot(3, desired_values={_VOICE_KEY: [_voice_binding(reference)]})
    )

    exported = _client(service).post("/api/config/export")
    document = cast(dict[str, Any], yaml.safe_load(exported.json()["content"]))

    assert exported.status_code == 200
    assert document["voice"]["openai_compatible_audio"][0]["api_key"] == MASKED_SECRET
    assert reference not in exported.json()["content"]


@pytest.mark.asyncio
async def test_import_restores_masked_voice_binding_key() -> None:
    reference = "$secret:SPEACHES_KEY"
    service, _runtime, mutations = _service(
        _snapshot(4, desired_values={_VOICE_KEY: [_voice_binding(reference)]}),
        reconciled=_snapshot(5, desired_values={_VOICE_KEY: [_voice_binding(reference)]}),
    )

    result = await service.replace_yaml(
        expected_revision=4,
        content=yaml.safe_dump(
            {"voice": {"openai_compatible_audio": [_voice_binding(MASKED_SECRET)]}}
        ),
    )

    assert result["revision"] == 5
    submitted = cast(list[dict[str, object]], mutations.calls[0][2].values[_VOICE_KEY])
    assert submitted[0]["api_key"] == reference


@pytest.mark.asyncio
async def test_import_rejects_plaintext_voice_binding_key() -> None:
    service, _runtime, mutations = _service(_snapshot(4))

    with pytest.raises(ConfigValuesError) as error:
        await service.replace_yaml(
            expected_revision=4,
            content=yaml.safe_dump(
                {"voice": {"openai_compatible_audio": [_voice_binding("raw-plaintext-key")]}}
            ),
        )

    assert error.value.code == "validation_error"
    assert "$secret:NAME" in error.value.message
    assert mutations.calls == []


@pytest.mark.asyncio
async def test_replace_yaml_anchors_masked_restore_to_expected_epoch() -> None:
    reference = "$secret:SPEACHES_KEY"
    service, _runtime, mutations = _service(
        _snapshot(9, desired_values={_VOICE_KEY: [_voice_binding(reference)]})
    )

    with pytest.raises(ConfigValuesError) as error:
        await service.replace_yaml(
            expected_revision=8,
            content=yaml.safe_dump(
                {"voice": {"openai_compatible_audio": [_voice_binding(MASKED_SECRET)]}}
            ),
        )

    assert error.value.code == "revision_conflict"
    assert error.value.status_code == 409
    assert error.value.actual_revision == 9
    assert mutations.calls == []


def test_stale_revision_replacement_is_rejected() -> None:
    mutations = _Mutations(
        revision=8,
        rows={"websocket.ping_interval": 12.0},
        secrets={"existing": "keep-me"},
        error=ConfigConflictError(7, 8),
    )
    before = (dict(mutations.rows), dict(mutations.secrets), mutations.revision)
    service, _runtime, _writer = _service(_snapshot(8), mutations=mutations)

    response = _client(service).put(
        "/api/config/template",
        json={"expected_revision": 7, "content": "websocket:\n  ping_interval: 15.0\n"},
    )

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "revision_conflict",
        "message": "Configuration revision is stale",
        "path": ["expected_revision"],
        "retryable": True,
        "expected_revision": 7,
        "actual_revision": 8,
    }
    assert mutations.calls == [
        (
            "daemon",
            7,
            ConfigPatch(values={"websocket.ping_interval": 15.0}),
        )
    ]
    assert (mutations.rows, mutations.secrets, mutations.revision) == before


def test_replacement_reports_apply_status() -> None:
    key = "websocket.ping_interval"
    failure = ApplyFailure(12, "websocket", frozenset({key}), "private failure details")
    service, _runtime, _mutations = _service(
        _snapshot(11),
        reconciled=_snapshot(
            12,
            desired_values={key: 30.0},
            failed_live_keys={key: failure},
        ),
    )

    response = _client(service).post(
        "/api/config/import",
        json={"expected_revision": 11, "content": "websocket:\n  ping_interval: 30.0\n"},
    )

    assert response.status_code == 200
    assert response.json()["committed"] is True
    assert response.json()["apply_status"] == "failed_live"
    assert response.json()["failed_live_keys"] == {key: {"revision": 12, "subscriber": "websocket"}}
    assert "private failure details" not in response.text


def test_yaml_round_trips_codec_vectors() -> None:
    values = {
        f"context_window_overrides.{encoded}": index
        for index, (_logical, encoded) in enumerate(DYNAMIC_SEGMENT_CODEC_VECTORS)
    }
    overrides = {
        encoded: index for index, (_logical, encoded) in enumerate(DYNAMIC_SEGMENT_CODEC_VECTORS)
    }
    service, _runtime, mutations = _service(
        _snapshot(20),
        reconciled=_snapshot(21, desired_values=values),
    )
    client = _client(service)

    imported = client.put(
        "/api/config/template",
        json={
            "expected_revision": 20,
            "content": yaml.safe_dump({"context_window_overrides": overrides}),
        },
    )
    exported = client.post("/api/config/export")
    exported_document = cast(dict[str, Any], yaml.safe_load(exported.json()["content"]))

    assert imported.status_code == 200
    assert mutations.calls[0][2].values == values
    assert exported.status_code == 200
    assert exported.json()["revision"] == 21
    assert exported_document["context_window_overrides"] == overrides
    assert flatten_config(exported_document) == values


@pytest.mark.asyncio
async def test_raw_dot_dynamic_segment_is_rejected() -> None:
    mutations = _Mutations(revision=5)
    service, _runtime, _writer = _service(_snapshot(5), mutations=mutations)

    with pytest.raises(ConfigValuesError) as error:
        await service.replace_yaml(
            expected_revision=5,
            content=yaml.safe_dump(
                {"ai": {"generation": {"endpoints": {"foo.api_base": "https://x.example"}}}}
            ),
        )

    assert error.value.code == "validation_error"
    assert "canonically encoded" in error.value.message
    assert mutations.calls == []


def test_embedding_blocked_replacement_maps_to_conflict() -> None:
    mutations = _Mutations(
        revision=6,
        error=EmbeddingConfigMutationBlocked(
            "Embedding switch run-1 is active; config mutation is blocked"
        ),
    )
    service, _runtime, _writer = _service(_snapshot(6), mutations=mutations)

    response = _client(service).put(
        "/api/config/template",
        json={"expected_revision": 6, "content": "websocket:\n  ping_interval: 15\n"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "embedding_mutation_blocked"
    assert response.json()["error"]["retryable"] is True


@pytest.mark.parametrize("revision", [True, 1.0, -1, 1 << 53])
def test_yaml_revision_domain_and_exhaustion(revision: object) -> None:
    service, _runtime, _mutations = _service(_snapshot(0))
    invalid = _client(service).post(
        "/api/config/import",
        json={"expected_revision": revision, "content": "{}\n"},
    )

    assert invalid.status_code == 422
    assert invalid.json()["detail"][0]["loc"] == ["body", "expected_revision"]

    exhausted_mutations = _Mutations(
        revision=(1 << 53) - 1,
        error=ConfigRevisionExhaustedError(),
    )
    exhausted_service, _exhausted_runtime, _writer = _service(
        _snapshot((1 << 53) - 1),
        mutations=exhausted_mutations,
    )
    exhausted = _client(exhausted_service).put(
        "/api/config/template",
        json={"expected_revision": (1 << 53) - 1, "content": "{}\n"},
    )

    assert exhausted.status_code == 422
    assert exhausted.json()["error"] == {
        "code": "revision_exhausted",
        "message": "Configuration revision cannot be advanced",
        "path": ["expected_revision"],
        "retryable": False,
    }
