"""DB-first configuration storage.

Stores config key-value pairs in SQLite as flattened dotted paths.
Values are JSON-encoded so types are preserved (strings, bools, numbers, lists).

Resolution order: DB config_store > Pydantic defaults.
YAML serves as import/export only after one-time migration.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.storage.database import DatabaseProtocol

if TYPE_CHECKING:
    from gobby.storage.secrets import SecretStore

logger = logging.getLogger(__name__)

# Suffixes that indicate a key holds a secret value
_SECRET_SUFFIXES = (
    "_api_key",
    "_api_token",
    "_api_secret",
    "_secret",
    "_password",
    "password",
    "_access_token",
    "_auth_token",
    "_secret_key",
    "_auth",
)


def config_key_to_secret_name(key: str) -> str:
    """Convert a dotted config key to a secret store name.

    Uses the last segment of the dotted key as the natural secret name.

    Examples:
        ``voice.elevenlabs_api_key`` -> ``elevenlabs_api_key``
        ``secrets.openai_api_key`` -> ``openai_api_key``
    """
    return key.rsplit(".", 1)[-1]


def is_secret_key_name(key: str) -> bool:
    """Check if a config key name matches common secret patterns."""
    last_part = key.rsplit(".", 1)[-1].lower()
    return any(last_part.endswith(suffix) for suffix in _SECRET_SUFFIXES)


class ConfigStore:
    """Key-value config storage backed by SQLite.

    Keys are flattened dotted paths (e.g. "llm_providers.claude.models").
    Values are JSON-encoded for type preservation.
    """

    def __init__(self, db: DatabaseProtocol):
        self.db = db

    def get(self, key: str) -> Any | None:
        """Get a single config value, deserialized from JSON.

        Returns None if key doesn't exist.
        """
        row = self.db.fetchone("SELECT value FROM config_store WHERE key = ?", (key,))
        if not row:
            return None
        return json.loads(row["value"])

    def get_all(self) -> dict[str, Any]:
        """Get all config entries as flat key-value pairs."""
        rows = self.db.fetchall("SELECT key, value FROM config_store")
        return {row["key"]: json.loads(row["value"]) for row in rows}

    def set(self, key: str, value: Any, source: str = "user") -> None:
        """Upsert a single config value (JSON-encoded)."""
        now = datetime.now(UTC).isoformat()
        json_value = json.dumps(value)
        self.db.execute(
            """INSERT INTO config_store (key, value, source, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   source = excluded.source,
                   updated_at = excluded.updated_at""",
            (key, json_value, source, now),
        )

    def set_many(self, entries: dict[str, Any], source: str = "user") -> int:
        """Bulk upsert config entries. Returns count of entries written."""
        now = datetime.now(UTC).isoformat()
        count = 0
        for key, value in entries.items():
            json_value = json.dumps(value)
            self.db.execute(
                """INSERT INTO config_store (key, value, source, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value,
                       source = excluded.source,
                       updated_at = excluded.updated_at""",
                (key, json_value, source, now),
            )
            count += 1
        return count

    def delete(self, key: str) -> bool:
        """Delete a single key. Returns True if it existed."""
        cursor = self.db.execute("DELETE FROM config_store WHERE key = ?", (key,))
        return bool(cursor.rowcount and cursor.rowcount > 0)

    def delete_all(self) -> int:
        """Delete all config entries. Returns count deleted."""
        cursor = self.db.execute("DELETE FROM config_store")
        return cursor.rowcount or 0

    def list_keys(self, prefix: str | None = None) -> list[str]:
        """List all keys, optionally filtered by prefix."""
        if prefix:
            rows = self.db.fetchall(
                "SELECT key FROM config_store WHERE key LIKE ? ORDER BY key",
                (f"{prefix}%",),
            )
        else:
            rows = self.db.fetchall("SELECT key FROM config_store ORDER BY key")
        return [row["key"] for row in rows]

    # -----------------------------------------------------------------
    # Secret-aware methods
    # -----------------------------------------------------------------

    def set_secret(
        self,
        key: str,
        plaintext_value: str,
        secret_store: SecretStore,
        source: str = "user",
    ) -> None:
        """Encrypt a config value via SecretStore and store a reference.

        Stores ``$secret:<natural_name>`` in config_store with ``is_secret=1``.
        The actual value is encrypted in the ``secrets`` table.
        Both writes happen in a single transaction for consistency.
        """
        secret_name = config_key_to_secret_name(key)
        ref = f"$secret:{secret_name}"
        now = datetime.now(UTC).isoformat()
        with self.db.transaction():
            secret_store.set(
                name=secret_name,
                plaintext_value=plaintext_value,
                category="general",
                description=f"Config secret for {key}",
            )
            self.db.execute(
                """INSERT INTO config_store (key, value, source, is_secret, updated_at)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value,
                       source = excluded.source,
                       is_secret = 1,
                       updated_at = excluded.updated_at""",
                (key, json.dumps(ref), source, now),
            )

    def get_secret_keys(self) -> list[str]:
        """Return all config keys flagged as secrets."""
        rows = self.db.fetchall("SELECT key FROM config_store WHERE is_secret = 1 ORDER BY key")
        return [row["key"] for row in rows]

    def clear_secret(self, key: str, secret_store: SecretStore) -> None:
        """Remove a secret from both config_store and the secrets table.

        Both deletions run in a single transaction so either both succeed
        or both roll back.
        """
        secret_name = config_key_to_secret_name(key)
        with self.db.transaction():
            self.db.execute("DELETE FROM config_store WHERE key = ?", (key,))
            secret_store.delete(secret_name)


# =============================================================================
# Flatten / unflatten utilities
# =============================================================================


def flatten_config(config_dict: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested config dict into dotted-path keys.

    Example:
        {"llm_providers": {"claude": {"enabled": True}}}
        → {"llm_providers.claude.enabled": True}

    Lists and non-dict values are kept as leaf values.
    """
    flat: dict[str, Any] = {}
    for key, value in config_dict.items():
        full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict):
            flat.update(flatten_config(value, full_key))
        else:
            flat[full_key] = value
    return flat


def unflatten_config(flat_dict: dict[str, Any]) -> dict[str, Any]:
    """Unflatten dotted-path keys back into a nested dict.

    Example:
        {"llm_providers.claude.enabled": True}
        → {"llm_providers": {"claude": {"enabled": True}}}
    """
    result: dict[str, Any] = {}
    for key, value in flat_dict.items():
        parts = key.split(".")
        current = result
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value
    return result
