"""Tests for secrets store with real Fernet encryption and PostgreSQL."""

from __future__ import annotations

import multiprocessing
import os
import stat
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

from gobby.storage import secrets as secrets_module
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.mcp_secrets import MCPSecretSlot, protect_mcp_mapping
from gobby.storage.projects import GLOBAL_PROJECT_ID, LocalProjectManager
from gobby.storage.secret_names import SECRET_NAME_PATTERN
from gobby.storage.secrets import (
    POSTURE_KEY_FILE,
    POSTURE_SCRYPT_PASSPHRASE,
    SECRET_KEK_PASSPHRASE_ENV,
    SECRET_MATERIAL_FILENAMES,
    SECRET_REF_PATTERN,
    VALID_CATEGORIES,
    SecretDecryptionError,
    SecretInfo,
    SecretKeyUnavailable,
    SecretStore,
    _get_or_create_kek_file_key,
)

pytestmark = pytest.mark.unit


def _create_kek_with_publish_barrier(
    home: str,
    candidate: bytes,
    barrier: Any,
    result_queue: Any,
) -> None:
    """Create a KEK after synchronizing both processes at atomic publication."""
    os.environ["GOBBY_HOME"] = home
    from gobby.storage import secrets as secrets_module

    original_generate_key = secrets_module.Fernet.generate_key
    original_link = secrets_module.os.link

    def deterministic_generate_key() -> bytes:
        return candidate

    def synchronized_link(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        barrier.wait(timeout=10)
        original_link(src, dst, *args, **kwargs)

    secrets_module.Fernet.generate_key = deterministic_generate_key
    secrets_module.os.link = synchronized_link
    try:
        result_queue.put(("ok", secrets_module._get_or_create_kek_file_key()))
    except Exception as exc:
        result_queue.put(("error", repr(exc)))
    finally:
        secrets_module.Fernet.generate_key = original_generate_key
        secrets_module.os.link = original_link


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def salt_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a temporary Gobby home for secret key files."""
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def store(temp_db: HubDatabase, salt_dir: Path, mock_machine_id: str) -> SecretStore:
    """SecretStore backed by real DB, real encryption, temp salt, mocked machine ID."""
    return SecretStore(temp_db)


def _synchronize_empty_key_material_loads(
    stores: list[SecretStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force stores to observe absent key material before either initializes it."""
    first_load_barrier = threading.Barrier(len(stores))
    for store in stores:
        original_load = store._load_key_material
        load_state = {"first": True}

        def synchronized_load(
            original_load: Any = original_load,
            load_state: dict[str, bool] = load_state,
        ) -> Any:
            row = original_load()
            if load_state["first"]:
                assert row is None
                load_state["first"] = False
                first_load_barrier.wait(timeout=5)
            return row

        monkeypatch.setattr(store, "_load_key_material", synchronized_load)


def _new_project_id(store: SecretStore) -> str:
    return LocalProjectManager(store.db).create(name=f"sec-{uuid.uuid4().hex[:8]}").id


# =============================================================================
# SecretInfo
# =============================================================================


class TestSecretInfo:
    def test_to_dict_all_fields(self) -> None:
        info = SecretInfo(
            id="uuid1",
            name="API_KEY",
            category="llm",
            description="OpenAI key",
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-02T00:00:00",
        )
        d = info.to_dict()
        assert d["id"] == "uuid1"
        assert d["name"] == "API_KEY"
        assert d["category"] == "llm"
        assert d["description"] == "OpenAI key"
        assert d["created_at"] == "2024-01-01T00:00:00+00:00"
        assert d["updated_at"] == "2024-01-02T00:00:00+00:00"

    def test_to_dict_none_description(self) -> None:
        info = SecretInfo(
            id="uuid2",
            name="TOKEN",
            category="general",
            description=None,
            created_at="2024-01-01",
            updated_at="2024-01-01",
        )
        d = info.to_dict()
        assert d["description"] is None

    def test_to_dict_includes_project_scope(self) -> None:
        info = SecretInfo(
            id="uuid3",
            name="TOKEN",
            category="general",
            description=None,
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
            project_id="proj-1",
        )
        d = info.to_dict()
        assert d["project_id"] == "proj-1"
        assert d["scope"] == "project"

    def test_to_dict_marks_global_scope(self) -> None:
        info = SecretInfo(
            id="uuid4",
            name="TOKEN",
            category="general",
            description=None,
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
            project_id=GLOBAL_PROJECT_ID,
        )
        d = info.to_dict()
        assert d["project_id"] == GLOBAL_PROJECT_ID
        assert d["scope"] == "global"

    def test_slots(self) -> None:
        """SecretInfo uses __slots__ for memory efficiency."""
        info = SecretInfo(
            id="id",
            name="n",
            category="general",
            description=None,
            created_at="2024-01-01T00:00:00+00:00",
            updated_at="2024-01-01T00:00:00+00:00",
        )
        assert hasattr(info, "__slots__")
        with pytest.raises(AttributeError):
            setattr(info, "nonexistent", "value")  # noqa: B010 - intentionally exercises slots


class TestGetOrCreateKekFile:
    def test_creates_kek_file(self, salt_dir: Path) -> None:
        kek_file = salt_dir / ".secret_kek"
        assert not kek_file.exists()
        key = _get_or_create_kek_file_key()
        assert key == kek_file.read_bytes()
        assert len(key) == 44

    def test_kek_file_permissions(self, salt_dir: Path) -> None:
        _get_or_create_kek_file_key()
        kek_file = salt_dir / ".secret_kek"
        mode = oct(kek_file.stat().st_mode & 0o777)
        assert mode == "0o600"

    def test_racing_processes_converge_on_one_kek(self, salt_dir: Path) -> None:
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        result_queue = context.Queue()
        candidates = [Fernet.generate_key(), Fernet.generate_key()]
        processes = [
            context.Process(
                target=_create_kek_with_publish_barrier,
                args=(str(salt_dir), candidate, barrier, result_queue),
            )
            for candidate in candidates
        ]

        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=15)

            assert [process.exitcode for process in processes] == [0, 0]
            results = [result_queue.get(timeout=2) for _ in processes]
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            result_queue.close()
            result_queue.join_thread()

        assert [status for status, _ in results] == ["ok", "ok"]
        keys = [value for _, value in results]
        assert keys[0] == keys[1]
        assert keys[0] in candidates
        assert (salt_dir / ".secret_kek").read_bytes() == keys[0]
        assert list(salt_dir.glob("..secret_kek.*.tmp")) == []

    def test_kek_publication_fsyncs_file_and_parent(
        self,
        salt_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_fsync = os.fsync
        fsync_targets: list[str] = []

        def recording_fsync(fd: int) -> None:
            mode = os.fstat(fd).st_mode
            fsync_targets.append("directory" if stat.S_ISDIR(mode) else "file")
            original_fsync(fd)

        monkeypatch.setattr("gobby.storage.secrets.os.fsync", recording_fsync)

        _get_or_create_kek_file_key()

        assert fsync_targets == ["file", "directory"]


def test_secret_material_paths_follow_gobby_home_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each operation resolves secret material against the current Gobby home."""
    fallback_home = tmp_path / "fallback-home"
    first_home = tmp_path / "first-home"
    second_home = tmp_path / "second-home"
    monkeypatch.setenv("HOME", str(fallback_home))

    monkeypatch.setenv("GOBBY_HOME", str(first_home))
    first_kek = _get_or_create_kek_file_key()

    monkeypatch.setenv("GOBBY_HOME", str(second_home))
    second_kek = _get_or_create_kek_file_key()

    assert (first_home / ".secret_kek").read_bytes() == first_kek
    assert (second_home / ".secret_kek").read_bytes() == second_kek
    assert first_kek != second_kek
    assert not (fallback_home / ".gobby").exists()

    monkeypatch.setenv("GOBBY_HOME", str(first_home))
    assert _get_or_create_kek_file_key() == first_kek
    monkeypatch.setenv("GOBBY_HOME", str(second_home))
    assert _get_or_create_kek_file_key() == second_kek


def test_secret_material_paths_treat_blank_gobby_home_as_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-home"
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("GOBBY_HOME", " \t")

    kek = _get_or_create_kek_file_key()

    assert (user_home / ".gobby" / ".secret_kek").read_bytes() == kek


def test_secret_store_explicit_home_binds_kek(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambient_home = tmp_path / "ambient-home"
    explicit_home = tmp_path / "explicit-home"
    monkeypatch.setenv("GOBBY_HOME", str(ambient_home))

    store = SecretStore(temp_db, gobby_home=explicit_home)
    store.set("bound_secret", "value")

    assert SecretStore(temp_db, gobby_home=explicit_home).get("bound_secret") == "value"
    assert (explicit_home / ".secret_kek").exists()
    assert not (ambient_home / ".secret_kek").exists()


def test_secret_store_default_home_binds_ambient_home(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambient_home = tmp_path / "ambient-home"
    monkeypatch.setenv("GOBBY_HOME", str(ambient_home))

    store = SecretStore(temp_db)
    store._kek_fernet(POSTURE_KEY_FILE)

    assert store.gobby_home == ambient_home
    assert (ambient_home / ".secret_kek").exists()


# =============================================================================
# SecretStore._get_fernet
# =============================================================================


class TestGetFernet:
    def test_lazy_initializes(self, store: SecretStore) -> None:
        assert store._fernet is None
        fernet = store._get_fernet()
        assert fernet is not None
        assert store._fernet is fernet

    def test_returns_cached(self, store: SecretStore) -> None:
        f1 = store._get_fernet()
        f2 = store._get_fernet()
        assert f1 is f2

    def test_machine_id_legacy_surfaces_are_removed(
        self,
        temp_db: HubDatabase,
        salt_dir: Path,
    ) -> None:
        assert not hasattr(secrets_module, "get_machine_id")
        assert not hasattr(secrets_module, "_derive_fernet_key")
        assert not hasattr(SecretStore, "migrate_legacy_machine_id_secrets")
        assert SECRET_MATERIAL_FILENAMES == (".secret_kek",)
        assert SecretStore(temp_db)._get_fernet() is not None


# =============================================================================
# SecretStore.set
# =============================================================================


class TestSecretStoreSet:
    def test_set_new_secret(self, store: SecretStore) -> None:
        info = store.set("API_KEY", "sk-12345", category="llm", description="OpenAI")
        assert info.name == "api_key"  # normalized to lowercase
        assert info.category == "llm"
        assert info.description == "OpenAI"
        assert info.id  # UUID should be set
        assert info.created_at
        assert info.updated_at

    def test_set_default_category(self, store: SecretStore) -> None:
        info = store.set("TOKEN", "value")
        assert info.category == "general"

    def test_set_upsert_existing(self, store: SecretStore) -> None:
        info1 = store.set("KEY", "old-value", category="general")
        info2 = store.set("KEY", "new-value", category="llm", description="updated")
        # Same ID (upsert, not insert)
        assert info2.id == info1.id
        assert info2.category == "llm"
        assert info2.description == "updated"
        # Value actually changed
        assert store.get("KEY") == "new-value"

    def test_set_project_scope_does_not_overwrite_global(self, store: SecretStore) -> None:
        project_id = _new_project_id(store)
        global_info = store.set("SCOPED_KEY", "global-value")
        project_info = store.set("SCOPED_KEY", "project-value", project_id=project_id)
        assert global_info.id != project_info.id
        assert store.get("SCOPED_KEY") == "global-value"
        assert store.get("SCOPED_KEY", project_id=project_id) == "project-value"
        assert project_info.project_id == project_id
        assert project_info.scope == "project"
        assert global_info.scope == "global"
        assert global_info.project_id == GLOBAL_PROJECT_ID

    def test_set_invalid_category(self, store: SecretStore) -> None:
        with pytest.raises(ValueError, match="Invalid category"):
            store.set("KEY", "value", category="invalid")

    def test_set_all_valid_categories(self, store: SecretStore) -> None:
        for i, cat in enumerate(VALID_CATEGORIES):
            info = store.set(f"KEY_{cat}", f"val_{i}", category=cat)
            assert info.category == cat

    def test_set_none_description(self, store: SecretStore) -> None:
        info = store.set("KEY", "value", description=None)
        assert info.description is None

    def test_set_encrypts_value(self, store: SecretStore, temp_db: HubDatabase) -> None:
        """The stored value in the DB should NOT be the plaintext."""
        store.set("SENSITIVE", "super-secret-value")
        row = temp_db.fetchone(
            "SELECT encrypted_value FROM secrets WHERE name = %s", ("sensitive",)
        )
        assert row is not None
        assert row["encrypted_value"] != "super-secret-value"
        assert len(row["encrypted_value"]) > 0

    @pytest.mark.parametrize("name", ["", "   ", "my-key", "my.key", "1leading"])
    def test_set_rejects_names_that_cannot_be_referenced(
        self,
        store: SecretStore,
        name: str,
    ) -> None:
        with pytest.raises(ValueError, match="Invalid secret name: must start"):
            store.set(name, "value")

    @pytest.mark.parametrize(
        ("name", "normalized_name"),
        [
            ("API_KEY", "api_key"),
            ("  _Private  ", "_private"),
            ("name123", "name123"),
        ],
    )
    def test_every_accepted_name_round_trips_through_reference(
        self,
        store: SecretStore,
        name: str,
        normalized_name: str,
    ) -> None:
        info = store.set(name, "stored-value")

        assert info.name == normalized_name
        assert store.resolve(f"$secret:{info.name}") == "stored-value"


# =============================================================================
# SecretStore.get
# =============================================================================


class TestSecretStoreGet:
    def test_get_round_trip(self, store: SecretStore) -> None:
        store.set("MY_KEY", "my-secret-value")
        result = store.get("MY_KEY")
        assert result == "my-secret-value"

    def test_get_not_found(self, store: SecretStore) -> None:
        result = store.get("NONEXISTENT")
        assert result is None

    def test_get_after_update(self, store: SecretStore) -> None:
        store.set("KEY", "old")
        store.set("KEY", "new")
        assert store.get("KEY") == "new"

    def test_get_invalid_token_raises_decryption_error(
        self, temp_db: HubDatabase, salt_dir: Path
    ) -> None:
        """Corrupt envelope tokens are distinct from missing rows."""
        store = SecretStore(temp_db)
        store.set("KEY", "secret")
        temp_db.execute(
            "UPDATE secrets SET encrypted_value = %s WHERE name = %s",
            ("not-a-fernet-token", "key"),
        )

        with pytest.raises(SecretDecryptionError):
            SecretStore(temp_db).get("KEY")

    def test_get_invalid_token_error_uses_safe_identifier(
        self,
        temp_db: HubDatabase,
        salt_dir: Path,
    ) -> None:
        """Decrypt failures expose a deterministic hash, not the secret name."""
        store = SecretStore(temp_db)
        store.set("KEY", "secret")
        temp_db.execute(
            "UPDATE secrets SET encrypted_value = %s WHERE name = %s",
            ("not-a-fernet-token", "key"),
        )

        with pytest.raises(SecretDecryptionError) as exc_info:
            SecretStore(temp_db).get("KEY")

        assert exc_info.value.secret_identifier.startswith("sha256:")
        assert "KEY" not in str(exc_info.value)

    def test_get_project_falls_back_to_global(self, store: SecretStore) -> None:
        project_id = _new_project_id(store)
        store.set("FALLBACK_KEY", "global-only")
        assert store.get("FALLBACK_KEY", project_id=project_id) == "global-only"
        assert store.get("FALLBACK_KEY") == "global-only"

    def test_get_various_value_types(self, store: SecretStore) -> None:
        """Encrypt/decrypt handles various string content."""
        test_values = [
            "",
            "simple",
            "with spaces and symbols: !@#$%^&*()",
            "unicode: \u00e9\u00e0\u00fc\u00f1",
            "a" * 10000,  # large value
            '{"key": "value"}',  # JSON
            "line1\nline2\nline3",  # multiline
        ]
        for i, val in enumerate(test_values):
            name = f"VAR_{i}"
            store.set(name, val)
            assert store.get(name) == val


class TestSecretStoreEnvelope:
    def test_concurrent_first_use_converges_on_one_envelope(
        self,
        temp_db: HubDatabase,
        salt_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stores = [SecretStore(temp_db), SecretStore(temp_db)]
        _synchronize_empty_key_material_loads(stores, monkeypatch)

        with ThreadPoolExecutor(max_workers=2) as executor:
            fernets = list(executor.map(lambda store: store._get_fernet(), stores))

        stores[0].set("FIRST_WRITER", "alpha")
        stores[1].set("SECOND_WRITER", "bravo")

        fresh_store = SecretStore(temp_db)
        assert fresh_store.get("FIRST_WRITER") == "alpha"
        assert fresh_store.get("SECOND_WRITER") == "bravo"
        probe = fernets[0].encrypt(b"shared-dek")
        assert fernets[1].decrypt(probe) == b"shared-dek"
        assert temp_db.fetchone("SELECT COUNT(*) AS count FROM secret_key_material")["count"] == 1

    def test_posture_change_wins_when_default_initialization_publishes_first(
        self,
        temp_db: HubDatabase,
        salt_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        default_store = SecretStore(temp_db)
        posture_store = SecretStore(temp_db)
        stores = [default_store, posture_store]
        _synchronize_empty_key_material_loads(stores, monkeypatch)

        default_insert = default_store._insert_key_material_if_absent
        posture_insert = posture_store._insert_key_material_if_absent
        default_published = threading.Event()

        def publish_default_first(*args: Any, **kwargs: Any) -> bool:
            inserted = default_insert(*args, **kwargs)
            assert inserted
            default_published.set()
            return inserted

        def wait_for_default(*args: Any, **kwargs: Any) -> bool:
            assert default_published.wait(timeout=5)
            return posture_insert(*args, **kwargs)

        monkeypatch.setattr(default_store, "_insert_key_material_if_absent", publish_default_first)
        monkeypatch.setattr(posture_store, "_insert_key_material_if_absent", wait_for_default)

        with ThreadPoolExecutor(max_workers=2) as executor:
            default_future = executor.submit(default_store._get_fernet)
            posture_future = executor.submit(
                posture_store.set_kek_posture,
                POSTURE_SCRYPT_PASSPHRASE,
                passphrase="correct horse",
            )
            default_future.result(timeout=10)
            posture_future.result(timeout=10)

        assert posture_store.current_kek_posture() == POSTURE_SCRYPT_PASSPHRASE
        default_store.set("AFTER_POSTURE_RACE", "secret")
        assert (
            SecretStore(temp_db, kek_passphrase="correct horse").get("AFTER_POSTURE_RACE")
            == "secret"
        )
        with pytest.raises(SecretKeyUnavailable, match=SECRET_KEK_PASSPHRASE_ENV):
            SecretStore(temp_db)._get_fernet()

    def test_envelope_decryption_is_independent_of_machine_identity(
        self,
        temp_db: HubDatabase,
        salt_dir: Path,
    ) -> None:
        SecretStore(temp_db).set("KEY", "secret")

        assert not hasattr(secrets_module, "get_machine_id")
        assert SecretStore(temp_db).get("KEY") == "secret"

    def test_posture_swap_rewraps_dek_without_reencrypting_secret_rows(
        self,
        temp_db: HubDatabase,
        salt_dir: Path,
    ) -> None:
        store = SecretStore(temp_db)
        store.set("KEY", "secret")
        before = temp_db.fetchone("SELECT encrypted_value FROM secrets WHERE name = %s", ("key",))
        assert before is not None

        store.set_kek_posture(POSTURE_SCRYPT_PASSPHRASE, passphrase="correct horse")
        after_passphrase = temp_db.fetchone(
            "SELECT encrypted_value FROM secrets WHERE name = %s",
            ("key",),
        )
        assert after_passphrase is not None
        assert after_passphrase["encrypted_value"] == before["encrypted_value"]
        assert SecretStore(temp_db, kek_passphrase="correct horse").get("KEY") == "secret"

        SecretStore(temp_db, kek_passphrase="correct horse").set_kek_posture(POSTURE_KEY_FILE)
        after_key_file = temp_db.fetchone(
            "SELECT encrypted_value FROM secrets WHERE name = %s",
            ("key",),
        )
        assert after_key_file is not None
        assert after_key_file["encrypted_value"] == before["encrypted_value"]
        assert SecretStore(temp_db).get("KEY") == "secret"

    def test_passphrase_posture_requires_passphrase(
        self,
        temp_db: HubDatabase,
        salt_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = SecretStore(temp_db)
        store.set("KEY", "secret")
        store.set_kek_posture(POSTURE_SCRYPT_PASSPHRASE, passphrase="correct horse")

        with pytest.raises(SecretKeyUnavailable, match=SECRET_KEK_PASSPHRASE_ENV):
            SecretStore(temp_db)._get_fernet()

        monkeypatch.setenv(SECRET_KEK_PASSPHRASE_ENV, "correct horse")
        assert SecretStore(temp_db).get("KEY") == "secret"


class TestSecretStoreReferences:
    def test_find_secret_references_normalizes_explicit_refs(self) -> None:
        refs = SecretStore.find_secret_references(
            [
                "token=$secret:API_KEY",
                "${NOT_REQUIRED}",
                "$secret:Other_Key",
                {"headers": ["Bearer $secret:Nested_Token"]},
            ]
        )
        assert refs == {"api_key", "other_key", "nested_token"}


# =============================================================================
# SecretStore.delete
# =============================================================================


class TestSecretStoreDelete:
    def test_delete_existing(self, store: SecretStore) -> None:
        store.set("KEY", "value")
        assert store.delete("KEY") is True
        assert store.get("KEY") is None

    def test_delete_not_found(self, store: SecretStore) -> None:
        assert store.delete("NONEXISTENT") is False

    def test_delete_is_exact_scope_and_reveals_global_fallback(self, store: SecretStore) -> None:
        project_id = _new_project_id(store)
        other_id = _new_project_id(store)
        store.set("SHARED_DELETE", "global-value")
        store.set("SHARED_DELETE", "project-value", project_id=project_id)
        store.set("SHARED_DELETE", "other-value", project_id=other_id)

        assert store.delete("SHARED_DELETE", project_id=project_id) is True
        assert store.get("SHARED_DELETE", project_id=project_id) == "global-value"
        assert store.get("SHARED_DELETE", project_id=other_id) == "other-value"
        assert store.get("SHARED_DELETE") == "global-value"

        assert store.delete("SHARED_DELETE") is True
        assert store.get("SHARED_DELETE") is None
        assert store.get("SHARED_DELETE", project_id=project_id) is None
        assert store.get("SHARED_DELETE", project_id=other_id) == "other-value"

    def test_delete_then_recreate(self, store: SecretStore) -> None:
        store.set("KEY", "value1")
        store.delete("KEY")
        store.set("KEY", "value2")
        assert store.get("KEY") == "value2"


# =============================================================================
# SecretStore.list
# =============================================================================


class TestSecretStoreList:
    def test_list_empty(self, store: SecretStore) -> None:
        results = store.list()
        assert results == []

    def test_list_returns_metadata_only(self, store: SecretStore) -> None:
        store.set("A_KEY", "secret-a", category="llm", description="Key A")
        store.set("B_KEY", "secret-b", category="general")
        results = store.list()
        assert len(results) == 2
        # Sorted by name (lowercase)
        assert results[0].name == "a_key"
        assert results[1].name == "b_key"
        # Metadata present
        assert results[0].category == "llm"
        assert results[0].description == "Key A"
        # No value attribute -- SecretInfo has __slots__
        assert not hasattr(results[0], "value")
        assert not hasattr(results[0], "encrypted_value")

    def test_list_after_delete(self, store: SecretStore) -> None:
        store.set("KEY", "value")
        store.delete("KEY")
        assert store.list() == []

    def test_list_returns_secret_info_instances(self, store: SecretStore) -> None:
        store.set("MY_KEY", "val")
        results = store.list()
        assert isinstance(results[0], SecretInfo)

    def test_list_project_includes_unshadowed_global_rows(self, store: SecretStore) -> None:
        project_id = _new_project_id(store)
        store.set("GLOBAL_ONLY", "g")
        store.set("SHARED_LIST", "global-shared")
        store.set("SHARED_LIST", "project-shared", project_id=project_id)
        store.set("PROJECT_ONLY", "p", project_id=project_id)

        global_rows = {item.name: item for item in store.list()}
        assert set(global_rows) == {"global_only", "shared_list"}
        assert global_rows["shared_list"].scope == "global"

        project_rows = {item.name: item for item in store.list(project_id=project_id)}
        assert set(project_rows) == {"global_only", "shared_list", "project_only"}
        assert project_rows["shared_list"].scope == "project"
        assert project_rows["shared_list"].project_id == project_id
        assert project_rows["global_only"].scope == "global"


# =============================================================================
# SecretStore.exists
# =============================================================================


class TestSecretStoreExists:
    def test_exists_true(self, store: SecretStore) -> None:
        store.set("KEY", "value")
        assert store.exists("KEY") is True

    def test_exists_false(self, store: SecretStore) -> None:
        assert store.exists("NONEXISTENT") is False

    def test_exists_project_falls_back_to_global(self, store: SecretStore) -> None:
        project_id = _new_project_id(store)
        store.set("EXISTS_GLOBAL", "value")
        assert store.exists("EXISTS_GLOBAL", project_id=project_id) is True
        assert store.exists("EXISTS_GLOBAL") is True
        assert store.exists("missing", project_id=project_id) is False

    def test_exists_after_delete(self, store: SecretStore) -> None:
        store.set("KEY", "value")
        store.delete("KEY")
        assert store.exists("KEY") is False


# =============================================================================
# SecretStore.resolve
# =============================================================================


class TestSecretStoreResolve:
    def test_resolve_single_reference(self, store: SecretStore) -> None:
        store.set("API_KEY", "sk-12345")
        result = store.resolve("Bearer $secret:API_KEY")
        assert result == "Bearer sk-12345"

    def test_resolve_multiple_references(self, store: SecretStore) -> None:
        store.set("USER", "admin")
        store.set("PASS", "s3cret")
        result = store.resolve("$secret:USER:$secret:PASS")
        assert result == "admin:s3cret"

    def test_resolve_missing_reference_substitutes_empty(self, store: SecretStore) -> None:
        result = store.resolve("Bearer $secret:MISSING_KEY")
        assert result == "Bearer "
        assert "$secret:" not in result

    def test_resolve_missing_reference_logs_safe_identifier(
        self,
        store: SecretStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("WARNING", logger="gobby.storage.secrets"):
            result = store.resolve("Bearer $secret:MISSING_KEY")

        assert result == "Bearer "
        assert "Configured secret reference not found" in caplog.text
        assert "could not be decrypted" not in caplog.text
        assert "sha256:" in caplog.text
        assert "MISSING_KEY" not in caplog.text

    def test_resolve_decrypt_failure_logs_distinctly_and_substitutes_empty(
        self,
        temp_db: HubDatabase,
        salt_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        store = SecretStore(temp_db)
        store.set("KEY", "secret")
        temp_db.execute(
            "UPDATE secrets SET encrypted_value = %s WHERE name = %s",
            ("not-a-fernet-token", "key"),
        )

        with caplog.at_level("ERROR", logger="gobby.storage.secrets"):
            result = SecretStore(temp_db).resolve("Bearer $secret:KEY")

        assert result == "Bearer "
        assert "$secret:" not in result
        assert "Configured secret reference could not be decrypted" in caplog.text
        assert "not found" not in caplog.text
        assert any(getattr(record, "reason", None) == "invalid_token" for record in caplog.records)
        assert "KEY" not in caplog.text

    def test_resolve_no_refs(self, store: SecretStore) -> None:
        result = store.resolve("plain text no refs")
        assert result == "plain text no refs"

    def test_resolve_empty_string(self, store: SecretStore) -> None:
        assert store.resolve("") == ""

    def test_resolve_mixed_found_and_missing(self, store: SecretStore) -> None:
        store.set("FOUND", "value")
        result = store.resolve("$secret:FOUND and $secret:MISSING")
        assert result == "value and "
        assert "$secret:" not in result


# =============================================================================
# SecretStore.resolve_dict
# =============================================================================


class TestSecretStoreResolveDict:
    def test_resolve_dict(self, store: SecretStore) -> None:
        store.set("TOKEN", "bearer-token-value")
        result = store.resolve_dict(
            {
                "Authorization": "Bearer $secret:TOKEN",
                "Plain": "no-secret",
            }
        )
        assert result["Authorization"] == "Bearer bearer-token-value"
        assert result["Plain"] == "no-secret"

    def test_resolve_dict_empty(self, store: SecretStore) -> None:
        result = store.resolve_dict({})
        assert result == {}

    def test_resolve_dict_all_refs(self, store: SecretStore) -> None:
        store.set("A", "val_a")
        store.set("B", "val_b")
        result = store.resolve_dict({"x": "$secret:A", "y": "$secret:B"})
        assert result == {"x": "val_a", "y": "val_b"}

    def test_resolve_uses_project_scope(self, store: SecretStore) -> None:
        project_id = _new_project_id(store)
        store.set("RESOLVE_KEY", "global-token")
        store.set("RESOLVE_KEY", "project-token", project_id=project_id)
        assert store.resolve("Bearer $secret:resolve_key") == "Bearer global-token"
        assert (
            store.resolve("Bearer $secret:resolve_key", project_id=project_id)
            == "Bearer project-token"
        )
        assert store.resolve_dict(
            {"TOKEN": "$secret:resolve_key"},
            project_id=project_id,
        ) == {"TOKEN": "project-token"}

    def test_resolve_dict_never_forwards_missing_refs(self, store: SecretStore) -> None:
        result = store.resolve_dict(
            {
                "Authorization": "Bearer $secret:MISSING",
                "NestedJson": '{"token":"$secret:MISSING"}',
            }
        )

        assert result == {"Authorization": "Bearer ", "NestedJson": '{"token":""}'}
        assert all("$secret:" not in value for value in result.values())


# =============================================================================
# SECRET_REF_PATTERN
# =============================================================================


class TestSecretRefPattern:
    def test_matches_valid_names(self) -> None:
        assert SECRET_REF_PATTERN.search("$secret:API_KEY")
        assert SECRET_REF_PATTERN.search("$secret:_private")
        assert SECRET_REF_PATTERN.search("$secret:MyKey123")

    def test_no_match_invalid_names(self) -> None:
        assert not SECRET_REF_PATTERN.search("$secret:123start")
        assert not SECRET_REF_PATTERN.search("$secret:")
        assert not SECRET_REF_PATTERN.search("$secrets:KEY")

    def test_extracts_name_group(self) -> None:
        m = SECRET_REF_PATTERN.search("prefix $secret:MY_KEY suffix")
        assert m is not None
        assert m.group(1) == "MY_KEY"

    @pytest.mark.parametrize("name", ["API_KEY", "_private", "MyKey123", "1bad", "bad-name"])
    def test_name_and_reference_patterns_share_one_grammar(self, name: str) -> None:
        name_matches = SECRET_NAME_PATTERN.fullmatch(name) is not None
        reference_matches = SECRET_REF_PATTERN.fullmatch(f"$secret:{name}") is not None

        assert reference_matches is name_matches


# =============================================================================
# VALID_CATEGORIES
# =============================================================================


class TestValidCategories:
    def test_expected_categories(self) -> None:
        assert VALID_CATEGORIES == {"general", "llm", "mcp_server", "memory", "integration"}


# =============================================================================
# Case-insensitive name normalization
# =============================================================================


class TestNameNormalization:
    def test_name_stored_lowercase(self, store: SecretStore) -> None:
        """Setting with uppercase stores as lowercase."""
        info = store.set("MY_KEY", "value")
        assert info.name == "my_key"

    def test_upsert_case_insensitive(self, store: SecretStore) -> None:
        """Setting API_KEY then api_key should upsert, not create two rows."""
        info1 = store.set("API_KEY", "old-value")
        info2 = store.set("api_key", "new-value")
        assert info2.id == info1.id
        assert store.get("api_key") == "new-value"
        assert store.get("API_KEY") == "new-value"
        # Only one row in DB
        results = store.list()
        matching = [r for r in results if r.name == "api_key"]
        assert len(matching) == 1

    def test_get_case_insensitive(self, store: SecretStore) -> None:
        """Get should find secrets regardless of case."""
        store.set("My_Secret", "value123")
        assert store.get("my_secret") == "value123"
        assert store.get("MY_SECRET") == "value123"
        assert store.get("My_Secret") == "value123"

    def test_delete_case_insensitive(self, store: SecretStore) -> None:
        """Delete should work regardless of case."""
        store.set("API_KEY", "value")
        assert store.delete("api_key") is True
        assert store.get("API_KEY") is None

    def test_exists_case_insensitive(self, store: SecretStore) -> None:
        """Exists should match regardless of case."""
        store.set("API_KEY", "value")
        assert store.exists("api_key") is True
        assert store.exists("API_KEY") is True
        assert store.exists("Api_Key") is True

    def test_resolve_case_insensitive(self, store: SecretStore) -> None:
        """Resolve should find secrets stored with different case."""
        store.set("API_KEY", "sk-12345")
        result = store.resolve("Bearer $secret:api_key")
        assert result == "Bearer sk-12345"
        result2 = store.resolve("Bearer $secret:API_KEY")
        assert result2 == "Bearer sk-12345"

    def test_whitespace_stripped(self, store: SecretStore) -> None:
        """Leading/trailing whitespace should be stripped."""
        store.set("  MY_KEY  ", "value")
        assert store.get("my_key") == "value"
        assert store.exists("MY_KEY") is True


class TestProtectMcpMappingScope:
    def test_protect_mcp_mapping_writes_instance_scope(self, store: SecretStore) -> None:
        project_id = _new_project_id(store)
        protected = protect_mcp_mapping(
            {"API_TOKEN": "sk-project-secret"},
            secret_store=store,
            persistence="database",
            scope=project_id,
            server_name="github",
            field="env",
        )
        assert protected is not None
        slot = MCPSecretSlot("database", project_id, "github", "env", "API_TOKEN")
        assert protected["API_TOKEN"] == f"$secret:{slot.name}"
        assert store.get(slot.name, project_id=project_id) == "sk-project-secret"
        assert store.get(slot.name) is None
        row = store.db.fetchone(
            "SELECT project_id FROM secrets WHERE name = %s AND project_id = %s",
            (slot.name, project_id),
        )
        assert row is not None
        assert str(row["project_id"]) == project_id
