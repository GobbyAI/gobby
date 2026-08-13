"""DB-first configuration storage.

Stores config key-value pairs in the hub database as flattened dotted paths.
Values are JSON-encoded so types are preserved (strings, bools, numbers, lists).

Resolution order: DB config_store > Pydantic defaults.
YAML serves as import/export only after one-time migration.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection
from typing import Any

from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_KEY_KEY,
    AI_EMBEDDING_CONFIG_KEY_SET,
    EMBEDDING_API_KEY_SECRET_NAME,
    EMBEDDING_INTERNAL_LIFECYCLE_KEYS,
    EMBEDDING_SWITCH_JOURNAL_KEY,
    validate_embedding_storage_config_key,
)
from gobby.config.registry import (
    ConfigRegistry,
    ConfigSecrecy,
    UnknownConfigKeyError,
    config_key_secrecy,
    decode_dynamic_segment,
)
from gobby.storage.config_mutations import (
    ConfigConflictError,
    ConfigMutationResult,
    ConfigMutations,
    ConfigPatch,
    ConfigValidationError,
    EmbeddingConfigMutationBlocked,
    SecretUpdate,
    config_key_to_secret_name,
    embedding_mutation_context,
)
from gobby.storage.config_repository import ConfigReadSnapshot, ConfigRepository
from gobby.storage.hub._ambient import ambient_transaction
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secret_names import normalize_secret_name
from gobby.storage.secrets import SecretInfo, SecretStore

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
        f"Config key '{key}' looks like a secret. Use ConfigMutations with a SecretUpdate "
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


class ConfigStore:
    """Key-value config storage backed by the hub database.

    Keys are flattened dotted paths (e.g. "gobby-tasks.validation.candidates").
    Values are JSON-encoded for type preservation.
    Secret config keys have a canonical 1:1 mapping to SecretStore names via
    config_key_to_secret_name(); do not share one encrypted secret across keys.
    """

    def __init__(self, db: HubDatabase, *, secret_store: SecretStore | None = None):
        self.db = db
        self._secret_store = secret_store
        self.repository = ConfigRepository(db, secret_store=secret_store)
        self._mutations = (
            ConfigMutations(db, secret_store=secret_store) if secret_store is not None else None
        )

    @property
    def mutations(self) -> ConfigMutations:
        if self._mutations is None:
            secret_store = self._secret_store or SecretStore(self.db)
            self._secret_store = secret_store
            self.repository = ConfigRepository(self.db, secret_store=secret_store)
            self._mutations = ConfigMutations(self.db, secret_store=secret_store)
        return self._mutations

    def initialize(self) -> frozenset[str]:
        """Run startup registry reconciliation before the daemon becomes ready."""
        return self.repository.reconcile_registry()

    def read_snapshot(self) -> ConfigReadSnapshot:
        return self.repository.read()

    def patch(self, *, expected_revision: int, patch: ConfigPatch) -> ConfigMutationResult:
        return self.mutations.patch(expected_revision=expected_revision, patch=patch)

    def replace_namespace(
        self,
        *,
        namespace: str,
        expected_revision: int,
        patch: ConfigPatch,
    ) -> ConfigMutationResult:
        return self.mutations.replace_namespace(
            namespace=namespace,
            expected_revision=expected_revision,
            patch=patch,
        )

    def _bind_secret_store(self, secret_store: SecretStore) -> ConfigMutations:
        if secret_store is self._secret_store:
            return self.mutations
        self._secret_store = secret_store
        self.repository = ConfigRepository(self.db, secret_store=secret_store)
        self._mutations = ConfigMutations(self.db, secret_store=secret_store)
        return self._mutations

    def _apply_internal(
        self,
        patch: ConfigPatch,
        *,
        source: str,
        secret_store: SecretStore | None = None,
        embedding_run_id: str | None = None,
        expected_revision: int | None = None,
        retry_on_conflict: bool = False,
    ) -> ConfigMutationResult:
        """Apply one internal patch.

        Callers that decided on a snapshot must thread that snapshot's revision
        as ``expected_revision`` so the CAS protects the decision; convenience
        setters without a decision snapshot pass ``retry_on_conflict`` instead
        and replay the same patch against one freshly read revision. Ambient
        transactions use one snapshot and therefore make one attempt.

        This differs from ``gobby.cli.config_writes.apply_cas_config_patch``,
        which rebuilds its patch from each freshly read snapshot.
        """
        structural_keys = AI_EMBEDDING_CONFIG_KEY_SET - {AI_EMBEDDING_API_KEY_KEY}
        patch_keys = set(patch.values) | set(patch.unset) | set(patch.secrets)
        if source not in {"embedding_switch", "install"} and patch_keys.intersection(
            structural_keys
        ):
            key = sorted(patch_keys.intersection(structural_keys))[0]
            raise ConfigValidationError(
                f"Configuration key {key!r} requires managed activation",
                key=key,
            )
        mutations = self._bind_secret_store(secret_store) if secret_store else self.mutations
        attempts = (
            2
            if retry_on_conflict
            and expected_revision is None
            and ambient_transaction(self.db) is None
            else 1
        )
        for attempt in range(attempts):
            revision = (
                expected_revision
                if expected_revision is not None
                else mutations.repository.current_revision()
            )
            try:
                return mutations.patch_internal(
                    expected_revision=revision,
                    patch=patch,
                    source=source,
                    embedding_run_id=embedding_run_id,
                )
            except ConfigConflictError:
                if attempt + 1 >= attempts:
                    raise
        raise AssertionError("unreachable")

    def get_internal_lifecycle(self, key: str) -> Any | None:
        """Read lifecycle state through the daemon-owner-only surface."""
        self._validate_internal_lifecycle_key(key)
        return self.repository.read(resolve_secrets=False).overrides.get(key)

    def set_internal_lifecycle(self, key: str, value: Any) -> None:
        """Persist daemon-owned lifecycle state outside public config surfaces."""
        self._validate_internal_lifecycle_key(key)
        new_run_id = _journal_run_id(value)
        if new_run_id in (None, "unknown"):
            raise ValueError("Embedding lifecycle journal must contain a run_id")
        with embedding_mutation_context(self.db) as transaction:
            active_run_id = self._active_switch_run_id(transaction)
            if active_run_id is not None and active_run_id != new_run_id:
                raise EmbeddingConfigMutationBlocked(
                    f"Embedding switch {active_run_id} already owns the lifecycle journal"
                )
            self._apply_internal(
                ConfigPatch(values={key: value}),
                source="embedding_switch",
                embedding_run_id=new_run_id,
                expected_revision=self.repository.read_revision(transaction, lock=True),
            )

    def delete_internal_lifecycle(self, key: str, run_id: str) -> bool:
        """Delete daemon-owned lifecycle state after safe cleanup/completion."""
        self._validate_internal_lifecycle_key(key)
        with embedding_mutation_context(self.db) as transaction:
            active_run_id = self._active_switch_run_id(transaction)
            if active_run_id != run_id:
                raise EmbeddingConfigMutationBlocked(
                    f"Embedding switch owner mismatch: expected {active_run_id or 'none'}, "
                    f"got {run_id}"
                )
            result = self._apply_internal(
                ConfigPatch(unset=frozenset({key})),
                source="embedding_switch",
                embedding_run_id=run_id,
                expected_revision=self.repository.read_revision(transaction, lock=True),
            )
        return key in result.changed_keys

    def set_embedding_switch_values(self, run_id: str, entries: dict[str, Any]) -> int:
        """Write canonical embedding values for the journal-owning switch only."""
        if not entries or not set(entries).issubset(AI_EMBEDDING_CONFIG_KEY_SET):
            raise ValueError("Embedding switch owner may write only canonical embedding values")
        with embedding_mutation_context(self.db) as transaction:
            active_run_id = self._active_switch_run_id(transaction)
            if active_run_id != run_id:
                raise EmbeddingConfigMutationBlocked(
                    f"Embedding switch owner mismatch: expected {active_run_id or 'none'}, "
                    f"got {run_id}"
                )
            for key, value in entries.items():
                _reject_plaintext_secret_value(key, value)
            result = self._apply_internal(
                ConfigPatch(values=entries),
                source="embedding_switch",
                embedding_run_id=run_id,
                expected_revision=self.repository.read_revision(transaction, lock=True),
            )
        return len(result.changed_keys)

    def complete_embedding_switch(
        self,
        run_id: str,
        entries: dict[str, Any],
        completed_key: str,
        completed_record: dict[str, object],
    ) -> int:
        """Commit structural values and durable completion state in one revision.

        The revision read and the CAS share one ambient mutation transaction so
        no concurrent commit can slip between the decision and the write.
        """
        if not entries or not set(entries).issubset(AI_EMBEDDING_CONFIG_KEY_SET):
            raise ValueError("Embedding switch owner may write only canonical embedding values")
        with embedding_mutation_context(self.db) as transaction:
            revision = self.repository.read_revision(transaction, lock=True)
            record = dict(completed_record)
            record["committed_revision"] = revision + 1
            result = self.mutations.patch_internal(
                expected_revision=revision,
                patch=ConfigPatch(
                    values={**entries, completed_key: record},
                    unset=frozenset({EMBEDDING_SWITCH_JOURNAL_KEY}),
                ),
                source="embedding_switch",
                embedding_run_id=run_id,
            )
        return result.revision

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

        self._bind_secret_store(secret_store)
        with embedding_mutation_context(self.db) as transaction:
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

            secrets = (
                {AI_EMBEDDING_API_KEY_KEY: SecretUpdate(plaintext_api_key)}
                if plaintext_api_key
                else {}
            )
            result = self._apply_internal(
                ConfigPatch(values=entries, secrets=secrets),
                source="install",
                secret_store=secret_store,
                expected_revision=self.repository.read_revision(transaction, lock=True),
            )
        return len(result.changed_keys)

    def set_named_secret(
        self,
        secret_store: SecretStore,
        name: str,
        plaintext_value: str,
        *,
        category: str,
        description: str | None,
    ) -> SecretInfo:
        """Mutate a generic secret under embedding-switch admission control."""
        mutations = self._bind_secret_store(secret_store)
        normalized_name = normalize_secret_name(name)
        with embedding_mutation_context(self.db) as transaction:
            if normalized_name == EMBEDDING_API_KEY_SECRET_NAME:
                self._assert_embedding_mutation_allowed(AI_EMBEDDING_CONFIG_KEY_SET, transaction)
            revision = self.repository.read_revision(transaction, lock=True)
            rows = self.repository.read_rows(transaction)
            snapshot = self.repository.snapshot_from_rows(transaction, revision, rows)
            referencing: set[str] = set()
            for key, value in snapshot.overrides.items():
                if normalized_name not in secret_store.find_secret_references((value,)):
                    continue
                spec = mutations.registry.resolve(key)
                if config_key_secrecy(spec, key) is ConfigSecrecy.REFERENCE:
                    referencing.add(key)
            if referencing:
                self._apply_internal(
                    ConfigPatch(
                        secrets={
                            key: SecretUpdate(
                                plaintext_value,
                                name=normalized_name,
                                category=category,
                                description=description,
                            )
                            for key in referencing
                        }
                    ),
                    source="user",
                    secret_store=secret_store,
                    expected_revision=snapshot.revision,
                )
                info = next(
                    (item for item in secret_store.list() if item.name == normalized_name),
                    None,
                )
                if info is None:
                    raise RuntimeError(f"Secret mutation did not persist {normalized_name!r}")
                return info
            canonical_key = next(
                (
                    spec.key
                    for spec in mutations.registry.key_specs
                    if config_key_secrecy(spec, spec.key) is ConfigSecrecy.REFERENCE
                    and config_key_to_secret_name(spec.key) == normalized_name
                ),
                None,
            )
            if canonical_key is not None:
                mutations.validate_named_secret(
                    key=canonical_key,
                    plaintext=plaintext_value,
                    snapshot=snapshot,
                )
            return secret_store.set(
                name=normalized_name,
                plaintext_value=plaintext_value,
                category=category,
                description=description,
            )

    def delete_named_secret(self, secret_store: SecretStore, name: str) -> bool:
        """Delete a generic secret under embedding-switch admission control."""
        self._bind_secret_store(secret_store)
        normalized_name = normalize_secret_name(name)
        with embedding_mutation_context(self.db) as transaction:
            if normalized_name == EMBEDDING_API_KEY_SECRET_NAME:
                self._assert_embedding_mutation_allowed(AI_EMBEDDING_CONFIG_KEY_SET, transaction)
            if normalized_name in secret_store.find_persisted_secret_references():
                return False
            return bool(secret_store.delete(normalized_name))

    @staticmethod
    def _validate_internal_lifecycle_key(key: str) -> None:
        if key not in EMBEDDING_INTERNAL_LIFECYCLE_KEYS:
            raise ValueError(f"Unsupported internal lifecycle key: {key}")

    def _assert_embedding_mutation_allowed(
        self,
        keys: Collection[str],
        transaction: Any,
    ) -> None:
        structural_keys = AI_EMBEDDING_CONFIG_KEY_SET - {AI_EMBEDDING_API_KEY_KEY}
        if not set(keys).intersection(structural_keys):
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
        try:
            raw = _decode_value(EMBEDDING_SWITCH_JOURNAL_KEY, row["value"])
        except ValueError as exc:
            raise EmbeddingConfigMutationBlocked("Malformed embedding switch journal") from exc
        run_id = _journal_run_id(raw)
        if run_id == "unknown":
            raise EmbeddingConfigMutationBlocked("Malformed embedding switch journal")
        return run_id

    # -----------------------------------------------------------------
    # Secret-aware methods
    # -----------------------------------------------------------------


# =============================================================================
# Flatten / unflatten utilities
# =============================================================================


def flatten_config(
    config_dict: dict[str, Any],
    prefix: str = "",
    *,
    registry: ConfigRegistry | None = None,
) -> dict[str, Any]:
    """Flatten a nested config dict into dotted-path keys.

    Example:
        {"gobby-tasks": {"validation": {"profile": "mid"}}}
        → {"gobby-tasks.validation.profile": "mid"}

    Lists and non-dict values are kept as leaf values. With a registry, map
    keys sitting at a dynamic position of a matching pattern prefix must be
    canonically encoded segments; raw-dot or malformed keys would otherwise
    splice into a different placeholder split when joined.
    """
    flat: dict[str, Any] = {}
    prefix_parts = tuple(prefix.split(".")) if prefix else ()
    for key, value in config_dict.items():
        if registry is not None and registry.dynamic_segment_follows(prefix_parts):
            try:
                decode_dynamic_segment(key)
            except ValueError as exc:
                location = repr(prefix) if prefix else "<root>"
                raise ValueError(
                    f"Dynamic configuration segment {key!r} under {location} "
                    f"is not canonically encoded: {exc}"
                ) from exc
        full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict):
            if registry is not None and _is_registered_leaf(registry, full_key):
                # An object-valued registered key is a leaf: recursing would
                # splice its fields into unregistered dotted keys.
                flat[full_key] = value
            elif value:
                flat.update(flatten_config(value, full_key, registry=registry))
            else:
                flat[full_key] = {}
        else:
            flat[full_key] = value
    return flat


def _is_registered_leaf(registry: ConfigRegistry, key: str) -> bool:
    try:
        registry.resolve(key)
    except UnknownConfigKeyError:
        return False
    return True


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
