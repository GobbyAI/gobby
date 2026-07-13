"""Tests for secrets store with real Fernet encryption and PostgreSQL.

Uses temp_db fixture for real database operations and mock_machine_id
for deterministic key derivation. Only external I/O (machine ID lookup)
is mocked.
"""

from __future__ import annotations

import multiprocessing
import os
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import (
    POSTURE_KEY_FILE,
    POSTURE_SCRYPT_PASSPHRASE,
    SECRET_KEK_PASSPHRASE_ENV,
    SECRET_REF_PATTERN,
    VALID_CATEGORIES,
    InvalidSecretSaltError,
    SecretDecryptionError,
    SecretInfo,
    SecretKeyUnavailable,
    SecretMigrationError,
    SecretStore,
    _derive_fernet_key,
    _get_or_create_kek_file_key,
    _get_or_create_salt,
)

pytestmark = pytest.mark.unit


def _create_salt_with_publish_barrier(
    home: str,
    candidate: bytes,
    barrier: Any,
    result_queue: Any,
) -> None:
    """Create a salt after synchronizing both processes at atomic publication."""
    os.environ["GOBBY_HOME"] = home
    from gobby.storage import secrets as secrets_module

    original_link = secrets_module.os.link
    original_urandom = secrets_module.os.urandom
    original_exists = secrets_module.Path.exists
    original_read_bytes = secrets_module.Path.read_bytes
    salt_file = secrets_module.Path(home) / ".secret_salt"
    is_first_lookup = True

    def deterministic_urandom(size: int) -> bytes:
        return candidate if size == len(candidate) else original_urandom(size)

    def synchronized_exists(path: Path) -> bool:
        nonlocal is_first_lookup
        exists = original_exists(path)
        if is_first_lookup and path == salt_file:
            is_first_lookup = False
            barrier.wait(timeout=10)
        return exists

    def synchronized_read_bytes(path: Path) -> bytes:
        nonlocal is_first_lookup
        if not is_first_lookup or path != salt_file:
            return original_read_bytes(path)

        try:
            salt = original_read_bytes(path)
        except FileNotFoundError:
            is_first_lookup = False
            barrier.wait(timeout=10)
            raise
        is_first_lookup = False
        barrier.wait(timeout=10)
        return salt

    def synchronized_link(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        barrier.wait(timeout=10)
        original_link(src, dst, *args, **kwargs)

    secrets_module.os.urandom = deterministic_urandom
    secrets_module.Path.exists = synchronized_exists
    secrets_module.Path.read_bytes = synchronized_read_bytes
    secrets_module.os.link = synchronized_link
    try:
        result_queue.put(("ok", secrets_module._get_or_create_salt()))
    except Exception as exc:
        result_queue.put(("error", repr(exc)))


def _create_salt_with_paused_write(
    home: str,
    partial_ready: Any,
    release_write: Any,
    result_queue: Any,
) -> None:
    """Pause a real child process after its first byte reaches the temp file."""
    os.environ["GOBBY_HOME"] = home
    from gobby.storage import secrets as secrets_module

    original_write = secrets_module.os.write
    is_first_write = True

    def paused_write(fd: int, data: Any) -> int:
        nonlocal is_first_write
        if is_first_write:
            is_first_write = False
            written = original_write(fd, data[:1])
            partial_ready.set()
            if not release_write.wait(timeout=10):
                raise TimeoutError("test did not release the paused salt write")
            return written
        return original_write(fd, data)

    secrets_module.os.write = paused_write
    try:
        result_queue.put(("ok", secrets_module._get_or_create_salt()))
    except Exception as exc:
        result_queue.put(("error", repr(exc)))


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


def _insert_legacy_secret(
    db: HubDatabase,
    name: str,
    plaintext: str | None = None,
    *,
    machine_id: str = "machine-A",
    encrypted_value: str | None = None,
) -> str:
    normalized = SecretStore._normalize_name(name)
    token = encrypted_value
    if token is None:
        salt = _get_or_create_salt()
        legacy = Fernet(_derive_fernet_key(machine_id, salt))
        token = legacy.encrypt((plaintext or "").encode("utf-8")).decode("utf-8")
    db.execute(
        """INSERT INTO secrets (id, name, encrypted_value, category, description, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (
            str(uuid.uuid4()),
            normalized,
            token,
            "general",
            None,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    return token


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


# =============================================================================
# _get_or_create_salt
# =============================================================================


class TestGetOrCreateSalt:
    def test_creates_salt_file(self, salt_dir: Path) -> None:
        salt_file = salt_dir / ".secret_salt"
        assert not salt_file.exists()
        salt = _get_or_create_salt()
        assert isinstance(salt, bytes)
        assert len(salt) == 16
        assert salt_file.exists()

    def test_returns_existing_salt(self, salt_dir: Path) -> None:
        # Create salt first time
        salt1 = _get_or_create_salt()
        # Read it again
        salt2 = _get_or_create_salt()
        assert salt1 == salt2

    def test_salt_file_permissions(self, salt_dir: Path) -> None:
        """Salt file should be created with 0600 permissions."""
        _get_or_create_salt()
        salt_file = salt_dir / ".secret_salt"
        mode = oct(salt_file.stat().st_mode & 0o777)
        assert mode == "0o600"

    @pytest.mark.parametrize("invalid_salt", [b"", b"x" * 15, b"x" * 17])
    def test_rejects_invalid_salt_length(self, salt_dir: Path, invalid_salt: bytes) -> None:
        salt_file = salt_dir / ".secret_salt"
        salt_file.write_bytes(invalid_salt)

        with pytest.raises(
            InvalidSecretSaltError,
            match=rf"expected 16 bytes, found {len(invalid_salt)}",
        ):
            _get_or_create_salt()

    def test_racing_processes_converge_on_one_salt(self, salt_dir: Path) -> None:
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        result_queue = context.Queue()
        candidates = [b"a" * 16, b"b" * 16]
        processes = [
            context.Process(
                target=_create_salt_with_publish_barrier,
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
        salts = [value for _, value in results]
        assert salts[0] == salts[1]
        assert salts[0] in candidates
        assert (salt_dir / ".secret_salt").read_bytes() == salts[0]
        assert list(salt_dir.glob("..secret_salt.*.tmp")) == []

    def test_failed_write_removes_temp_file(
        self,
        salt_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fail_write(fd: int, data: Any) -> int:
            raise OSError("injected write failure")

        monkeypatch.setattr(os, "write", fail_write)

        with pytest.raises(OSError, match="injected write failure"):
            _get_or_create_salt()

        assert not (salt_dir / ".secret_salt").exists()
        assert list(salt_dir.glob("..secret_salt.*.tmp")) == []

    def test_partial_temp_write_is_never_observable_as_salt(self, salt_dir: Path) -> None:
        context = multiprocessing.get_context("spawn")
        partial_ready = context.Event()
        release_write = context.Event()
        result_queue = context.Queue()
        process = context.Process(
            target=_create_salt_with_paused_write,
            args=(str(salt_dir), partial_ready, release_write, result_queue),
        )

        try:
            process.start()
            assert partial_ready.wait(timeout=10)
            assert not (salt_dir / ".secret_salt").exists()
            temp_files = list(salt_dir.glob("..secret_salt.*.tmp"))
            assert len(temp_files) == 1
            assert temp_files[0].stat().st_size == 1

            release_write.set()
            process.join(timeout=15)
            assert process.exitcode == 0
            status, salt = result_queue.get(timeout=2)
        finally:
            release_write.set()
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            result_queue.close()
            result_queue.join_thread()

        assert status == "ok"
        assert len(salt) == 16
        assert (salt_dir / ".secret_salt").read_bytes() == salt
        assert list(salt_dir.glob("..secret_salt.*.tmp")) == []


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
    first_salt = _get_or_create_salt()
    first_kek = _get_or_create_kek_file_key()

    monkeypatch.setenv("GOBBY_HOME", str(second_home))
    second_salt = _get_or_create_salt()
    second_kek = _get_or_create_kek_file_key()

    assert (first_home / ".secret_salt").read_bytes() == first_salt
    assert (first_home / ".secret_kek").read_bytes() == first_kek
    assert (second_home / ".secret_salt").read_bytes() == second_salt
    assert (second_home / ".secret_kek").read_bytes() == second_kek
    assert first_salt != second_salt
    assert first_kek != second_kek
    assert not (fallback_home / ".gobby").exists()

    monkeypatch.setenv("GOBBY_HOME", str(first_home))
    assert _get_or_create_salt() == first_salt
    assert _get_or_create_kek_file_key() == first_kek
    monkeypatch.setenv("GOBBY_HOME", str(second_home))
    assert _get_or_create_salt() == second_salt
    assert _get_or_create_kek_file_key() == second_kek


def test_secret_material_paths_treat_blank_gobby_home_as_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-home"
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("GOBBY_HOME", " \t")

    salt = _get_or_create_salt()
    kek = _get_or_create_kek_file_key()

    assert (user_home / ".gobby" / ".secret_salt").read_bytes() == salt
    assert (user_home / ".gobby" / ".secret_kek").read_bytes() == kek


# =============================================================================
# _derive_fernet_key
# =============================================================================


class TestDeriveFernetKey:
    def test_returns_valid_fernet_key(self) -> None:
        salt = os.urandom(16)
        key = _derive_fernet_key("test-machine-id", salt)
        assert isinstance(key, bytes)
        # Fernet keys are 32 bytes base64url-encoded = 44 bytes
        assert len(key) == 44

    def test_deterministic(self) -> None:
        salt = b"fixed-salt-12345"
        key1 = _derive_fernet_key("machine-1", salt)
        key2 = _derive_fernet_key("machine-1", salt)
        assert key1 == key2

    def test_different_machine_id_different_key(self) -> None:
        salt = b"fixed-salt-12345"
        key1 = _derive_fernet_key("machine-1", salt)
        key2 = _derive_fernet_key("machine-2", salt)
        assert key1 != key2

    def test_different_salt_different_key(self) -> None:
        key1 = _derive_fernet_key("machine-1", b"salt-aaaaaaaaaa01")
        key2 = _derive_fernet_key("machine-1", b"salt-bbbbbbbbbb02")
        assert key1 != key2

    def test_key_works_with_fernet(self) -> None:
        from cryptography.fernet import Fernet

        salt = os.urandom(16)
        key = _derive_fernet_key("test-id", salt)
        f = Fernet(key)
        encrypted = f.encrypt(b"hello")
        assert f.decrypt(encrypted) == b"hello"


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

    def test_key_file_envelope_does_not_require_machine_id(
        self,
        temp_db: HubDatabase,
        salt_dir: Path,
    ) -> None:
        with patch("gobby.storage.secrets.get_machine_id", return_value=None):
            s = SecretStore(temp_db)
            assert s._get_fernet() is not None


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

    def test_set_raises_if_row_vanishes_after_upsert(
        self, store: SecretStore, temp_db: HubDatabase
    ) -> None:
        """Defensive guard: if the row is missing after upsert, raise ValueError."""
        original_fetchone = temp_db.fetchone
        call_count = 0

        def patched_fetchone(sql: str, params: tuple = ()) -> Any:
            nonlocal call_count
            call_count += 1
            if "SELECT * FROM secrets WHERE id" in sql:
                return None  # Simulate row vanishing
            return original_fetchone(sql, params)

        setattr(temp_db, "fetchone", patched_fetchone)  # noqa: B010 - monkeypatches instance method
        try:
            with pytest.raises(ValueError, match="not found after upsert"):
                store.set("VANISH", "value")
        finally:
            setattr(temp_db, "fetchone", original_fetchone)  # noqa: B010

    def test_set_encrypts_value(self, store: SecretStore, temp_db: HubDatabase) -> None:
        """The stored value in the DB should NOT be the plaintext."""
        store.set("SENSITIVE", "super-secret-value")
        row = temp_db.fetchone(
            "SELECT encrypted_value FROM secrets WHERE name = %s", ("sensitive",)
        )
        assert row is not None
        assert row["encrypted_value"] != "super-secret-value"
        assert len(row["encrypted_value"]) > 0


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
    def test_machine_id_change_does_not_break_envelope_decryption(
        self,
        temp_db: HubDatabase,
        salt_dir: Path,
    ) -> None:
        with patch("gobby.storage.secrets.get_machine_id", return_value="machine-A"):
            SecretStore(temp_db).set("KEY", "secret")

        with patch("gobby.storage.secrets.get_machine_id", return_value="machine-B"):
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

    def test_lazy_get_fernet_refuses_to_initialize_with_legacy_rows(
        self,
        temp_db: HubDatabase,
        salt_dir: Path,
    ) -> None:
        _insert_legacy_secret(temp_db, "KEY", "secret")

        with pytest.raises(RuntimeError, match="run ensure_ready"):
            SecretStore(temp_db)._get_fernet()


class TestSecretStoreLegacyMigration:
    def test_dry_run_reports_without_writing(
        self,
        temp_db: HubDatabase,
        salt_dir: Path,
    ) -> None:
        original = _insert_legacy_secret(temp_db, "KEY", "secret")

        with patch("gobby.storage.secrets.get_machine_id", return_value="machine-A"):
            report = SecretStore(temp_db).migrate_legacy_machine_id_secrets(dry_run=True)

        assert report.dry_run is True
        assert report.migrated == 1
        assert report.entries[0].status == "would_migrate"
        assert (
            temp_db.fetchone("SELECT 1 FROM secret_key_material WHERE id = %s", ("default",))
            is None
        )
        row = temp_db.fetchone("SELECT encrypted_value FROM secrets WHERE name = %s", ("key",))
        assert row is not None
        assert row["encrypted_value"] == original

    def test_migrates_legacy_machine_bound_secret(
        self,
        temp_db: HubDatabase,
        salt_dir: Path,
    ) -> None:
        original = _insert_legacy_secret(temp_db, "KEY", "secret")

        with patch("gobby.storage.secrets.get_machine_id", return_value="machine-A"):
            report = SecretStore(temp_db).migrate_legacy_machine_id_secrets()

        assert report.migrated == 1
        assert report.entries[0].status == "migrated"
        assert temp_db.fetchone("SELECT 1 FROM secret_key_material WHERE id = %s", ("default",))
        row = temp_db.fetchone("SELECT encrypted_value FROM secrets WHERE name = %s", ("key",))
        assert row is not None
        assert row["encrypted_value"] != original

        with patch("gobby.storage.secrets.get_machine_id", return_value="machine-B"):
            assert SecretStore(temp_db).get("KEY") == "secret"

    def test_required_legacy_secret_failure_raises(
        self,
        temp_db: HubDatabase,
        salt_dir: Path,
    ) -> None:
        _insert_legacy_secret(temp_db, "KEY", encrypted_value="not-a-fernet-token")

        with (
            patch("gobby.storage.secrets.get_machine_id", return_value="machine-A"),
            pytest.raises(SecretMigrationError) as exc_info,
        ):
            SecretStore(temp_db).ensure_ready(required_secret_names={"key"})

        assert exc_info.value.report.failed == 1
        assert exc_info.value.report.entries[0].required is True
        assert (
            temp_db.fetchone("SELECT 1 FROM secret_key_material WHERE id = %s", ("default",))
            is None
        )

    def test_optional_legacy_secret_failure_skips_for_reentry(
        self,
        temp_db: HubDatabase,
        salt_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _insert_legacy_secret(temp_db, "KEY", encrypted_value="not-a-fernet-token")

        with (
            patch("gobby.storage.secrets.get_machine_id", return_value="machine-A"),
            caplog.at_level("WARNING", logger="gobby.storage.secrets"),
        ):
            report = SecretStore(temp_db).ensure_ready()

        assert report.skipped == 1
        assert report.entries[0].status == "skipped"
        assert temp_db.fetchone("SELECT 1 FROM secret_key_material WHERE id = %s", ("default",))
        with pytest.raises(SecretDecryptionError):
            SecretStore(temp_db).get("KEY")
        assert any(getattr(record, "secret", "").startswith("sha256:") for record in caplog.records)
        assert "KEY" not in caplog.text

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


# =============================================================================
# SecretStore.exists
# =============================================================================


class TestSecretStoreExists:
    def test_exists_true(self, store: SecretStore) -> None:
        store.set("KEY", "value")
        assert store.exists("KEY") is True

    def test_exists_false(self, store: SecretStore) -> None:
        assert store.exists("NONEXISTENT") is False

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
