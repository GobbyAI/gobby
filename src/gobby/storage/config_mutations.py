"""Atomic compare-and-swap mutations for registry-backed configuration."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, ClassVar, cast

from pydantic import TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python

from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_KEY_KEY,
    AI_EMBEDDING_CONFIG_KEY_SET,
    EMBEDDING_SWITCH_JOURNAL_KEY,
    embedding_config_secret_name,
)
from gobby.config.registry import (
    CONFIG_REGISTRY,
    ActivationPolicy,
    ConfigPatternSpec,
    ConfigRegistry,
    ConfigSecrecy,
    ConfigVisibility,
    RegistrySpec,
    UnknownConfigKeyError,
    config_key_secrecy,
)
from gobby.storage.config_repository import (
    MAX_CONFIG_REVISION,
    ConfigReadSnapshot,
    ConfigRepository,
    registry_is_secret,
)
from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.secrets import SecretStore
from gobby.utils.datetime import utc_now

CONFIG_CHANGED_CHANNEL = "gobby_config_changed"
_CONFIG_SECRET_NAMES = {"databases.falkordb.password": "falkordb_password"}


class ConfigMutationError(RuntimeError):
    """Typed configuration mutation failure."""

    code = "config_mutation_failed"
    retryable = False


class ConfigConflictError(ConfigMutationError):
    """The mandatory expected revision is stale."""

    code = "revision_conflict"
    retryable = True

    def __init__(self, expected_revision: int, actual_revision: int) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            f"Configuration revision conflict: expected {expected_revision}, "
            f"current revision is {actual_revision}"
        )


class ConfigRevisionExhaustedError(ConfigMutationError):
    """The exact cross-language revision domain has no successor."""

    code = "revision_exhausted"
    retryable = False

    def __init__(self) -> None:
        super().__init__(f"Configuration revision {MAX_CONFIG_REVISION} cannot be advanced")


class ConfigValidationError(ConfigMutationError, ValueError):
    """A patch cannot produce a valid complete configuration snapshot."""

    code = "invalid_config"
    retryable = False

    def __init__(self, message: str, *, key: str | None = None) -> None:
        self.key = key
        super().__init__(message)


class EmbeddingConfigMutationBlocked(ConfigMutationError):
    """An active embedding switch owns canonical embedding state."""

    code = "embedding_mutation_blocked"


@dataclass(frozen=True, slots=True)
class SecretUpdate:
    """Plaintext replacement for one registry-declared secret reference."""

    plaintext: str
    name: str | None = None
    category: str = "general"
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigPatch:
    """Typed set, unset, and secret operations applied as one CAS."""

    values: Mapping[str, object] = field(default_factory=dict)
    unset: frozenset[str] = frozenset()
    secrets: Mapping[str, SecretUpdate] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConfigMutationResult:
    """Committed revision and keys whose effective state changed."""

    revision: int
    changed_keys: frozenset[str]


@dataclass(frozen=True, slots=True)
class EmbeddingSwitchJournalMutation:
    """Serialize embedding admission with every configuration mutation."""

    PRIORITY: ClassVar[int] = 800


def embedding_mutation_context(db: Any) -> AbstractContextManager[Transaction]:
    """Open the shared embedding/configuration mutation lock."""
    transaction_immediate = getattr(db, "transaction_immediate", None)
    if callable(transaction_immediate):
        return cast(
            AbstractContextManager[Transaction],
            transaction_immediate(EmbeddingSwitchJournalMutation()),
        )
    return cast(AbstractContextManager[Transaction], db.transaction())


def config_key_to_secret_name(key: str) -> str:
    """Return the canonical one-to-one SecretStore name for a config key."""
    if secret_name := _CONFIG_SECRET_NAMES.get(key):
        return secret_name
    if secret_name := embedding_config_secret_name(key):
        return secret_name
    return key.rsplit(".", 1)[-1]


class ConfigMutations:
    """Single state owner for revision-advancing configuration writes."""

    def __init__(
        self,
        db: HubDatabase,
        *,
        registry: ConfigRegistry = CONFIG_REGISTRY,
        secret_store: SecretStore | None = None,
    ) -> None:
        self.db = db
        self.registry = registry
        self.secret_store = secret_store or SecretStore(db)
        self.repository = ConfigRepository(
            db,
            registry=registry,
            secret_store=self.secret_store,
        )

    def patch(self, *, expected_revision: int, patch: ConfigPatch) -> ConfigMutationResult:
        return self._apply(
            expected_revision=expected_revision,
            patch=patch,
            source="user",
            allow_internal=False,
            namespace=None,
        )

    def patch_internal(
        self,
        *,
        expected_revision: int,
        patch: ConfigPatch,
        source: str,
        embedding_run_id: str | None = None,
    ) -> ConfigMutationResult:
        return self._apply(
            expected_revision=expected_revision,
            patch=patch,
            source=source,
            allow_internal=True,
            namespace=None,
            embedding_run_id=embedding_run_id,
        )

    def replace_namespace(
        self,
        *,
        namespace: str,
        expected_revision: int,
        patch: ConfigPatch,
    ) -> ConfigMutationResult:
        if not namespace or "." in namespace:
            raise ConfigValidationError("Configuration namespace must be one non-empty segment")
        return self._apply(
            expected_revision=expected_revision,
            patch=patch,
            source="user",
            allow_internal=False,
            namespace=namespace,
            embedding_run_id=None,
        )

    def _apply(
        self,
        *,
        expected_revision: int,
        patch: ConfigPatch,
        source: str,
        allow_internal: bool,
        namespace: str | None,
        embedding_run_id: str | None = None,
    ) -> ConfigMutationResult:
        _validate_revision(expected_revision)
        self._validate_patch_shape(patch)
        with embedding_mutation_context(self.db) as transaction:
            revision = self.repository._read_revision(transaction, lock=True)
            if revision != expected_revision:
                raise ConfigConflictError(expected_revision, revision)
            rows = self.repository._read_rows(transaction)
            snapshot = self.repository.snapshot_from_rows(transaction, revision, rows)
            effective_patch = self._scoped_patch(patch, snapshot, namespace)
            self._validate_embedding_admission(
                effective_patch,
                snapshot,
                source=source,
                embedding_run_id=embedding_run_id,
            )
            values, secret_references = self._validate_candidate(
                effective_patch,
                snapshot,
                allow_internal=allow_internal,
            )
            changed = self._effective_changes(
                effective_patch,
                values,
                secret_references,
                snapshot,
            )
            if not changed:
                return ConfigMutationResult(revision, frozenset())
            if revision == MAX_CONFIG_REVISION:
                raise ConfigRevisionExhaustedError
            next_revision = revision + 1
            self._write_changes(
                transaction,
                effective_patch,
                values,
                secret_references,
                changed,
                snapshot,
                source=source,
                revision=next_revision,
            )
            transaction.execute(
                "UPDATE config_state SET revision = %s WHERE id = %s",
                (next_revision, True),
            )
            transaction.execute(
                "SELECT pg_notify(%s, %s)",
                (CONFIG_CHANGED_CHANNEL, str(next_revision)),
            )
            return ConfigMutationResult(next_revision, frozenset(changed))

    @staticmethod
    def _validate_embedding_admission(
        patch: ConfigPatch,
        snapshot: ConfigReadSnapshot,
        *,
        source: str,
        embedding_run_id: str | None,
    ) -> None:
        keys = set(patch.values) | set(patch.secrets) | set(patch.unset)
        structural_keys = AI_EMBEDDING_CONFIG_KEY_SET - {AI_EMBEDDING_API_KEY_KEY}
        if not keys.intersection(structural_keys):
            return
        active_run_id = _journal_run_id(snapshot.overrides.get(EMBEDDING_SWITCH_JOURNAL_KEY))
        if active_run_id == "unknown":
            raise EmbeddingConfigMutationBlocked("Malformed embedding switch journal")
        if source == "embedding_switch":
            if active_run_id != embedding_run_id:
                raise EmbeddingConfigMutationBlocked(
                    f"Embedding switch owner mismatch: expected {active_run_id or 'none'}, "
                    f"got {embedding_run_id or 'none'}"
                )
            return
        if active_run_id is not None:
            raise EmbeddingConfigMutationBlocked(
                f"Embedding switch {active_run_id} is active; config mutation is blocked"
            )

    @staticmethod
    def _validate_patch_shape(patch: ConfigPatch) -> None:
        value_keys = set(patch.values)
        secret_keys = set(patch.secrets)
        duplicates = (
            (value_keys & secret_keys) | (value_keys & patch.unset) | (secret_keys & patch.unset)
        )
        if duplicates:
            raise ConfigValidationError(
                f"Configuration patch operations overlap: {sorted(duplicates)}"
            )

    def _scoped_patch(
        self,
        patch: ConfigPatch,
        snapshot: ConfigReadSnapshot,
        namespace: str | None,
    ) -> ConfigPatch:
        if namespace is None:
            return patch
        requested = set(patch.values) | set(patch.secrets) | set(patch.unset)
        outside = [key for key in requested if not self._key_in_namespace(key, namespace)]
        if outside:
            raise ConfigValidationError(
                f"Keys are outside configuration namespace {namespace!r}: {sorted(outside)}"
            )
        omitted = {
            key
            for key in snapshot.overrides
            if self._key_in_namespace(key, namespace) and key not in requested
        }
        return ConfigPatch(
            values=patch.values,
            unset=frozenset(set(patch.unset) | omitted),
            secrets=patch.secrets,
        )

    def _key_in_namespace(self, key: str, namespace: str) -> bool:
        spec = self._resolve(key)
        if namespace == "daemon":
            return spec.source_path is not None
        return key == namespace or key.startswith(f"{namespace}.")

    def _validate_candidate(
        self,
        patch: ConfigPatch,
        snapshot: ConfigReadSnapshot,
        *,
        allow_internal: bool,
    ) -> tuple[dict[str, object], dict[str, str]]:
        candidate = dict(snapshot.overrides)
        values: dict[str, object] = {}
        references: dict[str, str] = {}
        try:
            for key in patch.unset:
                spec = self._resolve(key)
                self._authorize(spec, key, allow_internal=allow_internal)
                candidate.pop(key, None)
            for key, value in patch.values.items():
                spec = self._resolve(key)
                self._authorize(spec, key, allow_internal=allow_internal)
                validated = self._validate_value(spec, key, value)
                if config_key_secrecy(spec, key) is ConfigSecrecy.REFERENCE:
                    self._validate_reference(key, validated)
                values[key] = validated
                candidate[key] = validated
            for key, update in patch.secrets.items():
                spec = self._resolve(key)
                self._authorize(spec, key, allow_internal=allow_internal)
                if config_key_secrecy(spec, key) is not ConfigSecrecy.REFERENCE:
                    raise ConfigValidationError(
                        f"Configuration key {key!r} is not a secret",
                        key=key,
                    )
                if not isinstance(update.plaintext, str):
                    raise ConfigValidationError(
                        f"Secret value for {key!r} must be a string",
                        key=key,
                    )
                name = update.name or config_key_to_secret_name(key)
                reference = f"$secret:{name}"
                self._validate_reference(key, reference, canonical=update.name is None)
                references[key] = reference
                candidate[key] = reference
            self.repository.runtime_candidate(candidate)
        except ConfigValidationError:
            raise
        except (ValidationError, ValueError, TypeError) as exc:
            raise ConfigValidationError(
                f"Complete configuration candidate is invalid: {exc}"
            ) from exc
        return values, references

    @staticmethod
    def _authorize(spec: RegistrySpec, key: str, *, allow_internal: bool) -> None:
        if allow_internal:
            return
        if spec.visibility is ConfigVisibility.RESTRICTED:
            raise ConfigValidationError(f"Configuration key {key!r} is restricted", key=key)
        if spec.activation is ActivationPolicy.MANAGED:
            raise ConfigValidationError(
                f"Configuration key {key!r} requires managed activation",
                key=key,
            )

    @staticmethod
    def _validate_value(spec: RegistrySpec, key: str, value: object) -> object:
        annotation = spec.annotation
        if isinstance(spec, ConfigPatternSpec) and spec.field_specs:
            matched = spec.match(key)
            field_name = matched.get("field") if matched else None
            field_spec = next(
                (candidate for candidate in spec.field_specs if candidate.name == field_name),
                None,
            )
            if field_spec is None:
                raise ConfigValidationError(f"Unsupported patterned configuration key {key!r}")
            annotation = field_spec.annotation
        try:
            return cast(object, TypeAdapter(annotation).validate_python(value, strict=True))
        except ValidationError as exc:
            raise ConfigValidationError(
                f"Invalid value for configuration key {key!r}: {exc}",
                key=key,
            ) from exc

    @staticmethod
    def _validate_reference(key: str, value: object, *, canonical: bool = False) -> None:
        if not isinstance(value, str) or not value.startswith("$secret:"):
            raise ConfigValidationError(
                f"Secret configuration key {key!r} requires a secret update or reference",
                key=key,
            )
        name = value.removeprefix("$secret:")
        if not name:
            raise ConfigValidationError(f"Secret reference for {key!r} is empty", key=key)
        if canonical and name != config_key_to_secret_name(key):
            raise ConfigValidationError(
                f"Secret reference for {key!r} is not canonical",
                key=key,
            )

    def _effective_changes(
        self,
        patch: ConfigPatch,
        values: dict[str, object],
        references: dict[str, str],
        snapshot: ConfigReadSnapshot,
    ) -> set[str]:
        changed = {key for key in patch.unset if key in snapshot.overrides}
        for key, value in values.items():
            if snapshot.values.get(key) != value:
                changed.add(key)
        for key, update in patch.secrets.items():
            binding = snapshot.secret_bindings.get(key)
            if (
                binding is None
                or binding.reference != references[key]
                or binding.plaintext != update.plaintext
            ):
                changed.add(key)
        return changed

    def _write_changes(
        self,
        transaction: Transaction,
        patch: ConfigPatch,
        values: dict[str, object],
        references: dict[str, str],
        changed: Collection[str],
        snapshot: ConfigReadSnapshot,
        *,
        source: str,
        revision: int,
    ) -> None:
        for key in sorted(set(patch.unset) & set(changed)):
            self._delete_secret_binding(key, snapshot)
            transaction.execute("DELETE FROM config_store WHERE key = %s", (key,))
        for key in sorted(set(values) & set(changed)):
            old_binding = snapshot.secret_bindings.get(key)
            new_value = values[key]
            if old_binding is not None and old_binding.reference != new_value:
                self.secret_store.delete(old_binding.reference.removeprefix("$secret:"))
            self._upsert(transaction, key, new_value, source, revision)
        for key in sorted(set(patch.secrets) & set(changed)):
            update = patch.secrets[key]
            reference = references[key]
            old_binding = snapshot.secret_bindings.get(key)
            self.secret_store.set(
                name=reference.removeprefix("$secret:"),
                plaintext_value=update.plaintext,
                category=update.category,
                description=update.description or f"Config secret for {key}",
            )
            self._upsert(transaction, key, reference, source, revision)
            if old_binding is not None and old_binding.reference != reference:
                self.secret_store.delete(old_binding.reference.removeprefix("$secret:"))

    def _upsert(
        self,
        transaction: Transaction,
        key: str,
        value: object,
        source: str,
        revision: int,
    ) -> None:
        spec = self._resolve(key)
        transaction.execute(
            """INSERT INTO config_store
                   (key, value, source, is_secret, revision, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (key) DO UPDATE SET
                   value = EXCLUDED.value,
                   source = EXCLUDED.source,
                   is_secret = EXCLUDED.is_secret,
                   revision = EXCLUDED.revision,
                   updated_at = EXCLUDED.updated_at""",
            (
                key,
                json.dumps(to_jsonable_python(value)),
                source,
                registry_is_secret(spec, key),
                revision,
                utc_now(),
            ),
        )

    def _delete_secret_binding(self, key: str, snapshot: ConfigReadSnapshot) -> None:
        binding = snapshot.secret_bindings.get(key)
        if binding is not None:
            self.secret_store.delete(binding.reference.removeprefix("$secret:"))

    def _resolve(self, key: str) -> RegistrySpec:
        try:
            return self.registry.resolve(key)
        except UnknownConfigKeyError as exc:
            raise ConfigValidationError(f"Unknown configuration key: {key}", key=key) from exc


def _validate_revision(revision: int) -> None:
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise ConfigValidationError("expected_revision must be an integer")
    if not 0 <= revision <= MAX_CONFIG_REVISION:
        raise ConfigValidationError(
            f"expected_revision must be between 0 and {MAX_CONFIG_REVISION}"
        )


def _journal_run_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return "unknown"
    run_id = value.get("run_id")
    return run_id if isinstance(run_id, str) and run_id else "unknown"


__all__ = [
    "CONFIG_CHANGED_CHANNEL",
    "MAX_CONFIG_REVISION",
    "ConfigConflictError",
    "ConfigMutationError",
    "ConfigMutationResult",
    "ConfigMutations",
    "ConfigPatch",
    "ConfigRevisionExhaustedError",
    "ConfigValidationError",
    "EmbeddingConfigMutationBlocked",
    "EmbeddingSwitchJournalMutation",
    "SecretUpdate",
    "config_key_to_secret_name",
    "embedding_mutation_context",
]
