"""Tests for the process-level legacy secret key derivation cache."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from gobby.storage import secrets as secrets_module
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_key_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setattr(secrets_module, "get_machine_id", lambda: "machine-A")
    secrets_module._clear_legacy_fernet_key_cache()
    yield
    secrets_module._clear_legacy_fernet_key_cache()


def test_two_stores_derive_legacy_key_once() -> None:
    stores = [SecretStore(MagicMock()), SecretStore(MagicMock())]

    with patch.object(
        secrets_module,
        "_derive_fernet_key_uncached",
        wraps=secrets_module._derive_fernet_key_uncached,
    ) as derive:
        ciphers = [store._legacy_fernet() for store in stores]

    assert derive.call_count == 1
    token = ciphers[0].encrypt(b"value")
    assert ciphers[1].decrypt(token) == b"value"


def test_concurrent_stores_single_flight_first_derivation() -> None:
    stores = [SecretStore(MagicMock()), SecretStore(MagicMock())]
    start = threading.Barrier(len(stores))

    def load_cipher(store: SecretStore) -> Fernet:
        start.wait(timeout=5)
        return store._legacy_fernet()

    with patch.object(
        secrets_module,
        "_derive_fernet_key_uncached",
        wraps=secrets_module._derive_fernet_key_uncached,
    ) as derive:
        with ThreadPoolExecutor(max_workers=len(stores)) as executor:
            ciphers = list(executor.map(load_cipher, stores))

    assert derive.call_count == 1
    token = ciphers[0].encrypt(b"value")
    assert ciphers[1].decrypt(token) == b"value"


@pytest.mark.asyncio
async def test_async_repeated_stores_reuse_first_derivation() -> None:
    stores = [SecretStore(MagicMock()), SecretStore(MagicMock())]
    ready = asyncio.Event()
    arrivals = 0

    async def load_cipher(store: SecretStore) -> Fernet:
        nonlocal arrivals
        arrivals += 1
        if arrivals == len(stores):
            ready.set()
        await ready.wait()
        return store._legacy_fernet()

    with patch.object(
        secrets_module,
        "_derive_fernet_key_uncached",
        wraps=secrets_module._derive_fernet_key_uncached,
    ) as derive:
        ciphers = await asyncio.gather(*(load_cipher(store) for store in stores))

    assert derive.call_count == 1
    token = ciphers[0].encrypt(b"value")
    assert ciphers[1].decrypt(token) == b"value"


def test_cache_is_bounded_and_uses_digest_keys() -> None:
    generated_key = Fernet.generate_key()
    with patch.object(
        secrets_module,
        "_derive_fernet_key_uncached",
        return_value=generated_key,
    ) as derive:
        for index in range(secrets_module._LEGACY_FERNET_KEY_CACHE_MAX_SIZE + 1):
            secrets_module._derive_fernet_key(f"machine-{index}", b"s" * 16)

        assert len(secrets_module._legacy_fernet_key_cache) == (
            secrets_module._LEGACY_FERNET_KEY_CACHE_MAX_SIZE
        )
        assert all(len(cache_key) == 32 for cache_key in secrets_module._legacy_fernet_key_cache)

        secrets_module._derive_fernet_key("machine-0", b"s" * 16)

    assert derive.call_count == secrets_module._LEGACY_FERNET_KEY_CACHE_MAX_SIZE + 2


def test_cache_key_includes_machine_id_and_salt() -> None:
    generated_key = Fernet.generate_key()
    with patch.object(
        secrets_module,
        "_derive_fernet_key_uncached",
        return_value=generated_key,
    ) as derive:
        secrets_module._derive_fernet_key("machine-A", b"a" * 16)
        secrets_module._derive_fernet_key("machine-A", b"a" * 16)
        secrets_module._derive_fernet_key("machine-A", b"b" * 16)
        secrets_module._derive_fernet_key("machine-B", b"a" * 16)

    assert derive.call_count == 3


def test_cache_can_be_cleared_between_tests() -> None:
    generated_key = Fernet.generate_key()
    with patch.object(
        secrets_module,
        "_derive_fernet_key_uncached",
        return_value=generated_key,
    ) as derive:
        secrets_module._derive_fernet_key("machine-A", b"s" * 16)
        secrets_module._derive_fernet_key("machine-A", b"s" * 16)
        secrets_module._clear_legacy_fernet_key_cache()
        secrets_module._derive_fernet_key("machine-A", b"s" * 16)

    assert derive.call_count == 2
