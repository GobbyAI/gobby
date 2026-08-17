"""Canonical SecretStore behavior after legacy migration removal."""

import base64
import os
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import (
    _AES_GCM_MIN_TOKEN_LEN,
    _SEAL_HKDF_INFO,
    SECRET_MATERIAL_FILENAMES,
    SecretStore,
)

pytestmark = pytest.mark.unit


def test_secret_round_trip_uses_only_canonical_key_material(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))

    SecretStore(temp_db).set("CANONICAL_SECRET", "encrypted-value")

    assert SecretStore(temp_db).get("CANONICAL_SECRET") == "encrypted-value"
    assert SECRET_MATERIAL_FILENAMES == (".secret_kek",)
    assert (tmp_path / ".secret_kek").is_file()
    assert not (tmp_path / ".secret_salt").exists()


def test_seal_uses_domain_separated_hkdf_key(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    store = SecretStore(temp_db, gobby_home=tmp_path)
    token = store.seal(b"interactive-password", aad=b"aad-v1")
    raw = base64.b64decode(token)
    dek = base64.urlsafe_b64decode(store._cached_dek())
    nonce, ciphertext = raw[:12], raw[12:]
    with pytest.raises(InvalidTag):
        AESGCM(dek).decrypt(nonce, ciphertext, b"aad-v1")
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_SEAL_HKDF_INFO,
    ).derive(dek)
    assert AESGCM(derived).decrypt(nonce, ciphertext, b"aad-v1") == b"interactive-password"
    assert store.open_sealed(token, aad=b"aad-v1") == b"interactive-password"


def test_open_sealed_rejects_truncated_token(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    store = SecretStore(temp_db, gobby_home=tmp_path)
    store.ensure_ready()
    truncated = base64.b64encode(os.urandom(_AES_GCM_MIN_TOKEN_LEN - 1)).decode("ascii")
    with pytest.raises(InvalidToken, match="truncated"):
        store.open_sealed(truncated, aad=b"aad-v1")


def test_set_kek_posture_rebinds_cached_dek(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    store = SecretStore(temp_db, gobby_home=tmp_path)
    store.ensure_ready()
    first = store._cached_dek()
    store._dek = b"stale-dek-material-should-be-replaced!!!!"
    store.set_kek_posture("key_file")
    assert store._cached_dek() == first
    token = store.seal(b"after-rebind", aad=b"aad")
    assert store.open_sealed(token, aad=b"aad") == b"after-rebind"
