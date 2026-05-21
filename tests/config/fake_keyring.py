"""Test helpers for bootstrap keyring-backed PostgreSQL credentials."""

from __future__ import annotations

import sys

import pytest

KEYRING_SERVICE = "gobby"
DATABASE_URL_KEY = "postgres_database_url"
DATABASE_URL_REF = f"keyring:{KEYRING_SERVICE}:{DATABASE_URL_KEY}"


class FakeKeyring:
    def __init__(self, initial: dict[tuple[str, str], str] | None = None) -> None:
        self.passwords = dict(initial or {})
        self.get_calls: list[tuple[str, str]] = []
        self.set_calls: list[tuple[str, str, str]] = []

    def get_password(self, service: str, username: str) -> str | None:
        self.get_calls.append((service, username))
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.set_calls.append((service, username, password))
        self.passwords[(service, username)] = password


def install_fake_keyring(monkeypatch: pytest.MonkeyPatch, fake_keyring: FakeKeyring) -> None:
    from gobby.config import bootstrap as bootstrap_module

    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
    monkeypatch.setattr(bootstrap_module, "keyring", fake_keyring, raising=False)
