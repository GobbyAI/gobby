"""Tests for ConfigStore CRUD operations and flatten/unflatten utilities."""

from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.config.embedding_keys import AI_EMBEDDING_API_KEY_KEY, runtime_embedding_key
from gobby.storage.config_store import (
    _SECRET_SUFFIXES,
    ConfigStore,
    flatten_config,
    is_secret_key_name,
    unflatten_config,
)
from gobby.storage.hub.protocol import Cursor, HubDatabase
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    """Create a test database with migrations applied."""
    database = temp_db
    return database


@pytest.fixture
def store(db) -> ConfigStore:
    return ConfigStore(db)


@pytest.fixture
def secret_store(
    db: HubDatabase,
    tmp_path: Path,
    mock_machine_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> SecretStore:
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    return SecretStore(db)


# =============================================================================
# flatten / unflatten
# =============================================================================


class TestFlatten:
    def test_flat_dict(self):
        assert flatten_config({"a": 1, "b": "x"}) == {"a": 1, "b": "x"}

    def test_nested_dict(self):
        result = flatten_config({"llm": {"claude": {"enabled": True}}})
        assert result == {"llm.claude.enabled": True}

    def test_mixed(self):
        result = flatten_config({"port": 8080, "llm": {"key": "abc"}})
        assert result == {"port": 8080, "llm.key": "abc"}

    def test_list_preserved(self):
        result = flatten_config({"tags": ["a", "b"]})
        assert result == {"tags": ["a", "b"]}

    def test_empty_dict(self):
        assert flatten_config({}) == {}

    def test_nested_empty_dict_is_preserved(self):
        assert flatten_config({"section": {"empty": {}}}) == {"section.empty": {}}

    def test_prefix(self):
        result = flatten_config({"key": "val"}, prefix="root")
        assert result == {"root.key": "val"}


class TestUnflatten:
    def test_simple(self):
        assert unflatten_config({"a": 1}) == {"a": 1}

    def test_nested(self):
        result = unflatten_config({"llm.claude.enabled": True})
        assert result == {"llm": {"claude": {"enabled": True}}}

    def test_roundtrip(self):
        original = {
            "llm": {"claude": {"enabled": True, "model": "opus"}},
            "empty": {},
            "port": 8080,
        }
        assert unflatten_config(flatten_config(original)) == original

    def test_empty(self):
        assert unflatten_config({}) == {}

    def test_sibling_keys(self):
        result = unflatten_config({"a.b": 1, "a.c": 2})
        assert result == {"a": {"b": 1, "c": 2}}

    @pytest.mark.parametrize(
        "flat",
        [
            {"a": 1, "a.b": 2},
            {"a.b": 2, "a": 1},
        ],
    )
    def test_scalar_nested_conflicts_are_rejected_independent_of_order(self, flat):
        with pytest.raises(ValueError, match="Conflicting scalar and nested config keys"):
            unflatten_config(flat)


# =============================================================================
# ConfigStore CRUD
# =============================================================================


class TestConfigStore:
    def test_set_and_get(self, store: ConfigStore):
        store.set("daemon_port", 9000)
        assert store.get("daemon_port") == 9000

    def test_get_nonexistent(self, store: ConfigStore):
        assert store.get("nonexistent") is None

    def test_get_all_empty(self, store: ConfigStore):
        assert store.get_all() == {}

    def test_get_all(self, store: ConfigStore):
        store.set("a", 1)
        store.set("b", "hello")
        result = store.get_all()
        assert result == {"a": 1, "b": "hello"}

    def test_get_corrupt_json_names_key(self, store: ConfigStore):
        store.set("broken.key", "valid")
        store.db.execute(
            "UPDATE config_store SET value = %s WHERE key = %s",
            ("{invalid", "broken.key"),
        )

        with pytest.raises(ValueError, match=r"Invalid JSON for config key 'broken\.key'"):
            store.get("broken.key")

    def test_get_all_corrupt_json_names_key(self, store: ConfigStore):
        store.set("broken.key", "valid")
        store.db.execute(
            "UPDATE config_store SET value = %s WHERE key = %s",
            ("{invalid", "broken.key"),
        )

        with pytest.raises(ValueError, match=r"Invalid JSON for config key 'broken\.key'"):
            store.get_all()

    def test_set_upsert(self, store: ConfigStore):
        store.set("key", "old")
        store.set("key", "new")
        assert store.get("key") == "new"

    def test_set_many(self, store: ConfigStore):
        count = store.set_many({"a": 1, "b": True, "c": "str"})
        assert count == 3
        assert store.get("a") == 1
        assert store.get("b") is True
        assert store.get("c") == "str"

    def test_set_many_rolls_back_all_entries_on_failure(
        self, store: ConfigStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original_execute = store.db.execute
        insert_count = 0

        def fail_on_second_insert(
            sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
        ) -> Cursor:
            nonlocal insert_count
            if "INSERT INTO config_store" in sql:
                insert_count += 1
                if insert_count == 2:
                    raise RuntimeError("injected config write failure")
            return original_execute(sql, params)

        monkeypatch.setattr(store.db, "execute", fail_on_second_insert)

        with pytest.raises(RuntimeError, match="injected config write failure"):
            store.set_many({"first": 1, "second": 2})

        assert store.get("first") is None
        assert store.get("second") is None

    def test_set_rejects_plaintext_secret_key(self, store: ConfigStore):
        with pytest.raises(ValueError, match=r"Config key 'ai\.embeddings\.api_key'"):
            store.set(AI_EMBEDDING_API_KEY_KEY, "sk-plaintext")

    def test_set_many_rejects_plaintext_secret_key(self, store: ConfigStore):
        with pytest.raises(ValueError, match=r"Config key 'ai\.embeddings\.api_key'"):
            store.set_many({AI_EMBEDDING_API_KEY_KEY: "sk-plaintext"})

    @pytest.mark.parametrize("key", [runtime_embedding_key("model"), "ai.embeddings.provider"])
    def test_set_rejects_removed_embedding_keys(self, store: ConfigStore, key: str):
        with pytest.raises(ValueError, match="Embedding"):
            store.set(key, "nomic-embed-text")

    @pytest.mark.parametrize("key", [runtime_embedding_key("model"), "ai.embeddings.provider"])
    def test_set_many_rejects_removed_embedding_keys(self, store: ConfigStore, key: str):
        with pytest.raises(ValueError, match="Embedding"):
            store.set_many({key: "nomic-embed-text"})

    @pytest.mark.parametrize("key", [runtime_embedding_key("api_key"), "ai.embeddings.provider"])
    def test_set_secret_rejects_removed_embedding_keys(self, store: ConfigStore, key: str):
        with pytest.raises(ValueError, match="Embedding"):
            store.set_secret(key, "secret", MagicMock())

    @pytest.mark.parametrize("key", [runtime_embedding_key("api_key"), "ai.embeddings.provider"])
    def test_clear_secret_rejects_removed_embedding_keys(self, store: ConfigStore, key: str):
        with pytest.raises(ValueError, match="Embedding"):
            store.clear_secret(key, MagicMock())

    @pytest.mark.parametrize(
        ("method", "args"),
        [
            ("set", ("llm_providers.default_model", "sonnet")),
            ("set_many", ({"llm_providers.default_model": "sonnet"},)),
            ("set_secret", ("llm_providers.api_keys.openai_api_key", "secret", MagicMock())),
            ("clear_secret", ("llm_providers.api_keys.openai_api_key", MagicMock())),
        ],
    )
    def test_write_methods_reject_removed_llm_provider_keys(
        self, store: ConfigStore, method: str, args: tuple[object, ...]
    ):
        with pytest.raises(ValueError, match="llm_providers"):
            getattr(store, method)(*args)

    def test_set_allows_secret_reference(self, store: ConfigStore):
        store.set(AI_EMBEDDING_API_KEY_KEY, None)
        store.set(AI_EMBEDDING_API_KEY_KEY, "$secret:embeddings_api_key")
        assert store.get(AI_EMBEDDING_API_KEY_KEY) == "$secret:embeddings_api_key"
        assert AI_EMBEDDING_API_KEY_KEY in store.get_secret_keys()

    def test_set_many_marks_canonical_secret_references(self, store: ConfigStore):
        store.set("service.credential", "configured-externally")
        store.set_many({"service.credential": "$secret:credential", "daemon_port": 9000})

        assert store.get_secret_keys() == ["service.credential"]

    def test_mark_secret_keys_marks_existing_rows(self, store: ConfigStore):
        store.set("service.credential", "configured-externally")

        store.mark_secret_keys({"service.credential"})

        assert store.get_secret_keys() == ["service.credential"]

    def test_set_rejects_cross_key_secret_reference(self, store: ConfigStore):
        with pytest.raises(ValueError, match=r"Config key 'ai\.embeddings\.api_key'"):
            store.set(AI_EMBEDDING_API_KEY_KEY, "$secret:openai_api_key")

    def test_set_many_rejects_cross_key_secret_reference(self, store: ConfigStore):
        with pytest.raises(ValueError, match=r"Config key 'service\.password'"):
            store.set_many({"service.password": "$secret:other_password"})

    def test_delete_existing(self, store: ConfigStore):
        store.set("key", "val")
        assert store.delete("key") is True
        assert store.get("key") is None

    def test_delete_nonexistent(self, store: ConfigStore):
        assert store.delete("nonexistent") is False

    def test_delete_rejects_secret_keys(
        self,
        store: ConfigStore,
        secret_store: SecretStore,
    ) -> None:
        key = "service.provider_api_key"
        store.set_secret(key, "keep-me", secret_store)

        with pytest.raises(ValueError, match="use clear_secret"):
            store.delete(key)

        assert store.get(key) == "$secret:provider_api_key"
        assert secret_store.get("provider_api_key") == "keep-me"

    def test_delete_all(self, store: ConfigStore, secret_store: SecretStore):
        store.set_many({"a": 1, "b": 2, "c": 3})
        count = store.delete_all(secret_store)
        assert count == 3
        assert store.get_all() == {}

    def test_delete_all_empty(self, store: ConfigStore, secret_store: SecretStore):
        assert store.delete_all(secret_store) == 0

    def test_delete_all_removes_only_config_backed_secrets(
        self,
        store: ConfigStore,
        secret_store: SecretStore,
    ) -> None:
        store.set_secret("service.provider_api_key", "config-secret", secret_store)
        secret_store.set("independent_token", "keep-me")

        assert store.delete_all(secret_store) == 1

        assert store.get_all() == {}
        assert secret_store.get("provider_api_key") is None
        assert secret_store.get("independent_token") == "keep-me"

    def test_delete_all_preserves_explicit_incoming_secret_reference(
        self,
        store: ConfigStore,
        secret_store: SecretStore,
    ) -> None:
        key = "service.provider_api_key"
        store.set_secret(key, "preserved", secret_store)

        assert store.delete_all(secret_store, preserved_secret_keys={key}) == 1

        assert store.get_all() == {}
        assert secret_store.get("provider_api_key") == "preserved"

    def test_delete_all_rolls_back_config_and_secret_deletions(
        self,
        store: ConfigStore,
        secret_store: SecretStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first_key = "first_secret_token"
        second_key = "second_secret_token"
        store.set_secret(first_key, "first", secret_store)
        store.set_secret(second_key, "second", secret_store)
        original_delete = secret_store.delete

        def fail_after_first_delete(name: str) -> bool:
            if name == second_key:
                raise RuntimeError("injected secret deletion failure")
            return original_delete(name)

        monkeypatch.setattr(secret_store, "delete", fail_after_first_delete)

        with pytest.raises(RuntimeError, match="injected secret deletion failure"):
            store.delete_all(secret_store)

        assert store.get_secret_keys() == [first_key, second_key]
        assert secret_store.get(first_key) == "first"
        assert secret_store.get(second_key) == "second"

    def test_list_keys(self, store: ConfigStore):
        store.set_many({"z": 1, "a": 2, "m": 3})
        keys = store.list_keys()
        assert keys == ["a", "m", "z"]  # sorted

    def test_list_keys_with_prefix(self, store: ConfigStore):
        store.set_many({"llm.a": 1, "llm.b": 2, "port": 3})
        keys = store.list_keys(prefix="llm.")
        assert keys == ["llm.a", "llm.b"]

    def test_source_tracking(self, store: ConfigStore):
        store.set("key", "val", source="migrated")
        row = store.db.fetchone("SELECT source FROM config_store WHERE key = %s", ("key",))
        assert row["source"] == "migrated"

    def test_preserves_types(self, store: ConfigStore):
        store.set("bool_val", True)
        store.set("int_val", 42)
        store.set("float_val", 3.14)
        store.set("str_val", "hello")
        store.set("list_val", [1, 2, 3])
        store.set("null_val", None)

        assert store.get("bool_val") is True
        assert store.get("int_val") == 42
        assert store.get("float_val") == 3.14
        assert store.get("str_val") == "hello"
        assert store.get("list_val") == [1, 2, 3]
        assert store.get("null_val") is None

    def test_set_secret_uses_backend_neutral_boolean(self) -> None:
        class FakeDB:
            def __init__(self):
                self.executed = []

            def transaction(self):
                return nullcontext()

            def execute(self, sql, params=()):
                self.executed.append((sql, params))

        class FakeSecretStore:
            def set(self, **kwargs):
                self.kwargs = kwargs

        db = FakeDB()
        ConfigStore(db).set_secret("service.password", "secret", FakeSecretStore())

        sql, params = db.executed[-1]
        assert "VALUES (%s, %s, %s, %s, %s)" in sql
        assert "is_secret = excluded.is_secret" in sql
        assert params[3] is True

    def test_get_secret_keys_uses_backend_neutral_boolean(self) -> None:
        class FakeDB:
            def __init__(self):
                self.calls = []

            def fetchall(self, sql, params=()):
                self.calls.append((sql, params))
                return [{"key": "service.password"}]

        db = FakeDB()
        assert ConfigStore(db).get_secret_keys() == ["service.password"]
        assert db.calls == [
            ("SELECT key FROM config_store WHERE is_secret = %s ORDER BY key", (True,))
        ]


class TestSecretKeyDetection:
    def test_password_is_a_secret_suffix(self) -> None:
        assert "password" in _SECRET_SUFFIXES

    def test_bare_api_key_is_secret_key_name(self) -> None:
        assert is_secret_key_name(AI_EMBEDDING_API_KEY_KEY) is True

    def test_falkordb_password_is_secret_key_name(self) -> None:
        assert is_secret_key_name("databases.falkordb.password") is True
