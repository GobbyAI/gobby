"""Tests for the registry-backed ConfigStore facade and path utilities."""

from pathlib import Path

import pytest

from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_KEY_KEY,
    EMBEDDING_API_KEY_SECRET_REF,
)
from gobby.storage.config_mutations import ConfigValidationError
from gobby.storage.config_repository import ConfigRepositoryError
from gobby.storage.config_store import (
    ConfigStore,
    flatten_config,
    is_secret_key_name,
    unflatten_config,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.integration


@pytest.fixture
def store(temp_db: HubDatabase) -> ConfigStore:
    return ConfigStore(temp_db)


@pytest.fixture
def secret_store(
    temp_db: HubDatabase,
    tmp_path: Path,
    mock_machine_id: str,
) -> SecretStore:
    return SecretStore(temp_db, gobby_home=tmp_path)


class TestFlatten:
    def test_nested_dict(self) -> None:
        assert flatten_config({"a": {"b": {"c": 1}}}) == {"a.b.c": 1}

    def test_mixed_and_list_values(self) -> None:
        source = {"port": 8080, "server": {"host": "localhost"}, "items": [1, 2]}
        assert flatten_config(source) == {
            "port": 8080,
            "server.host": "localhost",
            "items": [1, 2],
        }

    def test_nested_empty_mapping_is_preserved(self) -> None:
        assert flatten_config({"outer": {"empty": {}}}) == {"outer.empty": {}}


class TestUnflatten:
    def test_nested_and_sibling_keys(self) -> None:
        assert unflatten_config({"a.b": 1, "a.c": 2, "x": 3}) == {
            "a": {"b": 1, "c": 2},
            "x": 3,
        }

    @pytest.mark.parametrize(
        "flat",
        [
            {"a": 1, "a.b": 2},
            {"a.b": 2, "a": 1},
        ],
    )
    def test_scalar_nested_conflicts_are_rejected(self, flat: dict[str, int]) -> None:
        with pytest.raises(ValueError, match="Conflicting scalar and nested config keys"):
            unflatten_config(flat)


class TestConfigStore:
    def test_set_get_upsert_and_revision(self, store: ConfigStore, temp_db: HubDatabase) -> None:
        store.set("ui.enabled", True)
        store.set("ui.enabled", False)

        assert store.get("ui.enabled") is False
        assert temp_db.fetchone("SELECT revision FROM config_state WHERE id = %s", (True,)) == {
            "revision": 2
        }

    def test_get_all_and_list_keys(self, store: ConfigStore) -> None:
        assert (
            store.set_many(
                {
                    "rules.enforcement_enabled": False,
                    "rules.aggregate_blocks": False,
                    "ui.enabled": True,
                }
            )
            == 3
        )

        assert store.get_all() == {
            "rules.enforcement_enabled": False,
            "rules.aggregate_blocks": False,
            "ui.enabled": True,
        }
        assert store.list_keys("rules.") == [
            "rules.aggregate_blocks",
            "rules.enforcement_enabled",
        ]

    def test_invalid_complete_candidate_rolls_back_batch(
        self,
        store: ConfigStore,
        temp_db: HubDatabase,
    ) -> None:
        with pytest.raises(ConfigValidationError, match="Complete configuration candidate"):
            store.set_many({"ui.enabled": True, "hooks.adapter_timeout": 130.0})

        assert temp_db.fetchall("SELECT key FROM config_store") == []
        assert store.repository.current_revision() == 0

    def test_corrupt_registered_row_names_key(
        self,
        store: ConfigStore,
        temp_db: HubDatabase,
    ) -> None:
        store.set("ui.enabled", True)
        temp_db.execute(
            "UPDATE config_store SET value = %s WHERE key = %s",
            ("{broken", "ui.enabled"),
        )

        with pytest.raises(ConfigRepositoryError, match="ui.enabled"):
            store.get_all()

    @pytest.mark.parametrize(
        "method,args",
        [
            ("set", ("embeddings.model", "old")),
            ("set_many", ({"ai.embeddings.provider": "old"},)),
            ("set_secret", ("embeddings.api_key", "secret")),
            ("clear_secret", ("ai.embeddings.provider",)),
        ],
    )
    def test_removed_embedding_keys_are_rejected(
        self,
        store: ConfigStore,
        secret_store: SecretStore,
        method: str,
        args: tuple[object, ...],
    ) -> None:
        call = getattr(store, method)
        if method in {"set_secret", "clear_secret"}:
            args = (*args, secret_store)
        with pytest.raises(ValueError, match="removed"):
            call(*args)

    @pytest.mark.parametrize("key", ["llm_providers", "llm_providers.openai.api_key"])
    def test_removed_provider_keys_are_rejected(self, store: ConfigStore, key: str) -> None:
        with pytest.raises(ValueError, match="removed"):
            store.set(key, "value")

    def test_secret_reference_is_registry_derived(
        self,
        store: ConfigStore,
        temp_db: HubDatabase,
    ) -> None:
        store.set(AI_EMBEDDING_API_KEY_KEY, EMBEDDING_API_KEY_SECRET_REF)
        store.mark_secret_keys({AI_EMBEDDING_API_KEY_KEY})

        assert store.get_secret_keys() == [AI_EMBEDDING_API_KEY_KEY]
        assert temp_db.fetchone(
            "SELECT is_secret FROM config_store WHERE key = %s",
            (AI_EMBEDDING_API_KEY_KEY,),
        ) == {"is_secret": True}

    def test_cross_key_secret_reference_is_rejected(self, store: ConfigStore) -> None:
        with pytest.raises(ValueError, match="looks like a secret"):
            store.set(AI_EMBEDDING_API_KEY_KEY, "$secret:falkordb_password")

    def test_delete_and_source_tracking(self, store: ConfigStore, temp_db: HubDatabase) -> None:
        store.set("ui.enabled", True, source="migrated")
        assert temp_db.fetchone(
            "SELECT source FROM config_store WHERE key = %s",
            ("ui.enabled",),
        ) == {"source": "migrated"}
        assert store.delete("ui.enabled") is True
        assert store.delete("ui.enabled") is False

    def test_preserves_registered_value_types(self, store: ConfigStore) -> None:
        values = {
            "ui.enabled": True,
            "ui_settings.fontSize": 14,
            "workflow.timeout": 90.5,
            "tool_approvals.global_rules": ["Read(*)"],
            "launch_defaults.123": {"provider": "codex"},
        }

        assert store.set_many(values) == len(values)
        assert store.get_all() == values


class TestSecretKeyDetection:
    def test_secret_suffixes(self) -> None:
        assert is_secret_key_name("database.password")
        assert is_secret_key_name("api_key")
        assert is_secret_key_name("databases.falkordb.password")
