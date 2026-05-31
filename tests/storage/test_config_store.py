"""Tests for ConfigStore CRUD operations and flatten/unflatten utilities."""

from contextlib import nullcontext
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
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    """Create a test database with migrations applied."""
    database = temp_db
    return database


@pytest.fixture
def store(db) -> ConfigStore:
    return ConfigStore(db)


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
        original = {"llm": {"claude": {"enabled": True, "model": "opus"}}, "port": 8080}
        assert unflatten_config(flatten_config(original)) == original

    def test_empty(self):
        assert unflatten_config({}) == {}

    def test_sibling_keys(self):
        result = unflatten_config({"a.b": 1, "a.c": 2})
        assert result == {"a": {"b": 1, "c": 2}}


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

    def test_set_allows_secret_reference(self, store: ConfigStore):
        store.set(AI_EMBEDDING_API_KEY_KEY, "$secret:embeddings_api_key")
        assert store.get(AI_EMBEDDING_API_KEY_KEY) == "$secret:embeddings_api_key"

    def test_delete_existing(self, store: ConfigStore):
        store.set("key", "val")
        assert store.delete("key") is True
        assert store.get("key") is None

    def test_delete_nonexistent(self, store: ConfigStore):
        assert store.delete("nonexistent") is False

    def test_delete_all(self, store: ConfigStore):
        store.set_many({"a": 1, "b": 2, "c": 3})
        count = store.delete_all()
        assert count == 3
        assert store.get_all() == {}

    def test_delete_all_empty(self, store: ConfigStore):
        assert store.delete_all() == 0

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

    def test_set_secret_uses_backend_neutral_boolean(self):
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
        ConfigStore(db).set_secret("service.requirepass", "secret", FakeSecretStore())

        sql, params = db.executed[-1]
        assert "VALUES (%s, %s, %s, %s, %s)" in sql
        assert "is_secret = excluded.is_secret" in sql
        assert params[3] is True

    def test_get_secret_keys_uses_backend_neutral_boolean(self):
        class FakeDB:
            def __init__(self):
                self.calls = []

            def fetchall(self, sql, params=()):
                self.calls.append((sql, params))
                return [{"key": "service.requirepass"}]

        db = FakeDB()
        assert ConfigStore(db).get_secret_keys() == ["service.requirepass"]
        assert db.calls == [
            ("SELECT key FROM config_store WHERE is_secret = %s ORDER BY key", (True,))
        ]


class TestSecretKeyDetection:
    def test_requirepass_is_a_secret_suffix(self) -> None:
        assert "requirepass" in _SECRET_SUFFIXES

    def test_bare_api_key_is_secret_key_name(self) -> None:
        assert is_secret_key_name(AI_EMBEDDING_API_KEY_KEY) is True

    def test_falkordb_requirepass_is_secret_key_name(self) -> None:
        assert is_secret_key_name("databases.falkordb.requirepass") is True
