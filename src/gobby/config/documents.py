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
)
from gobby.config.runtime import ConfigSnapshot
from gobby.config.values import MASKED_SECRET, ConfigValuesError
from gobby.storage.config_mutations import (
    ConfigConflictError,
    ConfigMutationResult,
    ConfigPatch,
    ConfigRevisionExhaustedError,
    ConfigValidationError,
    SecretUpdate,
)
from gobby.storage.config_repository import MAX_CONFIG_REVISION
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
        snapshot = self.runtime.snapshot
        patch = await self.run_blocking(lambda: self._prepare(content, snapshot))
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
        return await self._reconcile(result)

    async def export_yaml(self) -> dict[str, object]:
        """Export one desired daemon snapshot with secret references masked."""
        snapshot = self.runtime.snapshot
        content = await self.run_blocking(lambda: self._dump(snapshot))
        return {"revision": snapshot.revision, "content": content}

    def _prepare(self, content: str, snapshot: ConfigSnapshot) -> ConfigPatch:
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
        flat = flatten_config(parsed)
        values: dict[str, object] = {}
        secrets: dict[str, SecretUpdate] = {}
        validation_values: dict[str, object] = {}
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
                self._prepare_secret(
                    key=key,
                    value=value,
                    snapshot=snapshot,
                    values=values,
                    secrets=secrets,
                    validation_values=validation_values,
                )
                continue
            values[key] = value
            validation_values[key] = value
        try:
            self.runtime_candidate(validation_values)
        except (ValueError, TypeError) as exc:
            raise ConfigValuesError(
                "validation_error",
                f"Complete configuration candidate is invalid: {exc}",
                ("content",),
            ) from exc
        return ConfigPatch(values=values, secrets=secrets)

    def _prepare_secret(
        self,
        *,
        key: str,
        value: object,
        snapshot: ConfigSnapshot,
        values: dict[str, object],
        secrets: dict[str, SecretUpdate],
        validation_values: dict[str, object],
    ) -> None:
        if not isinstance(value, str):
            raise self._invalid_key(key, "Secret configuration value must be a string")
        if value == MASKED_SECRET:
            reference = snapshot.desired_values.get(key)
            if not isinstance(reference, str) or not reference.startswith("$secret:"):
                raise self._invalid_key(key, "Masked secret has no persisted reference")
            plaintext = snapshot.desired_secret(key)
            if plaintext is None:
                raise self._invalid_key(key, "Persisted secret cannot be resolved")
            values[key] = reference
            validation_values[key] = plaintext
            return
        if value.startswith("$secret:"):
            name = value.removeprefix("$secret:")
            plaintext = self.resolve_secret(name)
            if not name or plaintext is None:
                raise self._invalid_key(key, "Secret reference cannot be resolved")
            values[key] = value
            validation_values[key] = plaintext
            return
        secrets[key] = SecretUpdate(plaintext=value, category="config")
        validation_values[key] = value

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
            values[key] = (
                MASKED_SECRET if config_key_secrecy(spec, key) is ConfigSecrecy.REFERENCE else value
            )
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
