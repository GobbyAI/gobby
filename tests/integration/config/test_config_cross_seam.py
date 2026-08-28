"""Cross-seam integration for the reactive configuration authority.

Real ConfigMutations, ConfigRepository, ConfigRuntime, and
ConfigDocumentsService against one store that simultaneously carries
embedding overrides installed by a completed switch, the completed-switch
record itself, and voice audio bindings referencing stored secrets.

The per-seam unit suites stub the neighbours; this file exercises PATCH,
template replace, the YAML export->import round-trip, and post-switch
mutation reconciliation through the real seams in one arranged world.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import cast

import pytest

from gobby.ai.embedding_switch import (
    build_physical_names,
    complete_switch,
    managed_embedding_projection,
    persist_journal,
    start_switch,
)
from gobby.config.documents import ConfigDocumentsService
from gobby.config.runtime import ConfigRuntime
from gobby.config.secret_mask import MASKED_SECRET
from gobby.config.values import ConfigValuesError, ConfigValuesService
from gobby.config.voice_secrets import VOICE_AUDIO_BINDINGS_KEY
from gobby.mcp_proxy.tools.config import create_config_registry
from gobby.storage.config_mutations import ConfigMutations, ConfigPatch, SecretUpdate
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.config_store import ConfigStore
from gobby.storage.embedding_generation_state import managed_projection_targets
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

VOICE_SECRET_NAME = "voice_audio_api_key"
VOICE_SECRET_VALUE = "voice-plaintext-token"
FALKOR_PASSWORD_KEY = "databases.falkordb.password"
FALKOR_SECRET_NAME = "cross_seam_falkordb_password"
FALKOR_SECRET_VALUE = "cross-seam-falkor-password"
LIVE_KEY = "rules.enforcement_enabled"


class _World:
    def __init__(
        self,
        *,
        db: HubDatabase,
        secret_store: SecretStore,
        store: ConfigStore,
        mutations: ConfigMutations,
        repository: ConfigRepository,
        runtime: ConfigRuntime,
        documents: ConfigDocumentsService,
        values: ConfigValuesService,
        events: list[dict[str, object]],
        committed_revision: int,
        target_model: str,
        promoted_target: str,
    ) -> None:
        self.db = db
        self.secret_store = secret_store
        self.store = store
        self.mutations = mutations
        self.repository = repository
        self.runtime = runtime
        self.documents = documents
        self.values = values
        self.events = events
        self.committed_revision = committed_revision
        self.target_model = target_model
        self.promoted_target = promoted_target

    async def patch_and_reconcile(self, values: dict[str, object]) -> int:
        result = self.mutations.patch(
            expected_revision=self.runtime.snapshot.revision,
            patch=ConfigPatch(values=values),
        )
        await self.runtime.reconcile_local_commit(result.revision)
        return result.revision


async def _run_blocking[T](operation: Callable[[], T]) -> T:
    return operation()


@pytest.fixture
async def world(temp_db: HubDatabase, tmp_path: Path) -> AsyncIterator[_World]:
    secret_store = SecretStore(temp_db, gobby_home=tmp_path)
    store = ConfigStore(temp_db, secret_store=secret_store)
    mutations = ConfigMutations(temp_db, secret_store=secret_store)
    repository = ConfigRepository(temp_db, secret_store=secret_store)
    repository.reconcile_registry()

    secret_store.set(VOICE_SECRET_NAME, VOICE_SECRET_VALUE, category="general")
    binding = {
        "provider": "groq",
        "url": "https://api.groq.example/v1",
        "model": "whisper-large-v3",
        "api_key": f"$secret:{VOICE_SECRET_NAME}",
    }
    mutations.patch(
        expected_revision=repository.current_revision(),
        patch=ConfigPatch(values={VOICE_AUDIO_BINDINGS_KEY: [binding]}),
    )
    mutations.patch(
        expected_revision=repository.current_revision(),
        patch=ConfigPatch(
            secrets={
                FALKOR_PASSWORD_KEY: SecretUpdate(
                    FALKOR_SECRET_VALUE,
                    name=FALKOR_SECRET_NAME,
                    category="general",
                )
            }
        ),
    )

    journal, _spec = start_switch(
        store,
        "qwen3-8b-q8",
        "ollama",
        current_dim=768,
        current_catalog_id="nomic-v1.5-f16",
    )
    journal.physical_names = build_physical_names(journal)
    persist_journal(store, journal)
    record = complete_switch(store, journal)

    runtime = ConfigRuntime(
        repository,
        managed_resolver=managed_embedding_projection,
    )
    events: list[dict[str, object]] = []

    async def record_event(revision: int) -> None:
        events.append({"type": "config_event", "revision": revision})

    runtime.register_revision_publisher(record_event)
    await runtime.start()
    values = ConfigValuesService(
        runtime=runtime,
        mutations=mutations,
        run_blocking=_run_blocking,
    )
    documents = ConfigDocumentsService(
        runtime=runtime,
        mutations=mutations,
        runtime_candidate=lambda overrides: repository.runtime_candidate(overrides, {}),
        resolve_secret=secret_store.get,
        run_blocking=_run_blocking,
    )
    try:
        yield _World(
            db=temp_db,
            secret_store=secret_store,
            store=store,
            mutations=mutations,
            repository=repository,
            runtime=runtime,
            documents=documents,
            values=values,
            events=events,
            committed_revision=record.committed_revision,
            target_model=journal.target_model,
            promoted_target=record.physical_names["memories"],
        )
    finally:
        await runtime.close()


async def test_reference_secret_is_masked_across_values_yaml_and_events(world: _World) -> None:
    values_body = await world.values.values()
    rendered_values = json.dumps(values_body, sort_keys=True)
    secret_set = cast(dict[str, dict[str, bool]], values_body["secret_set"])

    assert secret_set[FALKOR_PASSWORD_KEY] == {"desired": True, "active": True}
    assert MASKED_SECRET in rendered_values
    assert FALKOR_SECRET_VALUE not in rendered_values
    assert f"$secret:{FALKOR_SECRET_NAME}" not in rendered_values

    exported = await world.documents.export_yaml()
    content = cast(str, exported["content"])
    assert MASKED_SECRET in content
    assert FALKOR_SECRET_VALUE not in content
    assert f"$secret:{FALKOR_SECRET_NAME}" not in content

    revision = await world.patch_and_reconcile({LIVE_KEY: False})
    assert world.events[-1] == {"type": "config_event", "revision": revision}
    rendered_events = json.dumps(world.events, sort_keys=True)
    assert FALKOR_SECRET_VALUE not in rendered_events
    assert f"$secret:{FALKOR_SECRET_NAME}" not in rendered_events


async def test_invalid_reference_secret_is_redacted_from_mcp_result(world: _World) -> None:
    invalid = "plaintext must not leak"
    registry = create_config_registry(lambda: world.values)

    result = await registry.call(
        "patch_config_values",
        {
            "expected_revision": world.runtime.snapshot.revision,
            "values": {"databases": {"falkordb": {"password": invalid}}},
        },
    )

    assert result["error"] == {
        "code": "validation_error",
        "message": "Secret configuration value is invalid",
        "path": ["values", "databases", "falkordb", "password"],
        "retryable": False,
    }
    assert invalid not in json.dumps(result, sort_keys=True)


async def test_patch_preserves_switch_installed_overrides(world: _World) -> None:
    snapshot_before = world.runtime.snapshot
    assert snapshot_before.desired_values["ai.embeddings.model"] == world.target_model

    revision = await world.patch_and_reconcile({LIVE_KEY: False})

    snapshot = world.runtime.snapshot
    assert snapshot.revision == revision
    assert snapshot.active_values[LIVE_KEY] is False
    assert snapshot.desired_values["ai.embeddings.model"] == world.target_model
    bindings = cast(list[dict[str, object]], snapshot.desired_values[VOICE_AUDIO_BINDINGS_KEY])
    assert bindings[0]["api_key"] == f"$secret:{VOICE_SECRET_NAME}"


async def test_yaml_export_import_round_trip(world: _World) -> None:
    await world.patch_and_reconcile(
        {
            "session_summary.candidates": [{"candidate": "codex/gpt-5.6-sol"}],
            "ai.model_metadata_aliases": [
                {
                    "provider": "codex",
                    "provider_model_id": "gpt-5.6-sol",
                    "openrouter_model_id": "openai/gpt-5.6-sol",
                }
            ],
            "memory_backup.backup_path": ".gobby/test-memories.jsonl",
        }
    )
    sparse_revision = world.runtime.snapshot.revision + 1
    sparse_candidates = '[{"candidate":"codex/gpt-5.6-sol"}]'
    with world.db.transaction() as transaction:
        transaction.execute(
            "UPDATE config_store SET value = %s, revision = %s WHERE key = %s",
            (sparse_candidates, sparse_revision, "session_summary.candidates"),
        )
        transaction.execute(
            "UPDATE config_state SET revision = %s WHERE id = %s",
            (sparse_revision, True),
        )
    await world.runtime.reconcile_local_commit(sparse_revision)
    assert world.repository.read().overrides["session_summary.candidates"] == [
        {"candidate": "codex/gpt-5.6-sol"}
    ]

    exported = await world.documents.export_yaml()
    content = cast(str, exported["content"])
    revision = cast(int, exported["revision"])

    # The export masks the bound voice secret and never leaks its plaintext.
    assert VOICE_SECRET_VALUE not in content
    assert MASKED_SECRET in content

    event_count = len(world.events)
    for _ in range(2):
        result = await world.documents.replace_yaml(expected_revision=revision, content=content)
        assert result["revision"] == revision
        assert result["changed_keys"] == []

    assert len(world.events) == event_count
    snapshot = world.runtime.snapshot
    assert snapshot.revision == revision
    # Round-trip idempotence across the real seams: the masked voice key is
    # restored to its persisted secret reference, the switch-installed
    # embedding overrides survive, and the secret still resolves.
    bindings = cast(list[dict[str, object]], snapshot.desired_values[VOICE_AUDIO_BINDINGS_KEY])
    assert bindings[0]["api_key"] == f"$secret:{VOICE_SECRET_NAME}"
    assert snapshot.desired_values["ai.embeddings.model"] == world.target_model
    assert snapshot.desired_secret(VOICE_AUDIO_BINDINGS_KEY) is None  # not a flat secret key
    assert world.secret_store.get(VOICE_SECRET_NAME) == VOICE_SECRET_VALUE


async def test_template_replace_rejects_plaintext_voice_keys(world: _World) -> None:
    exported = await world.documents.export_yaml()
    content = cast(str, exported["content"])
    revision = cast(int, exported["revision"])
    poisoned = content.replace(MASKED_SECRET, "sk-plaintext-leak")

    with pytest.raises(ConfigValuesError):
        await world.documents.replace_yaml(expected_revision=revision, content=poisoned)

    # The rejected replace must not have advanced the committed revision.
    assert world.repository.current_revision() == revision


async def test_post_switch_mutation_reconciles_managed_targets(world: _World) -> None:
    await world.runtime.reconcile_local_commit(world.committed_revision)

    revision = await world.patch_and_reconcile({LIVE_KEY: True})

    bundle = world.runtime.capture()
    assert bundle.snapshot.revision == revision
    targets = managed_projection_targets(bundle.managed, "memory", "memories")
    assert targets[0] == "memories"
    assert world.promoted_target in targets
    assert bundle.snapshot.active_values[LIVE_KEY] is True
