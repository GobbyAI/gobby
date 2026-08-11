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
from gobby.storage.config_mutations import ConfigValidationError
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore


def test_internal_switch_journal_is_real_but_invisible_to_public_reads(
    temp_db: HubDatabase,
) -> None:
    store = ConfigStore(temp_db)
    store.set_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY, {"run_id": "run-1"})

    assert store.get_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY) == {"run_id": "run-1"}
    assert store.get(EMBEDDING_SWITCH_JOURNAL_KEY) is None
    assert EMBEDDING_SWITCH_JOURNAL_KEY not in store.get_all()
    assert EMBEDDING_SWITCH_JOURNAL_KEY not in store.list_keys()
    assert ConfigRepository(temp_db).runtime_candidate({}).embeddings is not None


def test_public_writes_reject_internal_lifecycle_key(temp_db: HubDatabase) -> None:
    store = ConfigStore(temp_db)

    with pytest.raises(ValueError, match="internal lifecycle"):
        store.set(EMBEDDING_SWITCH_JOURNAL_KEY, "payload")
    with pytest.raises(ValueError, match="internal lifecycle"):
        store.set_many({EMBEDDING_SWITCH_JOURNAL_KEY: "payload"})


def test_live_journal_blocks_embedding_mutation_and_bulk_reset_preserves_it(
    temp_db: HubDatabase,
) -> None:
    store = ConfigStore(temp_db)
    store.set("rules.enforcement_enabled", False)
    store.set_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY, {"run_id": "run-1"})

    with pytest.raises(ConfigValidationError, match="managed activation"):
        store.set(AI_EMBEDDING_MODEL_KEY, "new-model")
    assert store.delete_all() == 1

    assert store.get("rules.enforcement_enabled") is None
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
    assert store.get(AI_EMBEDDING_MODEL_KEY) == "new-model"

    assert store.delete_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY, "run-1")
    assert store.get_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY) is None


def test_bulk_delete_preserves_journal(
    temp_db: HubDatabase,
) -> None:
    class SecretStore:
        def set(self, **_kwargs: object) -> None:
            raise AssertionError("blocked secret mutation must not reach storage")

        def delete(self, _name: str) -> bool:
            return True

    store = ConfigStore(temp_db)
    secrets = SecretStore()
    store.set("rules.enforcement_enabled", False)
    store.set_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY, {"run_id": "run-1"})

    assert store.delete_all_except(secrets, set()) == 1  # type: ignore[arg-type]
    assert store.get_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY) is not None


def test_structural_keys_require_switch(temp_db: HubDatabase) -> None:
    store = ConfigStore(temp_db)

    with pytest.raises(ConfigValidationError, match="managed"):
        store.set(AI_EMBEDDING_MODEL_KEY, "text-embedding-3-large")


def test_api_key_rotation_is_live(temp_db: HubDatabase, tmp_path: Path) -> None:
    secrets = SecretStore(temp_db, gobby_home=tmp_path)
    store = ConfigStore(temp_db, secret_store=secrets)
    store.set_secret(AI_EMBEDDING_API_KEY_KEY, "first-key", secrets)
    store.set_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY, {"run_id": "run-1"})
    before = store.read_snapshot()

    store.set_secret(AI_EMBEDDING_API_KEY_KEY, "rotated-key", secrets)

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

    with pytest.raises(ConfigValidationError, match="managed activation"):
        store.set(AI_EMBEDDING_MODEL_KEY, "recovered-model")

    assert store.get(AI_EMBEDDING_MODEL_KEY) is None
    assert store.get_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY) is not None
