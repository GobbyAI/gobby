"""Tests for secrets store with real Fernet encryption and PostgreSQL.

Uses temp_db fixture for real database operations and mock_machine_id
for deterministic key derivation. Only external I/O (machine ID lookup)
is mocked.
"""

from __future__ import annotations

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
    SecretInfo,
    SecretKeyUnavailable,
    SecretMigrationError,
    SecretStore,
    _derive_fernet_key,
    _get_or_create_kek_file_key,
    _get_or_create_salt,
)

pytestmark = pytest.mark.unit


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def salt_dir(tmp_path: Path) -> Path:
    """Provide temp secret key files, patching SALT_FILE and KEK_FILE."""
    salt_file = tmp_path / ".secret_salt"
    kek_file = tmp_path / ".secret_kek"
    with (
        patch("gobby.storage.secrets.SALT_FILE", salt_file),
        patch("gobby.storage.secrets.KEK_FILE", kek_file),
    ):
        yield tmp_path
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

    def test_get_invalid_token_returns_none(self, temp_db: HubDatabase, salt_dir: Path) -> None:
        """Corrupt envelope tokens fail gracefully."""
        store = SecretStore(temp_db)
        store.set("KEY", "secret")
        temp_db.execute(
            "UPDATE secrets SET encrypted_value = %s WHERE name = %s",
            ("not-a-fernet-token", "key"),
        )

        assert SecretStore(temp_db).get("KEY") is None

    def test_get_invalid_token_logs_safe_identifier(
        self,
        temp_db: HubDatabase,
        salt_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Decrypt failures log a deterministic hash, not the secret name."""
        store = SecretStore(temp_db)
        store.set("KEY", "secret")
        temp_db.execute(
            "UPDATE secrets SET encrypted_value = %s WHERE name = %s",
            ("not-a-fernet-token", "key"),
        )

        with caplog.at_level("ERROR", logger="gobby.storage.secrets"):
            assert SecretStore(temp_db).get("KEY") is None

        assert any(getattr(record, "secret", "").startswith("sha256:") for record in caplog.records)
        assert "KEY" not in caplog.text

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
        assert SecretStore(temp_db).get("KEY") is None
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

    def test_resolve_unresolved_stays(self, store: SecretStore) -> None:
        result = store.resolve("Bearer $secret:MISSING_KEY")
        assert result == "Bearer $secret:MISSING_KEY"

    def test_resolve_missing_reference_logs_safe_identifier(
        self,
        store: SecretStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("WARNING", logger="gobby.storage.secrets"):
            result = store.resolve("Bearer $secret:MISSING_KEY")

        assert result == "Bearer $secret:MISSING_KEY"
        assert "sha256:" in caplog.text
        assert "MISSING_KEY" not in caplog.text

    def test_resolve_no_refs(self, store: SecretStore) -> None:
        result = store.resolve("plain text no refs")
        assert result == "plain text no refs"

    def test_resolve_empty_string(self, store: SecretStore) -> None:
        assert store.resolve("") == ""

    def test_resolve_mixed_found_and_missing(self, store: SecretStore) -> None:
        store.set("FOUND", "value")
        result = store.resolve("$secret:FOUND and $secret:MISSING")
        assert result == "value and $secret:MISSING"


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
