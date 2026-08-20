"""Validate-first daemon configuration document replacement."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

import yaml

from gobby.config.app import DaemonConfig
from gobby.config.registry import (
    CONFIG_REGISTRY,
    ActivationPolicy,
    ConfigRegistry,
    ConfigSecrecy,
    ConfigVisibility,
    UnknownConfigKeyError,
    config_key_secrecy,
    config_structured_identity_field,
    config_structured_reference_fields,
)
from gobby.config.runtime import ConfigSnapshot
from gobby.config.secret_mask import MASKED_SECRET
from gobby.config.values import (
    ConfigValuesError,
    clear_stale_generation_endpoint_probe_evidence,
    reject_unprobed_responses_endpoints,
)
from gobby.config.voice_secrets import (
    mask_structured_references,
    restore_masked_structured_references,
    validate_structured_references,
)
from gobby.storage.config_mutations import (
    ConfigConflictError,
    ConfigMutationResult,
    ConfigPatch,
    ConfigRevisionExhaustedError,
    ConfigValidationError,
    EmbeddingConfigMutationBlocked,
    SecretUpdate,
)
from gobby.storage.config_repository import MAX_CONFIG_REVISION, ConfigRepositoryError
from gobby.storage.config_store import flatten_config, unflatten_config

DAEMON_NAMESPACE = "daemon"


class ConfigDocumentRuntime(Protocol):
    @property
    def snapshot(self) -> ConfigSnapshot: ...

    async def reconcile_local_commit(self, revision: int) -> ConfigSnapshot: ...


class ConfigDocumentMutations(Protocol):
    def replace_namespace(
        self,
        *,
        namespace: str,
        expected_revision: int,
        patch: ConfigPatch,
    ) -> ConfigMutationResult: ...


class RunBlocking(Protocol):
    def __call__[T](self, operation: Callable[[], T]) -> Awaitable[T]: ...


class ConfigDocumentsService:
    """Parse, validate, replace, and export daemon-owned configuration."""

    def __init__(
        self,
        *,
        runtime: ConfigDocumentRuntime,
        mutations: ConfigDocumentMutations,
        runtime_candidate: Callable[[dict[str, object]], DaemonConfig],
        resolve_secret: Callable[[str], str | None],
        run_blocking: RunBlocking,
        registry: ConfigRegistry = CONFIG_REGISTRY,
    ) -> None:
        self.runtime = runtime
        self.mutations = mutations
        self.runtime_candidate = runtime_candidate
        self.resolve_secret = resolve_secret
        self.run_blocking = run_blocking
        self.registry = registry

    async def replace_yaml(
        self,
        *,
        expected_revision: int,
        content: str,
    ) -> dict[str, object]:
        """Validate a full YAML candidate before one daemon-namespace CAS."""
        self._validate_revision(expected_revision)
        snapshot = self._runtime_snapshot()
        patch, restored_masked_reference = await self.run_blocking(
            lambda: self._prepare(content, snapshot)
        )
        if restored_masked_reference and snapshot.revision != expected_revision:
            # Masked restoration reads the snapshot, so it must share the CAS epoch.
            raise ConfigValuesError(
                "revision_conflict",
                "Configuration revision is stale",
                ("expected_revision",),
                status_code=409,
                retryable=True,
                expected_revision=expected_revision,
                actual_revision=snapshot.revision,
            )
        try:
            result = await self.run_blocking(
                lambda: self.mutations.replace_namespace(
                    namespace=DAEMON_NAMESPACE,
                    expected_revision=expected_revision,
                    patch=patch,
                )
            )
        except ConfigConflictError as exc:
            raise ConfigValuesError(
                "revision_conflict",
                "Configuration revision is stale",
                ("expected_revision",),
                status_code=409,
                retryable=True,
                expected_revision=exc.expected_revision,
                actual_revision=exc.actual_revision,
            ) from exc
        except ConfigRevisionExhaustedError as exc:
            raise ConfigValuesError(
                "revision_exhausted",
                "Configuration revision cannot be advanced",
                ("expected_revision",),
            ) from exc
        except ConfigValidationError as exc:
            raise ConfigValuesError(
                "validation_error",
                str(exc),
                tuple(exc.key.split(".")) if exc.key else (),
            ) from exc
        except EmbeddingConfigMutationBlocked as exc:
            raise ConfigValuesError(
                "embedding_mutation_blocked",
                str(exc),
                ("content",),
                status_code=409,
                retryable=True,
            ) from exc
        return await self._reconcile(result)

    async def export_yaml(self) -> dict[str, object]:
        """Export one desired daemon snapshot with secret references masked."""
        snapshot = self._runtime_snapshot()
        content = await self.run_blocking(lambda: self._dump(snapshot))
        return {"revision": snapshot.revision, "content": content}

    def _runtime_snapshot(self) -> ConfigSnapshot:
        try:
            return self.runtime.snapshot
        except RuntimeError as exc:
            raise ConfigValuesError(
                "runtime_unavailable",
                "Configuration runtime is not ready",
                (),
                status_code=503,
                retryable=True,
            ) from exc

    def _prepare(self, content: str, snapshot: ConfigSnapshot) -> tuple[ConfigPatch, bool]:
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ConfigValuesError(
                "validation_error",
                f"Invalid YAML: {exc}",
                ("content",),
            ) from exc
        if parsed is None:
            parsed = {}
        if not isinstance(parsed, dict):
            raise ConfigValuesError(
                "validation_error",
                "YAML document must be a mapping",
                ("content",),
            )
        self._validate_mapping_keys(parsed)
        try:
            flat = flatten_config(parsed, registry=self.registry)
        except ValueError as exc:
            raise ConfigValuesError(
                "validation_error",
                str(exc),
                ("content",),
            ) from exc
        values: dict[str, object] = {}
        secrets: dict[str, SecretUpdate] = {}
        validation_values: dict[str, object] = {}
        wire_values: dict[str, object] = {}
        restored_masked_reference = False
        for key, value in flat.items():
            if not isinstance(key, str):
                raise self._invalid_key(str(key), "Configuration keys must be strings")
            try:
                spec = self.registry.resolve(key)
            except UnknownConfigKeyError as exc:
                raise self._invalid_key(key, f"Unknown configuration key {key!r}") from exc
            if spec.source_path is None or spec.visibility is ConfigVisibility.RESTRICTED:
                raise self._invalid_key(key, f"Configuration key {key!r} is not daemon-owned")
            if spec.activation is ActivationPolicy.MANAGED:
                raise self._invalid_key(
                    key,
                    f"Configuration key {key!r} requires managed activation",
                )
            if config_key_secrecy(spec, key) is ConfigSecrecy.REFERENCE:
                restored_masked_reference = (
                    self._prepare_secret(
                        key=key,
                        value=value,
                        snapshot=snapshot,
                        values=values,
                        secrets=secrets,
                        validation_values=validation_values,
                        wire_values=wire_values,
                    )
                    or restored_masked_reference
                )
                continue
            if reference_fields := config_structured_reference_fields(spec):
                fields = tuple(field.name for field in reference_fields)
                restored_masked_reference = (
                    self._has_masked_structured_reference(value, fields)
                    or restored_masked_reference
                )
                value = self._prepare_structured_references(
                    key,
                    value,
                    snapshot,
                    fields,
                    config_structured_identity_field(spec).name,
                )
            values[key] = value
            validation_values[key] = value
            wire_values[key] = value
        changed_values = {
            key: value
            for key, value in wire_values.items()
            if key not in snapshot.desired_values or snapshot.desired_values[key] != value
        }
        omitted = frozenset(set(snapshot.desired_values) - set(wire_values))
        cleared = clear_stale_generation_endpoint_probe_evidence(
            values,
            desired=snapshot.desired,
            secret_keys=secrets,
            probe_verified=False,
        )
        for key in cleared:
            validation_values[key] = None
            wire_values[key] = None
        reject_unprobed_responses_endpoints(
            changed_values,
            omitted,
            {key: tuple(key.split(".")) for key in (*changed_values, *omitted)},
            snapshot.desired,
            document=True,
        )
        try:
            self.runtime_candidate(validation_values)
        except (ConfigRepositoryError, ValueError, TypeError) as exc:
            raise ConfigValuesError(
                "validation_error",
                f"Complete configuration candidate is invalid: {exc}",
                ("content",),
            ) from exc
        return ConfigPatch(values=values, secrets=secrets), restored_masked_reference

    @staticmethod
    def _has_masked_structured_reference(value: object, reference_fields: tuple[str, ...]) -> bool:
        return isinstance(value, list) and any(
            isinstance(item, dict)
            and any(item.get(field) == MASKED_SECRET for field in reference_fields)
            for item in value
        )

    def _prepare_structured_references(
        self,
        key: str,
        value: object,
        snapshot: ConfigSnapshot,
        reference_fields: tuple[str, ...],
        identity_field: str,
    ) -> object:
        """Restore masked structured fields and reject plaintext on import."""
        try:
            value = restore_masked_structured_references(
                key,
                value,
                snapshot.desired_values.get(key),
                reference_fields,
                identity_field,
            )
            validate_structured_references(key, value, reference_fields)
        except ValueError as exc:
            raise self._invalid_key(key, str(exc)) from exc
        return value

    def _prepare_secret(
        self,
        *,
        key: str,
        value: object,
        snapshot: ConfigSnapshot,
        values: dict[str, object],
        secrets: dict[str, SecretUpdate],
        validation_values: dict[str, object],
        wire_values: dict[str, object],
    ) -> bool:
        if value in (None, ""):
            # An unset secret contributes no override: the namespace replace
            # clears any stored reference, and the projection default applies.
            validation_values[key] = None
            wire_values[key] = None
            return False
        if not isinstance(value, str):
            raise self._invalid_key(key, "Secret configuration value must be a string")
        if value == MASKED_SECRET:
            reference = snapshot.desired_values.get(key)
            if reference in (None, ""):
                validation_values[key] = None
                wire_values[key] = reference
                return False
            if not isinstance(reference, str):
                raise self._invalid_key(key, "Masked secret has no persisted value")
            if not reference.startswith("$secret:"):
                secrets[key] = SecretUpdate(plaintext=reference, category="general")
                validation_values[key] = reference
                wire_values[key] = reference
                return True
            plaintext = snapshot.desired_secret(key)
            if plaintext is None:
                raise self._invalid_key(key, "Persisted secret cannot be resolved")
            values[key] = reference
            validation_values[key] = plaintext
            wire_values[key] = reference
            return True
        if value.startswith("$secret:"):
            name = value.removeprefix("$secret:")
            plaintext = self.resolve_secret(name)
            if not name or plaintext is None:
                raise self._invalid_key(key, "Secret reference cannot be resolved")
            values[key] = value
            validation_values[key] = plaintext
            wire_values[key] = value
            return False
        secrets[key] = SecretUpdate(plaintext=value, category="general")
        validation_values[key] = value
        wire_values[key] = value
        return False

    def _dump(self, snapshot: ConfigSnapshot) -> str:
        values: dict[str, object] = {}
        for key, value in snapshot.desired_values.items():
            try:
                spec = self.registry.resolve(key)
            except UnknownConfigKeyError:
                continue
            if (
                spec.source_path is None
                or spec.visibility is ConfigVisibility.RESTRICTED
                or spec.activation is ActivationPolicy.MANAGED
            ):
                continue
            if config_key_secrecy(spec, key) is ConfigSecrecy.REFERENCE:
                values[key] = MASKED_SECRET if value not in (None, "") else value
            elif reference_fields := config_structured_reference_fields(spec):
                values[key] = mask_structured_references(
                    value,
                    tuple(field.name for field in reference_fields),
                )
            else:
                values[key] = value
        return yaml.safe_dump(
            unflatten_config(values),
            default_flow_style=False,
            sort_keys=True,
        )

    async def _reconcile(self, result: ConfigMutationResult) -> dict[str, object]:
        try:
            snapshot = await self.runtime.reconcile_local_commit(result.revision)
        except Exception:
            return {
                "committed": True,
                "revision": result.revision,
                "changed_keys": sorted(result.changed_keys),
                "apply_status": "reconcile_failed",
                "pending_restart_keys": [],
                "failed_live_keys": {
                    key: {"revision": result.revision, "subscriber": "runtime"}
                    for key in sorted(result.changed_keys)
                },
            }
        failed = {
            key: {
                "revision": failure.revision,
                "subscriber": failure.subscriber,
            }
            for key, failure in snapshot.failed_live_keys.items()
            if self._is_daemon_key(key)
        }
        pending = sorted(key for key in snapshot.pending_restart_keys if self._is_daemon_key(key))
        status = "failed_live" if failed else "pending_restart" if pending else "applied"
        return {
            "committed": True,
            "revision": result.revision,
            "changed_keys": sorted(result.changed_keys),
            "apply_status": status,
            "pending_restart_keys": pending,
            "failed_live_keys": failed,
        }

    def _is_daemon_key(self, key: str) -> bool:
        try:
            return self.registry.resolve(key).source_path is not None
        except UnknownConfigKeyError:
            return False

    @staticmethod
    def _invalid_key(key: str, message: str) -> ConfigValuesError:
        return ConfigValuesError(
            "validation_error",
            message,
            ("content", *key.split(".")),
        )

    @classmethod
    def _validate_mapping_keys(cls, value: object) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise cls._invalid_key(str(key), "Configuration keys must be strings")
                cls._validate_mapping_keys(child)
        elif isinstance(value, list):
            for child in value:
                cls._validate_mapping_keys(child)

    @staticmethod
    def _validate_revision(revision: int) -> None:
        if type(revision) is not int or not 0 <= revision <= MAX_CONFIG_REVISION:
            raise ConfigValuesError(
                "validation_error",
                f"Expected revision must be an exact integer from 0 to {MAX_CONFIG_REVISION}",
                ("expected_revision",),
            )


__all__ = ["ConfigDocumentsService"]
