"""Atomic compare-and-swap mutations for registry-backed configuration."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections.abc import Collection, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, ClassVar, cast

from pydantic import TypeAdapter, ValidationError
from pydantic_core import PydanticSerializationError, to_json, to_jsonable_python

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
    config_structured_reference_fields,
)
from gobby.config.voice_secrets import validate_structured_references
from gobby.storage.config_repository import (
    MAX_CONFIG_REVISION,
    ConfigReadSnapshot,
    ConfigRepository,
    SecretBinding,
    registry_is_secret,
)
from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.secret_names import normalize_secret_name
from gobby.storage.secrets import SecretStore
from gobby.utils.datetime import utc_now

logger = logging.getLogger(__name__)

CONFIG_CHANGED_CHANNEL = "gobby_config_changed"
_CONFIG_SECRET_NAMES = {"databases.falkordb.password": "falkordb_password"}
_SECRET_NAME_SANITIZER = re.compile(r"[^a-z0-9_]+")
_MAX_CONFIG_SECRET_NAME_LENGTH = 200


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
    """Return a stable collision-resistant SecretStore name for a config key.

    Secret names are lowercase ASCII identifiers while encoded key segments are
    case-sensitive, so the sanitized readable part alone cannot be unique; a
    short digest of the exact key makes accidental collisions unlikely.
    """
    if secret_name := _CONFIG_SECRET_NAMES.get(key):
        return secret_name
    if secret_name := embedding_config_secret_name(key):
        return secret_name
    sanitized = _SECRET_NAME_SANITIZER.sub("_", key.lower()).strip("_")
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    suffix = f"_{digest}"
    readable_limit = _MAX_CONFIG_SECRET_NAME_LENGTH - len("config_") - len(suffix)
    return f"config_{sanitized[:readable_limit]}{suffix}"


def _require_json_domain(value: object) -> None:
    """Reject Python-only values before Pydantic's JSON serializer can coerce them."""
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise TypeError("non-finite floats are not valid JSON values")
    if isinstance(value, list):
        for item in value:
            _require_json_domain(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _require_json_domain(item)
        return
    raise TypeError(f"{type(value).__name__} is not a valid JSON value")


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

    def validate_named_secret(
        self,
        *,
        key: str,
        plaintext: str,
        snapshot: ConfigReadSnapshot,
    ) -> None:
        """Validate an unbound canonical secret without persisting it."""
        self._validate_candidate(
            ConfigPatch(secrets={key: SecretUpdate(plaintext)}),
            snapshot,
            allow_internal=True,
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
            revision = self.repository.read_revision(transaction, lock=True)
            if revision != expected_revision:
                raise ConfigConflictError(expected_revision, revision)
            rows = self.repository.read_rows(transaction)
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
            if self._key_in_namespace(key, namespace)
            and key not in requested
            and self._resolve(key).activation is not ActivationPolicy.MANAGED
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
        candidate_bindings = dict(snapshot.secret_bindings)
        values: dict[str, object] = {}
        references: dict[str, str] = {}
        secret_error: ConfigValidationError | None = None
        try:
            for key in patch.unset:
                spec = self._resolve(key)
                self._authorize(spec, key, allow_internal=allow_internal)
                candidate.pop(key, None)
                candidate_bindings.pop(key, None)
            for key, value in patch.values.items():
                spec = self._resolve(key)
                self._authorize(spec, key, allow_internal=allow_internal)
                validated = self._validate_value(spec, key, value)
                if config_key_secrecy(spec, key) is ConfigSecrecy.REFERENCE:
                    self._validate_reference(key, validated)
                    assert isinstance(validated, str)
                    references[key] = validated
                    existing_binding = candidate_bindings.get(key)
                    if existing_binding is None or existing_binding.reference != validated:
                        plaintext = self.secret_store.get(validated.removeprefix("$secret:"))
                        if plaintext is None:
                            raise ConfigValidationError(
                                f"Secret reference for {key!r} cannot be resolved",
                                key=key,
                            )
                        candidate_bindings[key] = SecretBinding(validated, plaintext)
                if reference_fields := config_structured_reference_fields(spec):
                    try:
                        validate_structured_references(
                            key,
                            to_jsonable_python(validated),
                            tuple(field.name for field in reference_fields),
                        )
                    except ValueError as exc:
                        raise ConfigValidationError(str(exc), key=key) from exc
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
                candidate_bindings[key] = SecretBinding(reference, update.plaintext)
            self.repository.runtime_candidate(candidate, candidate_bindings)
        except ConfigValidationError:
            raise
        except ValidationError as exc:
            detail = self._validation_detail(exc)
            error_key = self._validation_error_key(exc)
            changed_keys = (*patch.values, *patch.secrets, *patch.unset)
            if error_key is None and len(changed_keys) == 1:
                error_key = changed_keys[0]
            if error_key is not None and self._is_reference_key(error_key):
                logger.warning(
                    "Secret configuration validation failed for %s: %s", error_key, detail
                )
                secret_error = ConfigValidationError(
                    "Secret configuration value is invalid",
                    key=error_key,
                )
            else:
                raise ConfigValidationError(
                    f"Complete configuration candidate is invalid: {detail}",
                    key=error_key,
                ) from exc
        except ValueError as exc:
            changed_keys = (*patch.values, *patch.secrets, *patch.unset)
            error_key = changed_keys[0] if len(changed_keys) == 1 else None
            raise ConfigValidationError(
                f"Complete configuration candidate is invalid: {exc}",
                key=error_key,
            ) from exc
        if secret_error is not None:
            raise secret_error
        return values, references

    @staticmethod
    def _validation_detail(exc: ValidationError) -> str:
        parts: list[str] = []
        for error in exc.errors(include_input=False, include_url=False):
            path = ".".join(str(segment) for segment in error["loc"])
            parts.append(f"{path}: {error['msg']}" if path else str(error["msg"]))
        return "; ".join(parts)

    def _validation_error_key(self, exc: ValidationError) -> str | None:
        for error in exc.errors(include_input=False, include_url=False):
            key = ".".join(str(segment) for segment in error["loc"])
            try:
                self.registry.resolve(key)
            except UnknownConfigKeyError:
                continue
            return key
        return None

    def _is_reference_key(self, key: str) -> bool:
        try:
            spec = self.registry.resolve(key)
        except UnknownConfigKeyError:
            return False
        return config_key_secrecy(spec, key) is ConfigSecrecy.REFERENCE

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
            _require_json_domain(value)
        except (TypeError, ValueError) as exc:
            raise ConfigValidationError(
                f"Invalid value for configuration key {key!r}: {exc}",
                key=key,
            ) from exc
        try:
            # Values arrive as decoded JSON/YAML documents, so strictness must
            # use JSON semantics: '5' never coerces to 5, but enum-keyed maps
            # accept their canonical string keys (python-strict would reject
            # the JSON form of a key's own default).
            return cast(
                object,
                TypeAdapter(annotation).validate_json(to_json(value), strict=True),
            )
        except TypeError:
            logger.exception("Configuration type adapter failed for %s", key)
            raise
        except (ValidationError, PydanticSerializationError, ValueError) as exc:
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
        released: set[str] = set()
        retained_values = dict(snapshot.overrides)
        for key in patch.unset:
            retained_values.pop(key, None)
        retained_values.update(values)
        retained_values.update(references)

        def release_binding(key: str) -> None:
            binding = snapshot.secret_bindings.get(key)
            if binding is not None:
                name = normalize_secret_name(binding.reference.removeprefix("$secret:"))
                released.add(name)

        for key in sorted(set(patch.unset) & set(changed)):
            release_binding(key)
            transaction.execute("DELETE FROM config_store WHERE key = %s", (key,))
        for key in sorted(set(values) & set(changed)):
            old_binding = snapshot.secret_bindings.get(key)
            new_value = values[key]
            if old_binding is not None and old_binding.reference != new_value:
                release_binding(key)
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
                release_binding(key)
        self._delete_released_secrets(released, retained_values.values())

    def _delete_released_secrets(
        self,
        released: Collection[str],
        retained_values: Iterable[object],
    ) -> None:
        """Delete a released secret only when no config row still references it."""
        if not released:
            return
        retained = self.secret_store.find_persisted_secret_references(retained_values)
        for name in released:
            if name in retained:
                continue
            self.secret_store.delete(name)

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
