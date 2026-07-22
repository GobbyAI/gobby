"""DB-first configuration storage.

Stores config key-value pairs in the hub database as flattened dotted paths.
Values are JSON-encoded so types are preserved (strings, bools, numbers, lists).

Resolution order: DB config_store > Pydantic defaults.
YAML serves as import/export only after one-time migration.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Collection
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, cast

from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_KEY_KEY,
    AI_EMBEDDING_CONFIG_KEY_SET,
    EMBEDDING_API_KEY_SECRET_NAME,
    EMBEDDING_INTERNAL_LIFECYCLE_KEYS,
    EMBEDDING_SWITCH_JOURNAL_KEY,
    embedding_config_secret_name,
    validate_embedding_storage_config_key,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from gobby.storage.secrets import SecretStore

logger = logging.getLogger(__name__)

# Suffixes that indicate a key holds a secret value
_SECRET_SUFFIXES = (
    "api_key",
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

_CONFIG_SECRET_NAMES = {
    "databases.falkordb.password": "falkordb_password",
}
_REMOVED_LLM_PROVIDERS_CONFIG_PREFIX = "llm_providers"


def _decode_value(key: str, value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON for config key {key!r}") from exc


def _validate_storage_config_key(key: str) -> None:
    """Reject config keys that no longer have a runtime config surface."""
    if key == _REMOVED_LLM_PROVIDERS_CONFIG_PREFIX or key.startswith(
        f"{_REMOVED_LLM_PROVIDERS_CONFIG_PREFIX}."
    ):
        raise ValueError(
            f"Config key '{key}' has been removed. Use feature configs and provider "
            "discovery instead."
        )
    validate_embedding_storage_config_key(key)


def config_key_to_secret_name(key: str) -> str:
    """Convert a dotted config key to a secret store name.

    Uses the last segment of the dotted key as the natural secret name.
    """
    if secret_name := _CONFIG_SECRET_NAMES.get(key):
        return secret_name
    if secret_name := embedding_config_secret_name(key):
        return secret_name
    return key.rsplit(".", 1)[-1]


def is_secret_key_name(key: str) -> bool:
    """Check if a config key name matches common secret patterns."""
    last_part = key.rsplit(".", 1)[-1].lower()
    return any(last_part.endswith(suffix) for suffix in _SECRET_SUFFIXES)


def _is_secret_reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("$secret:")


def _is_canonical_secret_reference(key: str, value: Any) -> bool:
    return isinstance(value, str) and value == f"$secret:{config_key_to_secret_name(key)}"


def _reject_plaintext_secret_value(key: str, value: Any) -> None:
    if not is_secret_key_name(key) or value in (None, ""):
        return
    if _is_secret_reference(value) and _is_canonical_secret_reference(key, value):
        return
    raise ValueError(
        f"Config key '{key}' looks like a secret. Use ConfigStore.set_secret() "
        "to store the value encrypted at rest."
    )


def _journal_run_id(value: Any) -> str | None:
    payload = value
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except (TypeError, ValueError):
            return "unknown"
    if payload is None:
        return None
    run_id = payload.get("run_id") if isinstance(payload, dict) else None
    return str(run_id) if run_id else "unknown"


class EmbeddingConfigMutationBlocked(RuntimeError):
    """Raised when public embedding config mutation races a live switch."""


@dataclass(frozen=True)
class EmbeddingSwitchJournalMutation:
    """Serialize switch admission with every canonical embedding mutation."""

    PRIORITY: ClassVar[int] = 800


def embedding_mutation_context(db: Any) -> AbstractContextManager[Any]:
    """Open the shared switch/mutation lock with a backend-neutral fallback."""
    transaction_immediate = getattr(db, "transaction_immediate", None)
    if callable(transaction_immediate):
        return cast(
            AbstractContextManager[Any],
            transaction_immediate(EmbeddingSwitchJournalMutation()),
        )
    return cast(AbstractContextManager[Any], db.transaction())


class ConfigStore:
    """Key-value config storage backed by the hub database.

    Keys are flattened dotted paths (e.g. "gobby-tasks.validation.candidates").
    Values are JSON-encoded for type preservation.
    Secret config keys have a canonical 1:1 mapping to SecretStore names via
    config_key_to_secret_name(); do not share one encrypted secret across keys.
    """

    def __init__(self, db: HubDatabase):
        self.db = db

    def _mutation_context(self) -> AbstractContextManager[Any]:
        return embedding_mutation_context(self.db)

    def get(self, key: str) -> Any | None:
        """Get a single config value, deserialized from JSON.

        Returns None if key doesn't exist.
        """
        if key in EMBEDDING_INTERNAL_LIFECYCLE_KEYS:
            return None
        row = self.db.fetchone("SELECT value FROM config_store WHERE key = %s", (key,))
        if not row:
            return None
        return _decode_value(key, row["value"])

    def get_all(self) -> dict[str, Any]:
        """Get all config entries as flat key-value pairs."""
        rows = self.db.fetchall("SELECT key, value FROM config_store")
        return {
            row["key"]: _decode_value(row["key"], row["value"])
            for row in rows
            if row["key"] not in EMBEDDING_INTERNAL_LIFECYCLE_KEYS
        }

    def set(self, key: str, value: Any, source: str = "user") -> None:
        """Upsert a single config value (JSON-encoded)."""
        _validate_storage_config_key(key)
        _reject_plaintext_secret_value(key, value)
        with self._mutation_context():
            transaction = self.db
            self._assert_embedding_mutation_allowed((key,), transaction)
            self._upsert_value(
                key,
                value,
                source=source,
                is_secret=_is_canonical_secret_reference(key, value),
                executor=transaction,
            )

    def set_many(self, entries: dict[str, Any], source: str = "user") -> int:
        """Bulk upsert config entries. Returns count of entries written."""
        for key, value in entries.items():
            _validate_storage_config_key(key)
            _reject_plaintext_secret_value(key, value)
        count = 0
        with self._mutation_context():
            transaction = self.db
            self._assert_embedding_mutation_allowed(entries, transaction)
            for key, value in entries.items():
                self._upsert_value(
                    key,
                    value,
                    source=source,
                    is_secret=_is_canonical_secret_reference(key, value),
                    executor=transaction,
                )
                count += 1
        return count

    def delete(self, key: str) -> bool:
        """Delete a non-secret key. Returns True if it existed.

        Secret keys must use ``clear_secret`` so the encrypted value is also removed.
        """
        _validate_storage_config_key(key)
        with self._mutation_context():
            transaction = self.db
            self._assert_embedding_mutation_allowed((key,), transaction)
            row = transaction.execute(
                "SELECT is_secret FROM config_store WHERE key = %s FOR UPDATE",
                (key,),
            ).fetchone()
            if not row:
                return False
            if row["is_secret"]:
                raise ValueError(f"Config key {key!r} is secret; use clear_secret")
            cursor = transaction.execute("DELETE FROM config_store WHERE key = %s", (key,))
        return bool(cursor.rowcount and cursor.rowcount > 0)

    def delete_all(
        self,
        secret_store: SecretStore | None = None,
        *,
        preserved_secret_keys: Collection[str] = (),
    ) -> int:
        """Delete all config entries and their encrypted secrets atomically.

        ``preserved_secret_keys`` keeps encrypted values that incoming config
        references will immediately reattach to new config rows.
        """
        preserved = set(preserved_secret_keys)
        with self._mutation_context():
            transaction = self.db
            self._assert_embedding_mutation_allowed(AI_EMBEDDING_CONFIG_KEY_SET, transaction)
            secret_rows = transaction.execute(
                "SELECT key FROM config_store WHERE is_secret = %s ORDER BY key",
                (True,),
            ).fetchall()
            secret_keys = [str(row["key"]) for row in secret_rows]
            cursor = transaction.execute("DELETE FROM config_store")
            for key in secret_keys:
                if secret_store is not None and key not in preserved:
                    secret_store.delete(config_key_to_secret_name(key))
        return cursor.rowcount or 0

    def delete_all_except(
        self,
        secret_store: SecretStore,
        preserved_keys: Collection[str],
    ) -> int:
        """Delete public rows except an allowlist while always preserving lifecycle state."""
        preserved = set(preserved_keys) | set(EMBEDDING_INTERNAL_LIFECYCLE_KEYS)
        with self._mutation_context():
            transaction = self.db
            rows = transaction.execute(
                "SELECT key, is_secret FROM config_store FOR UPDATE"
            ).fetchall()
            delete_keys = [str(row["key"]) for row in rows if row["key"] not in preserved]
            self._assert_embedding_mutation_allowed(delete_keys, transaction)
            if not delete_keys:
                return 0
            for row in rows:
                if row["key"] in delete_keys and row["is_secret"]:
                    secret_store.delete(config_key_to_secret_name(str(row["key"])))
            placeholders = ",".join("%s" for _ in delete_keys)
            cursor = transaction.execute(
                f"DELETE FROM config_store WHERE key IN ({placeholders})",  # nosec B608
                tuple(delete_keys),
            )
        return cursor.rowcount or 0

    def list_keys(self, prefix: str | None = None) -> list[str]:
        """List all keys, optionally filtered by prefix."""
        if prefix:
            rows = self.db.fetchall(
                "SELECT key FROM config_store WHERE key LIKE %s ORDER BY key",
                (f"{prefix}%",),
            )
        else:
            rows = self.db.fetchall("SELECT key FROM config_store ORDER BY key")
        return [row["key"] for row in rows if row["key"] not in EMBEDDING_INTERNAL_LIFECYCLE_KEYS]

    def get_internal_lifecycle(self, key: str) -> Any | None:
        """Read lifecycle state through the daemon-owner-only surface."""
        self._validate_internal_lifecycle_key(key)
        row = self.db.fetchone("SELECT value FROM config_store WHERE key = %s", (key,))
        return _decode_value(key, row["value"]) if row else None

    def set_internal_lifecycle(self, key: str, value: Any) -> None:
        """Persist daemon-owned lifecycle state outside public config surfaces."""
        self._validate_internal_lifecycle_key(key)
        new_run_id = _journal_run_id(value)
        if new_run_id in (None, "unknown"):
            raise ValueError("Embedding lifecycle journal must contain a run_id")
        with self._mutation_context():
            transaction = self.db
            active_run_id = self._active_switch_run_id(transaction)
            if active_run_id is not None and active_run_id != new_run_id:
                raise EmbeddingConfigMutationBlocked(
                    f"Embedding switch {active_run_id} already owns the lifecycle journal"
                )
            self._upsert_value(
                key,
                value,
                source="embedding_switch",
                executor=transaction,
            )

    def delete_internal_lifecycle(self, key: str, run_id: str) -> bool:
        """Delete daemon-owned lifecycle state after safe cleanup/completion."""
        self._validate_internal_lifecycle_key(key)
        with self._mutation_context():
            transaction = self.db
            active_run_id = self._active_switch_run_id(transaction)
            if active_run_id != run_id:
                raise EmbeddingConfigMutationBlocked(
                    f"Embedding switch owner mismatch: expected {active_run_id or 'none'}, "
                    f"got {run_id}"
                )
            cursor = transaction.execute("DELETE FROM config_store WHERE key = %s", (key,))
        return bool(cursor.rowcount and cursor.rowcount > 0)

    def set_embedding_switch_values(self, run_id: str, entries: dict[str, Any]) -> int:
        """Write canonical embedding values for the journal-owning switch only."""
        if not entries or not set(entries).issubset(AI_EMBEDDING_CONFIG_KEY_SET):
            raise ValueError("Embedding switch owner may write only canonical embedding values")
        with self._mutation_context():
            transaction = self.db
            active_run_id = self._active_switch_run_id(transaction)
            if active_run_id != run_id:
                raise EmbeddingConfigMutationBlocked(
                    f"Embedding switch owner mismatch: expected {active_run_id or 'none'}, "
                    f"got {run_id}"
                )
            for key, value in entries.items():
                _reject_plaintext_secret_value(key, value)
                self._upsert_value(
                    key,
                    value,
                    source="embedding_switch",
                    executor=transaction,
                )
        return len(entries)

    def set_embedding_bootstrap_values(
        self,
        entries: dict[str, Any],
        *,
        secret_store: SecretStore,
        plaintext_api_key: str | None,
        managed_collections_exist: Callable[[], bool],
    ) -> int:
        """Persist a provably fresh installer bootstrap under switch admission lock."""
        public_value_keys = AI_EMBEDDING_CONFIG_KEY_SET - {AI_EMBEDDING_API_KEY_KEY}
        if not entries or not set(entries).issubset(public_value_keys):
            raise ValueError("Embedding bootstrap contains unsupported configuration keys")
        for key, value in entries.items():
            _validate_storage_config_key(key)
            _reject_plaintext_secret_value(key, value)

        with self._mutation_context():
            transaction = self.db
            active_run_id = self._active_switch_run_id(transaction)
            if active_run_id is not None:
                raise EmbeddingConfigMutationBlocked(
                    f"Embedding switch {active_run_id} is active; installer mutation is blocked"
                )
            rows = transaction.execute("SELECT key FROM config_store").fetchall()
            existing_keys = {str(row["key"]) for row in rows}
            if existing_keys.intersection(AI_EMBEDDING_CONFIG_KEY_SET) or secret_store.exists(
                EMBEDDING_API_KEY_SECRET_NAME
            ):
                raise EmbeddingConfigMutationBlocked(
                    "Embedding configuration already exists; use the daemon switch lifecycle"
                )
            if managed_collections_exist():
                raise EmbeddingConfigMutationBlocked(
                    "Managed embedding collections already exist; use the daemon switch lifecycle"
                )

            for key, value in entries.items():
                self._upsert_value(
                    key,
                    value,
                    source="install",
                    is_secret=False,
                    executor=transaction,
                )
            if plaintext_api_key:
                secret_store.set(
                    name=EMBEDDING_API_KEY_SECRET_NAME,
                    plaintext_value=plaintext_api_key,
                    category="general",
                    description=f"Config secret for {AI_EMBEDDING_API_KEY_KEY}",
                )
                self._upsert_value(
                    AI_EMBEDDING_API_KEY_KEY,
                    f"$secret:{EMBEDDING_API_KEY_SECRET_NAME}",
                    source="install",
                    is_secret=True,
                    executor=transaction,
                )
        return len(entries) + int(bool(plaintext_api_key))

    def set_named_secret(
        self,
        secret_store: SecretStore,
        name: str,
        plaintext_value: str,
        *,
        category: str,
        description: str | None,
    ) -> Any:
        """Mutate a generic secret under embedding-switch admission control."""
        with self._mutation_context():
            transaction = self.db
            if name == EMBEDDING_API_KEY_SECRET_NAME:
                self._assert_embedding_mutation_allowed(AI_EMBEDDING_CONFIG_KEY_SET, transaction)
            return secret_store.set(
                name=name,
                plaintext_value=plaintext_value,
                category=category,
                description=description,
            )

    def delete_named_secret(self, secret_store: SecretStore, name: str) -> bool:
        """Delete a generic secret under embedding-switch admission control."""
        with self._mutation_context():
            transaction = self.db
            if name == EMBEDDING_API_KEY_SECRET_NAME:
                self._assert_embedding_mutation_allowed(AI_EMBEDDING_CONFIG_KEY_SET, transaction)
            return bool(secret_store.delete(name))

    @staticmethod
    def _validate_internal_lifecycle_key(key: str) -> None:
        if key not in EMBEDDING_INTERNAL_LIFECYCLE_KEYS:
            raise ValueError(f"Unsupported internal lifecycle key: {key}")

    def _upsert_value(
        self,
        key: str,
        value: Any,
        *,
        source: str,
        is_secret: bool = False,
        executor: Any | None = None,
    ) -> None:
        target = executor or self.db
        target.execute(
            """INSERT INTO config_store (key, value, source, is_secret, updated_at)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   source = excluded.source,
                   is_secret = excluded.is_secret,
                   updated_at = excluded.updated_at""",
            (key, json.dumps(value), source, is_secret, utc_now()),
        )

    def _assert_embedding_mutation_allowed(
        self,
        keys: Collection[str],
        transaction: Any,
    ) -> None:
        if not set(keys).intersection(AI_EMBEDDING_CONFIG_KEY_SET):
            return
        run_id = self._active_switch_run_id(transaction)
        if run_id is not None:
            raise EmbeddingConfigMutationBlocked(
                f"Embedding switch {run_id} is active; config mutation is blocked"
            )

    @staticmethod
    def _active_switch_run_id(transaction: Any) -> str | None:
        row = transaction.execute(
            "SELECT value FROM config_store WHERE key = %s FOR UPDATE",
            (EMBEDDING_SWITCH_JOURNAL_KEY,),
        ).fetchone()
        if row is None:
            return None
        raw = _decode_value(EMBEDDING_SWITCH_JOURNAL_KEY, row["value"])
        return _journal_run_id(raw)

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

        Stores ``$secret:<natural_name>`` in config_store with ``is_secret=true``.
        The actual value is encrypted in the ``secrets`` table.
        Both writes happen in a single transaction for consistency.
        """
        _validate_storage_config_key(key)
        with self._mutation_context():
            transaction = self.db
            self._assert_embedding_mutation_allowed((key,), transaction)
            secret_name = config_key_to_secret_name(key)
            ref = f"$secret:{secret_name}"
            secret_store.set(
                name=secret_name,
                plaintext_value=plaintext_value,
                category="general",
                description=f"Config secret for {key}",
            )
            self._upsert_value(
                key,
                ref,
                source=source,
                is_secret=True,
                executor=transaction,
            )

    def get_secret_keys(self) -> list[str]:
        """Return all config keys flagged as secrets."""
        rows = self.db.fetchall(
            "SELECT key FROM config_store WHERE is_secret = %s ORDER BY key",
            (True,),
        )
        return [row["key"] for row in rows]

    def mark_secret_keys(self, keys: Collection[str]) -> None:
        """Mark existing config rows as secrets."""
        if not keys:
            return
        placeholders = ",".join("%s" for _ in keys)
        self.db.execute(
            f"UPDATE config_store SET is_secret = %s WHERE key IN ({placeholders})",
            (True, *sorted(keys)),
        )

    def clear_secret(self, key: str, secret_store: SecretStore) -> None:
        """Remove a secret from both config_store and the secrets table.

        Both deletions run in a single transaction so either both succeed
        or both roll back.
        """
        _validate_storage_config_key(key)
        secret_name = config_key_to_secret_name(key)
        with self._mutation_context():
            transaction = self.db
            self._assert_embedding_mutation_allowed((key,), transaction)
            transaction.execute("DELETE FROM config_store WHERE key = %s", (key,))
            secret_store.delete(secret_name)


