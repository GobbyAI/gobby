"""Activation projection helpers for runtime configuration bundles."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from gobby.config.app import DaemonConfig
from gobby.config.registry import ActivationPolicy
from gobby.config.runtime_contracts import (
    ConfigSnapshotRepository,
    ConfigSubscriber,
    PreparedSubscriber,
    RuntimeActiveBundle,
    RuntimeRegistry,
    StoredConfigSnapshot,
)
from gobby.config.runtime_models import ApplyFailure, ConfigSnapshot, RuntimeSecretBinding


@dataclass(frozen=True, slots=True)
class SubscriberFailure:
    subscriber: ConfigSubscriber
    error: BaseException


def apply_failure(
    subscriber: ConfigSubscriber,
    keys: frozenset[str],
    error: BaseException,
    *,
    revision: int,
) -> ApplyFailure:
    return ApplyFailure(
        revision=revision,
        subscriber=subscriber.name,
        keys=keys,
        message=str(error),
    )


def copy_snapshot(
    snapshot: ConfigSnapshot,
    *,
    failed_live_keys: Mapping[str, ApplyFailure],
) -> ConfigSnapshot:
    return ConfigSnapshot(
        revision=snapshot.revision,
        desired=snapshot._desired,
        active=snapshot._active,
        row_revisions=snapshot.row_revisions,
        pending_restart_keys=snapshot.pending_restart_keys,
        failed_live_keys=failed_live_keys,
        desired_values=snapshot._desired_values,
        active_values=snapshot._active_values,
        desired_overrides=snapshot._desired_overrides,
        active_overrides=snapshot._active_overrides,
        desired_bindings=snapshot._desired_bindings,
        active_bindings=snapshot._active_bindings,
    )


def _pending_restart_keys(
    registry: RuntimeRegistry,
    desired_values: Mapping[str, object],
    active_values: Mapping[str, object],
    desired_bindings: Mapping[str, RuntimeSecretBinding],
    active_bindings: Mapping[str, RuntimeSecretBinding],
) -> frozenset[str]:
    pending: set[str] = set()
    keys = set(desired_values) | set(active_values) | set(desired_bindings) | set(active_bindings)
    for key in keys:
        if registry.resolve(key).activation is not ActivationPolicy.RESTART_REQUIRED:
            continue
        desired_fp = desired_bindings.get(key)
        active_fp = active_bindings.get(key)
        if desired_values.get(key) != active_values.get(key) or (
            None if desired_fp is None else desired_fp.fingerprint
        ) != (None if active_fp is None else active_fp.fingerprint):
            pending.add(key)
    return frozenset(pending)


def project_activation(
    current_bundle: RuntimeActiveBundle,
    stored: StoredConfigSnapshot,
    desired: DaemonConfig,
    desired_bindings: Mapping[str, RuntimeSecretBinding],
    changed_live: frozenset[str],
    prepared: Mapping[str, PreparedSubscriber],
    failure: SubscriberFailure | None,
    *,
    subscribers: Iterable[ConfigSubscriber],
    repository: ConfigSnapshotRepository,
    registry: RuntimeRegistry,
) -> tuple[
    ConfigSnapshot,
    dict[str, object],
    dict[str, PreparedSubscriber],
    list[tuple[PreparedSubscriber, ConfigSubscriber]],
]:
    current = current_bundle.snapshot
    active_values = dict(current._read_active_values())
    active_overrides = dict(current._read_active_overrides())
    active_bindings = dict(current._read_active_bindings())
    failed = dict(current.failed_live_keys)
    services = dict(current_bundle.services)
    handles = dict(current_bundle._read_handles())
    old_handles: list[tuple[PreparedSubscriber, ConfigSubscriber]] = []
    subscribers = tuple(subscribers)
    if failure is None:
        replaced_subscribers: set[str] = set()
        for subscriber in subscribers:
            replacement = prepared.get(subscriber.name)
            if replacement is None:
                continue
            previous = handles.pop(subscriber.name, None)
            if previous is not None:
                old_handles.append((previous, subscriber))
            handles[subscriber.name] = replacement
            services[subscriber.name] = replacement.value
            replaced_subscribers.add(subscriber.name)
        activated_keys = set(changed_live)
        activated_keys.update(
            key
            for key, prior_failure in failed.items()
            if prior_failure.subscriber in replaced_subscribers
        )
        for key in activated_keys:
            if key in stored.values:
                active_values[key] = stored.values[key]
            else:
                active_values.pop(key, None)
            if key in stored.overrides:
                active_overrides[key] = stored.overrides[key]
            else:
                active_overrides.pop(key, None)
            if key in desired_bindings:
                active_bindings[key] = desired_bindings[key]
            else:
                active_bindings.pop(key, None)
            failed.pop(key, None)
    else:
        recorded_failure = apply_failure(
            failure.subscriber,
            changed_live,
            failure.error,
            revision=stored.revision,
        )
        for key in changed_live:
            failed[key] = recorded_failure
        for subscriber in subscribers:
            replacement = prepared.get(subscriber.name)
            if replacement is not None:
                old_handles.append((replacement, subscriber))
    active = repository.runtime_candidate(active_overrides, active_bindings)
    pending = _pending_restart_keys(
        registry,
        stored.values,
        active_values,
        desired_bindings,
        active_bindings,
    )
    snapshot = ConfigSnapshot(
        revision=stored.revision,
        desired=desired,
        active=active,
        row_revisions=stored.row_revisions,
        pending_restart_keys=pending,
        failed_live_keys=failed,
        desired_values=stored.values,
        active_values=active_values,
        desired_overrides=stored.overrides,
        active_overrides=active_overrides,
        desired_bindings=desired_bindings,
        active_bindings=active_bindings,
    )
    return snapshot, services, handles, old_handles
