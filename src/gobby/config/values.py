"""Universal public configuration read and mutation service."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Mapping
from typing import Protocol

from pydantic_core import to_jsonable_python

from gobby.config.ai import GenerationEndpointConfig
from gobby.config.app import DaemonConfig
from gobby.config.registry import (
    CONFIG_REGISTRY,
    ActivationPolicy,
    ConfigRegistry,
    ConfigSecrecy,
    ConfigVisibility,
    RegistrySpec,
    UnknownConfigKeyError,
    config_key_secrecy,
    config_structured_identity_field,
    config_structured_reference_fields,
    decode_dynamic_segment,
    encode_dynamic_segment,
)
from gobby.config.runtime import ConfigSnapshot
from gobby.config.secret_mask import MASKED_SECRET
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
    SecretUpdate,
)
from gobby.storage.config_store import unflatten_config

_GENERATION_ENDPOINT_PREFIX = "ai.generation.endpoints."
_GENERATION_ENDPOINT_IDENTITY_FIELDS = ("protocol", "wire_api", "api_base", "model")
_GENERATION_ENDPOINT_EVIDENCE_FIELDS = (
    "probed_model",
    "input_modalities",
    "probed_json",
    "probed_tools",
)


class ConfigRuntimeReader(Protocol):
    """Runtime operations needed by configuration interfaces."""

    @property
    def snapshot(self) -> ConfigSnapshot: ...

    async def reconcile_local_commit(self, revision: int) -> ConfigSnapshot: ...


class ConfigMutationWriter(Protocol):
    """Revisioned writer used by the universal service."""

    def patch(self, *, expected_revision: int, patch: ConfigPatch) -> ConfigMutationResult: ...


class RunBlocking(Protocol):
    def __call__[T](self, operation: Callable[[], T]) -> Awaitable[T]: ...


class ConfigValuesError(RuntimeError):
    """Transport-neutral public configuration error."""

    def __init__(
        self,
        code: str,
        message: str,
        path: tuple[str, ...],
        status_code: int = 422,
        retryable: bool = False,
        action: str | None = None,
        expected_revision: int | None = None,
        actual_revision: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path
        self.status_code = status_code
        self.retryable = retryable
        self.action = action
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision

    def public_body(self) -> dict[str, object]:
        error: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "path": list(self.path),
            "retryable": self.retryable,
        }
        if self.action is not None:
            error["action"] = self.action
        if self.expected_revision is not None:
            error["expected_revision"] = self.expected_revision
        if self.actual_revision is not None:
            error["actual_revision"] = self.actual_revision
        return {"error": error}


def reject_unprobed_responses_endpoints(
    values: Mapping[str, object],
    unset: Collection[str],
    resolved_paths: Mapping[str, tuple[str, ...]],
    desired: DaemonConfig,
    *,
    document: bool = False,
) -> None:
    """Require probe-gated activation for every changed responses endpoint."""
    touched = {
        key.removeprefix(_GENERATION_ENDPOINT_PREFIX).partition(".")[0]
        for key in (*values, *unset)
        if key.startswith(_GENERATION_ENDPOINT_PREFIX)
    }
    for segment in sorted(touched):
        wire_key = f"{_GENERATION_ENDPOINT_PREFIX}{segment}.wire_api"
        wire_api = values.get(wire_key)
        if wire_api is None:
            try:
                endpoint_name = decode_dynamic_segment(segment)
            except ValueError:
                endpoint_name = segment
            endpoint = desired.ai.generation.endpoints.get(endpoint_name)
            wire_api = endpoint.wire_api if endpoint is not None else None
        if wire_api != "responses":
            continue
        path = resolved_paths.get(
            wire_key,
            ("ai", "generation", "endpoints", segment),
        )
        path_root = "content" if document else "values"
        action = (
            f"Probe and activate endpoint {segment!r} before importing YAML"
            if document
            else f"/api/config/generation-endpoints/{segment}/activate"
        )
        message = (
            f"Responses endpoint {segment!r} must be probed and activated before YAML import"
            if document
            else (
                f"Responses endpoint {segment!r} must be saved through "
                f"/api/config/generation-endpoints/{segment}/activate"
            )
        )
        raise ConfigValuesError(
            "probe_required",
            message,
            (path_root, *path),
            action=action,
        )


def clear_stale_generation_endpoint_probe_evidence(
    values: dict[str, object],
    *,
    desired: DaemonConfig,
    unset: Collection[str] = (),
    secret_keys: Collection[str] = (),
    probe_verified: bool = False,
) -> tuple[str, ...]:
    """Clear persisted probe evidence when a non-probe mutation changes identity.

    Identity is previous-versus-next after secret handling, never payload-key
    presence: a resubmitted ``MASKED_SECRET`` api_key is not a change. Returns
    the evidence keys that were cleared so callers can keep validation/wire
    views in sync.
    """
    if probe_verified:
        return ()
    unset_set = set(unset)
    secret_set = set(secret_keys)
    cleared: list[str] = []
    for name, endpoint in desired.ai.generation.endpoints.items():
        prefix = f"{_GENERATION_ENDPOINT_PREFIX}{encode_dynamic_segment(name)}."
        if not _generation_endpoint_identity_changed(
            endpoint,
            prefix,
            values,
            unset_set,
            secret_set,
        ):
            continue
        for field in _GENERATION_ENDPOINT_EVIDENCE_FIELDS:
            key = f"{prefix}{field}"
            values[key] = None
            cleared.append(key)
    return tuple(cleared)


def _generation_endpoint_identity_changed(
    endpoint: GenerationEndpointConfig,
    prefix: str,
    values: Mapping[str, object],
    unset: set[str],
    secret_keys: set[str],
) -> bool:
    previous = {
        "protocol": endpoint.protocol,
        "wire_api": endpoint.wire_api,
        "api_base": endpoint.api_base,
        "model": endpoint.model,
    }
    for field in _GENERATION_ENDPOINT_IDENTITY_FIELDS:
        key = f"{prefix}{field}"
        if key in unset:
            return True
        if key in values and values[key] != previous[field]:
            return True
    api_key = f"{prefix}api_key"
    if api_key in unset or api_key in secret_keys:
        return True
    if api_key in values and values[api_key] != endpoint.api_key:
        return True
    return False


class ConfigValuesService:
    """Registry-owned public projections and CAS mutation orchestration."""

    def __init__(
        self,
        *,
        runtime: ConfigRuntimeReader,
        mutations: ConfigMutationWriter,
        registry: ConfigRegistry = CONFIG_REGISTRY,
        run_blocking: RunBlocking | None = None,
    ) -> None:
        self.runtime = runtime
        self.mutations = mutations
        self.registry = registry
        self._run_blocking = run_blocking or _run_in_thread

    async def schema(self) -> dict[str, object]:
        return self.registry.json_schema(ConfigVisibility.PUBLIC)

    async def values(self) -> dict[str, object]:
        return self._snapshot_body(self._snapshot())

    def desired_config(self) -> DaemonConfig:
        return self._snapshot().desired

    def desired_secret(self, key: str) -> str | None:
        return self._snapshot().desired_secret(key)

    def _snapshot(self) -> ConfigSnapshot:
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

    def _anchored_snapshot(self, expected_revision: int) -> ConfigSnapshot:
        """Return the runtime snapshot for exactly the CAS epoch being mutated."""
        snapshot = self._snapshot()
        if snapshot.revision != expected_revision:
            raise ConfigValuesError(
                "revision_conflict",
                "Configuration revision is stale",
                ("expected_revision",),
                status_code=409,
                retryable=True,
                expected_revision=expected_revision,
                actual_revision=snapshot.revision,
            )
        return snapshot

    async def patch(
        self,
        *,
        expected_revision: int,
        values: Mapping[str, object],
        unset: Collection[str] = (),
    ) -> dict[str, object]:
        flat, paths = self._flatten_values(values)
        return await self.patch_flat(
            expected_revision=expected_revision,
            values=flat,
            unset=frozenset(unset),
            paths=paths,
        )

    async def patch_flat(
        self,
        *,
        expected_revision: int,
        values: Mapping[str, object],
        unset: frozenset[str] = frozenset(),
        paths: Mapping[str, tuple[str, ...]] | None = None,
        probe_verified: bool = False,
    ) -> dict[str, object]:
        value_updates: dict[str, object] = {}
        secret_updates: dict[str, SecretUpdate] = {}
        resolved_paths = dict(paths or {})
        for key, value in values.items():
            path = resolved_paths.setdefault(key, tuple(key.split(".")))
            spec = self._authorize(key, path)
            secrecy = config_key_secrecy(spec, key)
            if secrecy is ConfigSecrecy.REFERENCE:
                if value == MASKED_SECRET:
                    continue
                if not isinstance(value, str):
                    raise ConfigValuesError(
                        "validation_error",
                        "Secret configuration value must be a string",
                        ("values", *path),
                    )
                if value.startswith("$secret:"):
                    raise ConfigValuesError(
                        "validation_error",
                        "Secret references are not accepted as plaintext values",
                        ("values", *path),
                    )
                secret_updates[key] = SecretUpdate(plaintext=value, category="general")
            elif reference_fields := config_structured_reference_fields(spec):
                value_updates[key] = self._prepared_structured_references(
                    key,
                    value,
                    path,
                    tuple(field.name for field in reference_fields),
                    config_structured_identity_field(spec).name,
                    expected_revision=expected_revision,
                )
            else:
                value_updates[key] = value
        for key in unset:
            path = resolved_paths.setdefault(key, tuple(key.split(".")))
            self._authorize(key, path)
        if not probe_verified:
            self._reject_unprobed_responses_endpoints(
                values,
                unset,
                resolved_paths,
                expected_revision=expected_revision,
            )
        clear_stale_generation_endpoint_probe_evidence(
            value_updates,
            desired=self._anchored_snapshot(expected_revision).desired,
            unset=unset,
            secret_keys=secret_updates,
            probe_verified=probe_verified,
        )

        patch = ConfigPatch(
            values=value_updates,
            unset=unset,
            secrets=secret_updates,
        )
        try:
            result = await self._run_blocking(
                lambda: self.mutations.patch(
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
            error_key = exc.key
            path = resolved_paths.get(error_key, tuple(error_key.split("."))) if error_key else ()
            message = (
                "Secret configuration value is invalid"
                if error_key is not None and self._is_secret_key(error_key)
                else str(exc)
            )
            raise ConfigValuesError(
                "validation_error",
                message,
                ("values", *path),
            ) from exc

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
        return self._mutation_body(result, snapshot)

    def _prepared_structured_references(
        self,
        key: str,
        value: object,
        path: tuple[str, ...],
        reference_fields: tuple[str, ...],
        identity_field: str,
        *,
        expected_revision: int,
    ) -> object:
        """Restore masked structured fields and reject plaintext before CAS."""
        has_masked = isinstance(value, list) and any(
            isinstance(item, Mapping)
            and any(item.get(field) == MASKED_SECRET for field in reference_fields)
            for item in value
        )
        if has_masked:
            snapshot = self._anchored_snapshot(expected_revision)
            try:
                value = restore_masked_structured_references(
                    key,
                    value,
                    snapshot.desired_values.get(key),
                    reference_fields,
                    identity_field,
                )
            except ValueError as exc:
                raise ConfigValuesError(
                    "validation_error",
                    str(exc),
                    ("values", *path),
                ) from exc
        try:
            validate_structured_references(key, value, reference_fields)
        except ValueError as exc:
            raise ConfigValuesError(
                "validation_error",
                str(exc),
                ("values", *path),
            ) from exc
        return value

    def _reject_unprobed_responses_endpoints(
        self,
        values: Mapping[str, object],
        unset: Collection[str],
        resolved_paths: Mapping[str, tuple[str, ...]],
        *,
        expected_revision: int,
    ) -> None:
        """Require the probe-gated activation route for responses-wire endpoints."""
        reject_unprobed_responses_endpoints(
            values,
            unset,
            resolved_paths,
            self._anchored_snapshot(expected_revision).desired,
        )

    def _authorize(self, key: str, path: tuple[str, ...]) -> RegistrySpec:
        try:
            spec = self.registry.resolve(key)
        except UnknownConfigKeyError as exc:
            raise ConfigValuesError(
                "validation_error",
                "Unknown configuration key",
                ("values", *path),
            ) from exc
        if spec.visibility is not ConfigVisibility.PUBLIC:
            raise ConfigValuesError(
                "validation_error",
                "Configuration key is not public",
                ("values", *path),
            )
        if spec.activation is ActivationPolicy.MANAGED:
            raise ConfigValuesError(
                "managed_activation_required",
                "Configuration key requires managed activation",
                ("values", *path),
                action="/api/embeddings/switch/start",
            )
        return spec

    def _flatten_values(
        self,
        values: Mapping[str, object],
    ) -> tuple[dict[str, object], dict[str, tuple[str, ...]]]:
        flat: dict[str, object] = {}
        paths: dict[str, tuple[str, ...]] = {}

        def walk(current: Mapping[str, object], prefix: tuple[str, ...]) -> None:
            for segment, value in current.items():
                if self.registry.dynamic_segment_follows(prefix):
                    try:
                        decode_dynamic_segment(segment)
                    except ValueError as exc:
                        raise ConfigValuesError(
                            "validation_error",
                            f"Dynamic configuration segment is not canonically encoded: {exc}",
                            ("values", *prefix, segment),
                        ) from exc
                path = (*prefix, segment)
                key = ".".join(path)
                try:
                    self.registry.resolve(key)
                except UnknownConfigKeyError:
                    if isinstance(value, Mapping):
                        walk(value, path)
                        continue
                    raise ConfigValuesError(
                        "validation_error",
                        "Unknown configuration key",
                        ("values", *path),
                    ) from None
                flat[key] = value
                paths[key] = path

        walk(values, ())
        return flat, paths

    def _snapshot_body(self, snapshot: ConfigSnapshot) -> dict[str, object]:
        desired, desired_set = self._public_projection(snapshot.desired_values)
        active, active_set = self._public_projection(snapshot.active_values)
        secret_keys = sorted(set(desired_set) | set(active_set))
        pending = sorted(key for key in snapshot.pending_restart_keys if self._is_public_key(key))
        return {
            "revision": snapshot.revision,
            "desired": unflatten_config(desired),
            "active": unflatten_config(active),
            "secret_set": {
                key: {
                    "desired": desired_set.get(key, False),
                    "active": active_set.get(key, False),
                }
                for key in secret_keys
            },
            "pending_restart_keys": pending,
            "failed_live_keys": self._failed_live(snapshot),
        }

    def _public_projection(
        self,
        values: Mapping[str, object],
    ) -> tuple[dict[str, object], dict[str, bool]]:
        projected: dict[str, object] = {}
        secret_set: dict[str, bool] = {}
        for key, value in values.items():
            try:
                spec = self.registry.resolve(key)
            except UnknownConfigKeyError:
                continue
            if spec.visibility is not ConfigVisibility.PUBLIC:
                continue
            if config_key_secrecy(spec, key) is ConfigSecrecy.REFERENCE:
                is_set = isinstance(value, str) and value.startswith("$secret:")
                secret_set[key] = is_set
                projected[key] = MASKED_SECRET if is_set else None
            elif reference_fields := config_structured_reference_fields(spec):
                projected[key] = mask_structured_references(
                    to_jsonable_python(value),
                    tuple(field.name for field in reference_fields),
                )
            else:
                projected[key] = to_jsonable_python(value)
        return projected, secret_set

    def _failed_live(self, snapshot: ConfigSnapshot) -> dict[str, object]:
        return {
            key: {
                "revision": failure.revision,
                "subscriber": failure.subscriber,
            }
            for key, failure in sorted(snapshot.failed_live_keys.items())
            if self._is_public_key(key)
        }

    def _mutation_body(
        self,
        result: ConfigMutationResult,
        snapshot: ConfigSnapshot,
    ) -> dict[str, object]:
        failed = self._failed_live(snapshot)
        pending = sorted(key for key in snapshot.pending_restart_keys if self._is_public_key(key))
        apply_status = "failed_live" if failed else "pending_restart" if pending else "applied"
        return {
            "committed": True,
            "revision": result.revision,
            "changed_keys": sorted(result.changed_keys),
            "apply_status": apply_status,
            "pending_restart_keys": pending,
            "failed_live_keys": failed,
        }

    def _is_public_key(self, key: str) -> bool:
        try:
            return self.registry.resolve(key).visibility is ConfigVisibility.PUBLIC
        except UnknownConfigKeyError:
            return False

    def _is_secret_key(self, key: str) -> bool:
        try:
            spec = self.registry.resolve(key)
        except UnknownConfigKeyError:
            return False
        return config_key_secrecy(spec, key) is ConfigSecrecy.REFERENCE


async def _run_in_thread[T](operation: Callable[[], T]) -> T:
    return await asyncio.to_thread(operation)


__all__ = [
    "ConfigValuesError",
    "ConfigValuesService",
    "clear_stale_generation_endpoint_probe_evidence",
    "reject_unprobed_responses_endpoints",
]
