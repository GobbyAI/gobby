"""Tests for secrets CLI command wiring."""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from gobby.storage.secrets import (
    POSTURE_KEY_FILE,
    POSTURE_SCRYPT_PASSPHRASE,
    SECRET_KEK_PASSPHRASE_ENV,
)

pytestmark = pytest.mark.unit

secrets_module = importlib.import_module("gobby.cli.secrets")


def test_migrate_command_is_removed() -> None:
    assert "migrate" not in secrets_module.secrets.commands


class _StoreContext:
    def __init__(self, store: MagicMock) -> None:
        self.store = store

    def __enter__(self) -> MagicMock:
        return self.store

    def __exit__(self, *args: object) -> None:
        return None


def test_rekey_passphrase_uses_new_env(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MagicMock()
    store.current_kek_posture.side_effect = [POSTURE_KEY_FILE, POSTURE_SCRYPT_PASSPHRASE]
    monkeypatch.setattr(secrets_module, "_SecretStoreContext", lambda: _StoreContext(store))
    monkeypatch.setenv(SECRET_KEK_PASSPHRASE_ENV, "current horse")
    monkeypatch.setenv(secrets_module.NEW_SECRET_KEK_PASSPHRASE_ENV, "replacement horse")

    result = CliRunner().invoke(
        secrets_module.secrets,
        ["rekey", "--posture", "passphrase"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    store.set_kek_posture.assert_called_once_with(
        POSTURE_SCRYPT_PASSPHRASE,
        passphrase="replacement horse",
    )
    assert "key-file -> scrypt-passphrase" in result.output


def test_rekey_key_file_uses_current_passphrase(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MagicMock()
    store.current_kek_posture.side_effect = [POSTURE_SCRYPT_PASSPHRASE, POSTURE_KEY_FILE]
    monkeypatch.setattr(secrets_module, "_SecretStoreContext", lambda: _StoreContext(store))
    monkeypatch.setenv(SECRET_KEK_PASSPHRASE_ENV, "current horse")

    result = CliRunner().invoke(
        secrets_module.secrets,
        ["rekey", "--posture", "key-file"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert store.kek_passphrase == "current horse"
    store.set_kek_posture.assert_called_once_with(POSTURE_KEY_FILE, passphrase=None)
    assert "scrypt-passphrase -> key-file" in result.output
