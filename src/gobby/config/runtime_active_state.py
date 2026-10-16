"""Persist the active Python configuration epoch for native readers."""

from __future__ import annotations

import json
from collections.abc import Mapping

from pydantic_core import to_jsonable_python

from gobby.config.runtime_models import ConfigSnapshot, RuntimeSecretBinding
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore


def active_state_document(
    snapshot: ConfigSnapshot, *, secret_store: SecretStore, deployment_token: str
) -> dict[str, object]:
    """Serialize an epoch, sealing secret bindings in their deployment domain."""

    def bindings(values: Mapping[str, RuntimeSecretBinding]) -> dict[str, object]:
        return {
            key: {
                "reference": binding.reference,
                "fingerprint": binding.fingerprint,
                "sealed": (
                    None
                    if binding.plaintext is None
                    else secret_store.seal(
                        binding.plaintext.encode(),
                        aad=(
                            f"config-binding:{deployment_token}:{key}:{binding.fingerprint}"
                        ).encode(),
                    )
                ),
            }
            for key, binding in values.items()
        }

    return {
        "desired_values": to_jsonable_python(dict(snapshot.desired_values)),
        "active_values": to_jsonable_python(dict(snapshot.active_values)),
        "desired_overrides": to_jsonable_python(dict(snapshot.desired_overrides)),
        "pending_restart_keys": sorted(snapshot.pending_restart_keys),
        "failed_live_keys": {
            key: {"revision": failure.revision, "subscriber": failure.subscriber}
            for key, failure in snapshot.failed_live_keys.items()
        },
        "desired_bindings": bindings(snapshot.desired_bindings),
        "active_bindings": bindings(snapshot.active_bindings),
    }


class ActiveStatePublisher:
    """Publish one row per deployment without changing config repository writes."""

    def __init__(
        self, database: HubDatabase, secret_store: SecretStore, deployment_token: str
    ) -> None:
        self._database = database
        self._secret_store = secret_store
        self._deployment_token = deployment_token

    def __call__(self, snapshot: ConfigSnapshot) -> None:
        document = active_state_document(
            snapshot, secret_store=self._secret_store, deployment_token=self._deployment_token
        )
        with self._database.transaction() as conn:
            conn.execute(
                """INSERT INTO config_active_state (deployment_token, revision, state, published_at)
                VALUES (%s, %s, %s::jsonb, now())
                ON CONFLICT (deployment_token) DO UPDATE SET
                    revision = EXCLUDED.revision, state = EXCLUDED.state,
                    published_at = EXCLUDED.published_at""",
                (self._deployment_token, snapshot.revision, json.dumps(document)),
            )
