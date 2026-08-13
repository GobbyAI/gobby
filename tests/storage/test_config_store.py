"""Tests for registry-backed config mutations, reads, and path utilities."""

from pathlib import Path

import pytest

from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_KEY_KEY,
    EMBEDDING_API_KEY_SECRET_NAME,
    EMBEDDING_API_KEY_SECRET_REF,
)
from gobby.storage.config_mutations import (
    ConfigMutations,
    ConfigPatch,
    ConfigValidationError,
    SecretUpdate,
)
from gobby.storage.config_repository import ConfigRepository, ConfigRepositoryError
from gobby.storage.config_store import (
    flatten_config,
    is_secret_key_name,
    unflatten_config,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.integration


@pytest.fixture
def secret_store(
    temp_db: HubDatabase,
    tmp_path: Path,
    mock_machine_id: str,
) -> SecretStore:
    return SecretStore(temp_db, gobby_home=tmp_path)


@pytest.fixture
def mutations(temp_db: HubDatabase, secret_store: SecretStore) -> ConfigMutations:
    return ConfigMutations(temp_db, secret_store=secret_store)


@pytest.fixture
def repository(temp_db: HubDatabase, secret_store: SecretStore) -> ConfigRepository:
    return ConfigRepository(temp_db, secret_store=secret_store)


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


class TestConfigPersistence:
    def test_patch_read_upsert_and_revision(
        self,
        mutations: ConfigMutations,
        repository: ConfigRepository,
        temp_db: HubDatabase,
    ) -> None:
        mutations.patch(expected_revision=0, patch=ConfigPatch(values={"ui.enabled": True}))
        mutations.patch(expected_revision=1, patch=ConfigPatch(values={"ui.enabled": False}))

        assert repository.read(resolve_secrets=False).overrides["ui.enabled"] is False
        assert temp_db.fetchone("SELECT revision FROM config_state WHERE id = %s", (True,)) == {
            "revision": 2
        }

    def test_snapshot_contains_batch_and_supports_prefix_filtering(
        self,
        mutations: ConfigMutations,
        repository: ConfigRepository,
    ) -> None:
        values = {
            "rules.enforcement_enabled": False,
            "rules.aggregate_blocks": False,
            "ui.enabled": True,
        }
        mutations.patch(expected_revision=0, patch=ConfigPatch(values=values))

        overrides = repository.read(resolve_secrets=False).overrides
        assert overrides == values
        assert sorted(key for key in overrides if key.startswith("rules.")) == [
            "rules.aggregate_blocks",
            "rules.enforcement_enabled",
        ]

    def test_invalid_complete_candidate_rolls_back_batch(
        self,
        mutations: ConfigMutations,
        repository: ConfigRepository,
        temp_db: HubDatabase,
    ) -> None:
        with pytest.raises(ConfigValidationError, match="Complete configuration candidate"):
            mutations.patch(
                expected_revision=0,
                patch=ConfigPatch(values={"ui.enabled": True, "hooks.adapter_timeout": 130.0}),
            )

        assert temp_db.fetchall("SELECT key FROM config_store") == []
        assert repository.current_revision() == 0

    def test_corrupt_registered_row_names_key(
        self,
        mutations: ConfigMutations,
        repository: ConfigRepository,
        temp_db: HubDatabase,
    ) -> None:
        mutations.patch(expected_revision=0, patch=ConfigPatch(values={"ui.enabled": True}))
        temp_db.execute(
            "UPDATE config_store SET value = %s WHERE key = %s",
            ("{broken", "ui.enabled"),
        )

        with pytest.raises(ConfigRepositoryError, match="ui.enabled"):
            repository.read(resolve_secrets=False)

    @pytest.mark.parametrize(
        "patch",
        [
            ConfigPatch(values={"embeddings.model": "old"}),
            ConfigPatch(values={"ai.embeddings.provider": "old"}),
            ConfigPatch(secrets={"embeddings.api_key": SecretUpdate("secret")}),
            ConfigPatch(unset=frozenset({"ai.embeddings.provider"})),
        ],
    )
    def test_removed_embedding_keys_are_rejected(
        self,
        mutations: ConfigMutations,
        patch: ConfigPatch,
    ) -> None:
        with pytest.raises(ConfigValidationError, match="Unknown configuration key"):
            mutations.patch(expected_revision=0, patch=patch)

    @pytest.mark.parametrize("key", ["llm_providers", "llm_providers.openai.api_key"])
    def test_removed_provider_keys_are_rejected(
        self,
        mutations: ConfigMutations,
        key: str,
    ) -> None:
        with pytest.raises(ConfigValidationError, match="Unknown configuration key"):
            mutations.patch(expected_revision=0, patch=ConfigPatch(values={key: "value"}))

    def test_secret_reference_is_registry_derived(
        self,
        mutations: ConfigMutations,
        repository: ConfigRepository,
        temp_db: HubDatabase,
    ) -> None:
        mutations.patch(
            expected_revision=0,
            patch=ConfigPatch(secrets={AI_EMBEDDING_API_KEY_KEY: SecretUpdate("secret-value")}),
        )

        snapshot = repository.read(resolve_secrets=False)
        assert snapshot.overrides[AI_EMBEDDING_API_KEY_KEY] == EMBEDDING_API_KEY_SECRET_REF
        binding = snapshot.secret_bindings[AI_EMBEDDING_API_KEY_KEY]
        assert binding.reference == EMBEDDING_API_KEY_SECRET_REF
        assert binding.plaintext is None
        assert temp_db.fetchone(
            "SELECT is_secret FROM config_store WHERE key = %s",
            (AI_EMBEDDING_API_KEY_KEY,),
        ) == {"is_secret": True}

    def test_unresolved_secret_reference_is_rejected(self, mutations: ConfigMutations) -> None:
        with pytest.raises(ConfigValidationError, match="cannot be resolved"):
            mutations.patch(
                expected_revision=0,
                patch=ConfigPatch(values={AI_EMBEDDING_API_KEY_KEY: "$secret:falkordb_password"}),
            )

    def test_unset_and_source_tracking(
        self,
        mutations: ConfigMutations,
        repository: ConfigRepository,
        temp_db: HubDatabase,
    ) -> None:
        mutations.patch_internal(
            expected_revision=0,
            patch=ConfigPatch(values={"ui.enabled": True}),
            source="migrated",
        )
        assert temp_db.fetchone(
            "SELECT source FROM config_store WHERE key = %s",
            ("ui.enabled",),
        ) == {"source": "migrated"}
        mutations.patch(expected_revision=1, patch=ConfigPatch(unset=frozenset({"ui.enabled"})))
        assert repository.read(resolve_secrets=False).overrides == {}

    def test_unset_secret_key_clears_binding(
        self,
        mutations: ConfigMutations,
        repository: ConfigRepository,
        secret_store: SecretStore,
    ) -> None:
        mutations.patch(
            expected_revision=0,
            patch=ConfigPatch(secrets={AI_EMBEDDING_API_KEY_KEY: SecretUpdate("secret-value")}),
        )
        mutations.patch(
            expected_revision=1,
            patch=ConfigPatch(unset=frozenset({AI_EMBEDDING_API_KEY_KEY})),
        )

        snapshot = repository.read(resolve_secrets=False)
        assert AI_EMBEDDING_API_KEY_KEY not in snapshot.overrides
        assert snapshot.secret_bindings == {}
        assert secret_store.get(EMBEDDING_API_KEY_SECRET_NAME) is None

    def test_preserves_registered_value_types(
        self,
        mutations: ConfigMutations,
        repository: ConfigRepository,
    ) -> None:
        values = {
            "ui.enabled": True,
            "ui_settings.fontSize": 14,
            "workflow.timeout": 90.5,
            "tool_approvals.global_rules": ["Read(*)"],
            "launch_defaults.123": {"provider": "codex"},
        }

        mutations.patch(expected_revision=0, patch=ConfigPatch(values=values))
        assert repository.read(resolve_secrets=False).overrides == values


class TestSecretKeyDetection:
    def test_secret_suffixes(self) -> None:
        assert is_secret_key_name("database.password")
        assert is_secret_key_name("api_key")
        assert is_secret_key_name("databases.falkordb.password")
