"""Projection helpers for immutable runtime configuration snapshots."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from types import MappingProxyType

from gobby.config.runtime_contracts import StoredConfigSnapshot, StoredSecretBinding
from gobby.config.runtime_models import ConfigSnapshot, RuntimeSecretBinding


def runtime_bindings(
    bindings: Mapping[str, StoredSecretBinding],
) -> MappingProxyType[str, RuntimeSecretBinding]:
    captured = {
        key: RuntimeSecretBinding(
            reference=binding.reference,
            plaintext=binding.plaintext,
            fingerprint=_secret_fingerprint(binding.plaintext),
        )
        for key, binding in bindings.items()
    }
    return MappingProxyType(captured)


def changed_keys(
    current: ConfigSnapshot,
    stored: StoredConfigSnapshot,
) -> frozenset[str]:
    desired_bindings = runtime_bindings(stored.secret_bindings)
    keys = (
        set(current._desired_values)
        | set(stored.values)
        | set(current.row_revisions)
        | set(stored.row_revisions)
        | set(current._desired_bindings)
        | set(desired_bindings)
    )
    changed = {
        key
        for key in keys
        if current._desired_values.get(key) != stored.values.get(key)
        or current.row_revisions.get(key) != stored.row_revisions.get(key)
        or _binding_fingerprint(current._desired_bindings.get(key))
        != _binding_fingerprint(desired_bindings.get(key))
    }
    return frozenset(changed)


def _secret_fingerprint(plaintext: str | None) -> str:
    payload = b"\x00" if plaintext is None else b"\x01" + plaintext.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _binding_fingerprint(binding: RuntimeSecretBinding | None) -> str | None:
    return None if binding is None else binding.fingerprint