# =============================================================================
# Flatten / unflatten utilities
# =============================================================================


def flatten_config(config_dict: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested config dict into dotted-path keys.

    Example:
        {"gobby-tasks": {"validation": {"profile": "mid"}}}
        → {"gobby-tasks.validation.profile": "mid"}

    Lists and non-dict values are kept as leaf values.
    """
    flat: dict[str, Any] = {}
    for key, value in config_dict.items():
        full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict):
            if value:
                flat.update(flatten_config(value, full_key))
            else:
                flat[full_key] = {}
        else:
            flat[full_key] = value
    return flat


def unflatten_config(flat_dict: dict[str, Any]) -> dict[str, Any]:
    """Unflatten dotted-path keys back into a nested dict.

    Example:
        {"gobby-tasks.validation.profile": "mid"}
        → {"gobby-tasks": {"validation": {"profile": "mid"}}}
    """
    result: dict[str, Any] = {}
    terminal_paths: set[tuple[str, ...]] = set()
    for key, value in sorted(flat_dict.items(), key=lambda item: (item[0].count("."), item[0])):
        parts = key.split(".")
        path = tuple(parts)
        if any(path[:length] in terminal_paths for length in range(1, len(path))):
            raise ValueError(f"Conflicting scalar and nested config keys at {key!r}")
        terminal_paths.add(path)
        current = result
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value
    return result
