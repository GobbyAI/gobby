from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.ai.embedding_switch import (
    PHASE_ABORTED,
    PHASE_BUILDING,
    abort_switch,
    advance_phase,
    complete_aborted_switch,
    get_switch_status,
    record_switch_error,
    start_switch,
)
from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_KEY_KEY,
    AI_EMBEDDING_MODEL_KEY,
    EMBEDDING_API_KEY_SECRET_NAME,
    EMBEDDING_SWITCH_JOURNAL_KEY,
)
from gobby.storage.config_mutations import (
    ConfigMutations,
    ConfigPatch,
    ConfigValidationError,
    EmbeddingConfigMutationBlocked,
    SecretUpdate,
)
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore


def test_internal_switch_journal_is_stored_but_excluded_from_runtime_candidate(
    temp_db: HubDatabase,
) -> None:
    store = ConfigStore(temp_db)
    store.set_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY, {"run_id": "run-1"})

    assert store.get_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY) == {"run_id": "run-1"}
    repository = ConfigRepository(temp_db)
    snapshot = repository.read(resolve_secrets=False)
    assert snapshot.overrides[EMBEDDING_SWITCH_JOURNAL_KEY] == {"run_id": "run-1"}
    assert repository.runtime_candidate(dict(snapshot.overrides), {}).embeddings is not None


def test_public_writes_reject_internal_lifecycle_key(temp_db: HubDatabase) -> None:
    mutations = ConfigMutations(temp_db)

    with pytest.raises(ConfigValidationError, match="restricted"):
        mutations.patch(
            expected_revision=0,
            patch=ConfigPatch(values={EMBEDDING_SWITCH_JOURNAL_KEY: "payload"}),
        )


def test_live_journal_blocks_embedding_mutation(
    temp_db: HubDatabase,
) -> None:
    store = ConfigStore(temp_db)
    mutations = ConfigMutations(temp_db)
    mutations.patch(
        expected_revision=0,
        patch=ConfigPatch(values={"rules.enforcement_enabled": False}),
    )
    store.set_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY, {"run_id": "run-1"})

    with pytest.raises(EmbeddingConfigMutationBlocked, match="active"):
        mutations.patch(
            expected_revision=mutations.repository.current_revision(),
            patch=ConfigPatch(values={AI_EMBEDDING_MODEL_KEY: "new-model"}),
        )
    snapshot = mutations.repository.read(resolve_secrets=False)
    assert snapshot.overrides["rules.enforcement_enabled"] is False
    assert store.get_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY) is not None


def test_lifecycle_owner_can_write_config_and_delete_journal_atomically(
    temp_db: HubDatabase,
) -> None:
    store = ConfigStore(temp_db)
    store.set_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY, {"run_id": "run-1"})

    store.set_embedding_switch_values(
        "run-1",
        {AI_EMBEDDING_MODEL_KEY: "new-model"},
    )
    snapshot = ConfigRepository(temp_db).read(resolve_secrets=False)
    assert snapshot.overrides[AI_EMBEDDING_MODEL_KEY] == "new-model"

    assert store.delete_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY, "run-1")
    assert store.get_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY) is None


def test_structural_keys_require_switch(temp_db: HubDatabase) -> None:
    mutations = ConfigMutations(temp_db)

    with pytest.raises(ConfigValidationError, match="managed"):
        mutations.patch(
            expected_revision=0,
            patch=ConfigPatch(values={AI_EMBEDDING_MODEL_KEY: "text-embedding-3-large"}),
        )


def test_api_key_rotation_is_live(temp_db: HubDatabase, tmp_path: Path) -> None:
    secrets = SecretStore(temp_db, gobby_home=tmp_path)
    store = ConfigStore(temp_db, secret_store=secrets)
    mutations = ConfigMutations(temp_db, secret_store=secrets)
    mutations.patch(
        expected_revision=0,
        patch=ConfigPatch(secrets={AI_EMBEDDING_API_KEY_KEY: SecretUpdate("first-key")}),
    )
    store.set_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY, {"run_id": "run-1"})
    before = store.read_snapshot()

    mutations.patch(
        expected_revision=before.revision,
        patch=ConfigPatch(secrets={AI_EMBEDDING_API_KEY_KEY: SecretUpdate("rotated-key")}),
    )

    after = store.read_snapshot()
    assert secrets.get(EMBEDDING_API_KEY_SECRET_NAME) == "rotated-key"
    assert after.revision == before.revision + 1
    assert before.secret_bindings[AI_EMBEDDING_API_KEY_KEY].plaintext == "first-key"
    assert after.secret_bindings[AI_EMBEDDING_API_KEY_KEY].plaintext == "rotated-key"
    assert store.get_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY) == {"run_id": "run-1"}


def test_real_config_store_persists_phase_error_and_abort_cleanup_lifecycle(
    temp_db: HubDatabase,
) -> None:
    store = ConfigStore(temp_db)

    journal, _spec = start_switch(store, "qwen3-8b-q8", "ollama")
    assert get_switch_status(store).run_id == journal.run_id  # type: ignore[union-attr]

    advance_phase(store, journal, PHASE_BUILDING)
    record_switch_error(store, journal, "retryable build failure")
    persisted = get_switch_status(store)
    assert persisted is not None
    assert persisted.phase == PHASE_BUILDING
    assert persisted.error == "retryable build failure"

    aborted = abort_switch(store)
    assert aborted is not None
    assert aborted.phase == PHASE_ABORTED
    assert get_switch_status(store) is not None

    complete_aborted_switch(store, aborted)
    assert get_switch_status(store) is None


def test_malformed_switch_journal_blocks_embedding_mutation(
    temp_db: HubDatabase,
) -> None:
    store = ConfigStore(temp_db)
    store.set_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY, {"run_id": "run-1"})
    temp_db.execute(
        "UPDATE config_store SET value = %s WHERE key = %s",
        (json.dumps("{not json"), EMBEDDING_SWITCH_JOURNAL_KEY),
    )

    with pytest.raises(EmbeddingConfigMutationBlocked, match="Malformed"):
        mutations = ConfigMutations(temp_db)
        mutations.patch(
            expected_revision=mutations.repository.current_revision(),
            patch=ConfigPatch(values={AI_EMBEDDING_MODEL_KEY: "recovered-model"}),
        )

    snapshot = ConfigRepository(temp_db).read(resolve_secrets=False)
    assert AI_EMBEDDING_MODEL_KEY not in snapshot.overrides
    assert store.get_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY) is not None
