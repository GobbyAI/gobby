"""Atomic desired/active configuration state for daemon consumers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from concurrent.futures import Future as ThreadFuture
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from types import MappingProxyType
from typing import TypeVar, cast

from gobby.config.app import DaemonConfig
from gobby.config.registry import CONFIG_REGISTRY, ActivationPolicy
from gobby.config.runtime_activation import SubscriberFailure as _PreparationFailure
from gobby.config.runtime_activation import apply_failure as _apply_failure
from gobby.config.runtime_activation import copy_snapshot as _copy_snapshot
from gobby.config.runtime_activation import project_activation as _project_activation
from gobby.config.runtime_contracts import (
    ConfigNotificationSource,
    ConfigRuntimeError,
    ConfigSnapshotRepository,
    ConfigSubscriber,
    ConstructorLaneSaturatedError,
    PreparedSubscriber,
    PreparedValue,
    RegistrySpec,
    RuntimeActiveBundle,
    RuntimeRegistry,
    SecretIdentityMismatchError,
    StoredConfigSnapshot,
    StoredSecretBinding,
)
from gobby.config.runtime_models import (
    ApplyFailure,
    ConfigChange,
    ConfigSnapshot,
    RuntimeSecretBinding,
    UnavailableService,
)
from gobby.config.runtime_projection import (
    changed_keys as _changed_keys,
)
from gobby.config.runtime_projection import (
    runtime_bindings as _runtime_bindings,
)
from gobby.storage.config_repository import MAX_CONFIG_REVISION

__all__ = [
    "ApplyFailure",
    "ConfigChange",
    "ConfigNotificationSource",
    "ConfigRuntime",
    "ConfigRuntimeError",
    "ConfigSnapshot",
    "ConfigSnapshotRepository",
    "ConfigSubscriber",
    "ConstructorLaneSaturatedError",
    "PreparedSubscriber",
    "PreparedValue",
    "RegistrySpec",
    "RuntimeActiveBundle",
    "RuntimeRegistry",
    "RuntimeSecretBinding",
    "SecretIdentityMismatchError",
    "StoredConfigSnapshot",
    "StoredSecretBinding",
    "UnavailableService",
]

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class ConfigRuntime:
    def __init__(
        self,
        repository: ConfigSnapshotRepository,
        *,
        registry: RuntimeRegistry = CONFIG_REGISTRY,
        subscribers: Iterable[ConfigSubscriber] = (),
        notification_source: ConfigNotificationSource | None = None,
        db_workers: int = 2,
        constructor_workers: int = 2,
        db_timeout: float = 5.5,
        constructor_admission_timeout: float = 0.25,
        statement_timeout_ms: int = 5_000,
        lock_timeout_ms: int = 5_000,
        reconnect_backoff: float = 0.1,
        revision_poll_interval: float = 30.0,
        expected_secret_identity: str | None = None,
        secret_identity_verifier: Callable[[], str] | None = None,
        managed_resolver: Callable[[StoredConfigSnapshot], Mapping[str, object]] | None = None,
    ) -> None:
        if db_workers <= 0 or constructor_workers <= 0:
            raise ValueError("Runtime lane worker counts must be positive")
        if db_timeout <= 0 or constructor_admission_timeout <= 0:
            raise ValueError("Runtime deadlines must be positive")
        if (expected_secret_identity is None) != (secret_identity_verifier is None):
            raise ValueError("Secret identity expectation and verifier must be configured together")
        self._repository = repository
        self._registry = registry
        self._subscribers = list(subscribers)
        self._notifications = notification_source
        self._db_executor = ThreadPoolExecutor(
            max_workers=db_workers,
            thread_name_prefix="gobby-config-db",
        )
        self._constructor_executor = ThreadPoolExecutor(
            max_workers=constructor_workers,
            thread_name_prefix="gobby-config-constructor",
        )
        self._db_capacity = asyncio.Semaphore(db_workers)
        self._constructor_capacity = asyncio.Semaphore(constructor_workers)
        self._db_timeout = db_timeout
        self._constructor_admission_timeout = constructor_admission_timeout
        self._statement_timeout_ms = statement_timeout_ms
        self._lock_timeout_ms = lock_timeout_ms
        self._reconnect_backoff = reconnect_backoff
        self._revision_poll_interval = revision_poll_interval
        self._expected_secret_identity = expected_secret_identity
        self._secret_identity_verifier = secret_identity_verifier
        self._managed_resolver = managed_resolver
        self._reconcile_lock = asyncio.Lock()
        self._reconnect_lock = asyncio.Lock()
        self._max_requested_revision = -1
        self._pending_revision_requests: dict[object, int] = {}
        self._bundle: RuntimeActiveBundle | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._revision_publisher: Callable[[int], Awaitable[None]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = False
        self._healthy = False
        self._closed = False

    @property
    def snapshot(self) -> ConfigSnapshot:
        return self.capture().snapshot

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def degraded(self) -> bool:
        bundle = self._bundle
        return bundle is not None and bool(bundle.snapshot.failed_live_keys)

    def capture(self) -> RuntimeActiveBundle:
        bundle = self._bundle
        if bundle is None:
            raise RuntimeError("ConfigRuntime has not started")
        return bundle

    def register_revision_publisher(
        self,
        publisher: Callable[[int], Awaitable[None]],
    ) -> None:
        """Register the daemon's single post-reconciliation revision publisher."""
        if self._revision_publisher is not None:
            raise RuntimeError("Configuration revision publisher is already registered")
        self._revision_publisher = publisher

    async def start(self) -> ConfigSnapshot:
        if self._closed:
            raise RuntimeError("ConfigRuntime is closed")
        if self._bundle is not None:
            return self._bundle.snapshot
        self._loop = asyncio.get_running_loop()
        if self._notifications is not None:
            await self._notifications.connect()
        try:
            await self._verify_secret_identity()
            stored, desired = await self._read_projection()
            if stored.revision < 0:
                raise ConfigRuntimeError("Stored configuration revision must be non-negative")
            snapshot, services, handles, managed = await self._initialize(stored, desired)
            activation_failure = await self._activate_many(handles, self._subscribers)
            if activation_failure is not None:
                await self._dispose_many(handles, self._subscribers)
                raise activation_failure.error
            self._bundle = RuntimeActiveBundle(
                snapshot,
                MappingProxyType(services),
                MappingProxyType(handles),
                managed,
            )
            self._max_requested_revision = stored.revision
            self._ready = True
            self._healthy = True
            if self._notifications is not None:
                self._listener_task = asyncio.create_task(
                    self._listen(),
                    name="config-runtime-listener",
                )
                if callable(getattr(self._repository, "current_revision", None)):
                    self._poll_task = asyncio.create_task(
                        self._poll_revisions(),
                        name="config-runtime-revision-poll",
                    )
            return snapshot
        except BaseException:
            self._healthy = False
            if self._notifications is not None:
                await self._notifications.close()
            raise

    async def reconcile_revision(self, revision: int) -> ConfigSnapshot:
        if revision < 0:
            raise ValueError("Configuration revision must be non-negative")
        if self._closed:
            raise RuntimeError("ConfigRuntime is closed")
        revision = min(revision, MAX_CONFIG_REVISION)
        request = object()
        self._pending_revision_requests[request] = revision
        self._max_requested_revision = max(self._max_requested_revision, revision)
        try:
            async with self._reconcile_lock:
                self._pending_revision_requests.pop(request, None)
                return await self._reconcile_locked(force=False)
        finally:
            self._pending_revision_requests.pop(request, None)

    async def reprepare_subscriber(self, name: str) -> ConfigSnapshot:
        if self._closed:
            raise RuntimeError("ConfigRuntime is closed")
        subscriber = next(
            (candidate for candidate in self._subscribers if candidate.name == name),
            None,
        )
        if subscriber is None:
            raise ValueError(f"Unknown configuration subscriber: {name}")
        async with self._reconcile_lock:
            while True:
                bundle = self.capture()
                snapshot = bundle.snapshot
                stored, desired = await self._read_projection()
                if stored.revision != snapshot.revision:
                    self._max_requested_revision = max(
                        self._max_requested_revision,
                        stored.revision,
                    )
                    await self._reconcile_locked(force=False)
                    continue
                desired_bindings = _runtime_bindings(stored.secret_bindings)
                managed = self._resolve_managed(stored)
                change = ConfigChange(
                    revision=snapshot.revision,
                    changed_keys=subscriber.keys,
                    previous=snapshot,
                    desired=desired,
                    _desired_bindings=desired_bindings,
                    managed=managed,
                )
                prepared, failure = await self._prepare_one(subscriber, change)
                if self._max_requested_revision > snapshot.revision:
                    if prepared is not None:
                        await self._dispose(prepared, subscriber)
                    await self._reconcile_locked(force=False)
                    continue
                prepared_map = {name: prepared} if prepared is not None else {}
                activation_failure = None
                if failure is None and prepared is not None:
                    activation_failure = await self._activate_many(prepared_map, (subscriber,))
                    failure = activation_failure
                snapshot, services, handles, old_handles = _project_activation(
                    bundle,
                    stored,
                    desired,
                    desired_bindings,
                    subscriber.keys,
                    prepared_map,
                    failure,
                    subscribers=(subscriber,),
                    repository=self._repository,
                    registry=self._registry,
                )
                self._bundle = RuntimeActiveBundle(
                    snapshot,
                    MappingProxyType(services),
                    MappingProxyType(handles),
                    managed if failure is None else bundle.managed,
                )
                await self._dispose_replaced(old_handles)
                if activation_failure is not None:
                    raise activation_failure.error
                return snapshot

    async def reconcile_local_commit(self, revision: int) -> ConfigSnapshot:
        """Await immediate reconciliation for a locally committed PATCH."""
        return await self.reconcile_revision(revision)

    def local_commit_handoff(self, revision: int) -> ThreadFuture[ConfigSnapshot]:
        """Schedule local reconciliation safely from a synchronous request thread."""
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("ConfigRuntime event loop is unavailable")
        return asyncio.run_coroutine_threadsafe(self.reconcile_revision(revision), loop)

    async def register_subscriber(self, subscriber: ConfigSubscriber) -> ConfigSnapshot:
        """Register against one stable epoch and return its active projection/revision."""
        if any(existing.name == subscriber.name for existing in self._subscribers):
            raise ValueError(f"Configuration subscriber already registered: {subscriber.name}")
        async with self._reconcile_lock:
            while True:
                bundle = self.capture()
                change = ConfigChange(
                    revision=bundle.snapshot.revision,
                    changed_keys=subscriber.keys,
                    previous=bundle.snapshot,
                    desired=bundle.snapshot.desired,
                    _desired_bindings=bundle.snapshot._desired_bindings,
                    managed=bundle.managed,
                )
                prepared, failure = await self._prepare_one(subscriber, change)
                if self._max_requested_revision > bundle.snapshot.revision:
                    if prepared is not None:
                        await self._dispose(prepared, subscriber)
                    await self._reconcile_locked(force=False)
                    continue
                services = dict(bundle.services)
                handles = dict(bundle._handles)
                snapshot = bundle.snapshot
                if failure is not None:
                    if subscriber.required:
                        raise RuntimeError(str(failure.error)) from failure.error
                    apply_failure = _apply_failure(
                        subscriber,
                        subscriber.keys,
                        failure.error,
                        revision=snapshot.revision,
                    )
                    services[subscriber.name] = UnavailableService(apply_failure)
                    failed = dict(snapshot.failed_live_keys)
                    for key in subscriber.keys:
                        failed[key] = apply_failure
                    snapshot = _copy_snapshot(snapshot, failed_live_keys=failed)
                else:
                    assert prepared is not None
                    services[subscriber.name] = prepared.value
                    handles[subscriber.name] = prepared
                    activation_failure = await self._activate_many(
                        {subscriber.name: prepared},
                        (subscriber,),
                    )
                    if activation_failure is not None:
                        await self._dispose(prepared, subscriber)
                        if subscriber.required:
                            raise activation_failure.error
                        handles.pop(subscriber.name, None)
                        apply_failure = _apply_failure(
                            subscriber,
                            subscriber.keys,
                            activation_failure.error,
                            revision=snapshot.revision,
                        )
                        services[subscriber.name] = UnavailableService(apply_failure)
                        failed = dict(snapshot.failed_live_keys)
                        for key in subscriber.keys:
                            failed[key] = apply_failure
                        snapshot = _copy_snapshot(snapshot, failed_live_keys=failed)
                self._subscribers.append(subscriber)
                self._bundle = RuntimeActiveBundle(
                    snapshot,
                    MappingProxyType(services),
                    MappingProxyType(handles),
                    bundle.managed,
                )
                return snapshot

    async def _initialize(
        self,
        stored: StoredConfigSnapshot,
        desired: DaemonConfig,
    ) -> tuple[
        ConfigSnapshot,
        dict[str, object],
        dict[str, PreparedSubscriber],
        MappingProxyType[str, object],
    ]:
        bindings = _runtime_bindings(stored.secret_bindings)
        managed = self._resolve_managed(stored)
        desired_values = dict(stored.values)
        desired_overrides = dict(stored.overrides)
        failures: dict[str, ApplyFailure] = {}
        services: dict[str, object] = {}
        handles: dict[str, PreparedSubscriber] = {}
        base = ConfigSnapshot(
            revision=stored.revision,
            desired=desired,
            active=desired,
            row_revisions=stored.row_revisions,
            pending_restart_keys=frozenset(),
            failed_live_keys={},
            desired_values=desired_values,
            active_values=desired_values,
            desired_overrides=desired_overrides,
            active_overrides=desired_overrides,
            desired_bindings=bindings,
            active_bindings=bindings,
        )
        change = ConfigChange(
            revision=stored.revision,
            changed_keys=frozenset(desired_values) | frozenset(bindings),
            previous=None,
            desired=desired.model_copy(deep=True),
            _desired_bindings=bindings,
            managed=managed,
        )
        for subscriber in self._subscribers:
            prepared, failure = await self._prepare_one(subscriber, change)
            if failure is None:
                assert prepared is not None
                services[subscriber.name] = prepared.value
                handles[subscriber.name] = prepared
                continue
            apply_failure = _apply_failure(
                subscriber,
                subscriber.keys,
                failure.error,
                revision=stored.revision,
            )
            if subscriber.required:
                await self._dispose_many(handles, self._subscribers)
                raise RuntimeError(str(failure.error)) from failure.error
            services[subscriber.name] = UnavailableService(apply_failure)
            for key in subscriber.keys:
                failures[key] = apply_failure
        return _copy_snapshot(base, failed_live_keys=failures), services, handles, managed

    _CONFIRMING_READS = 3
    _CONFIRM_BACKOFF = 0.05

    async def _reconcile_locked(self, *, force: bool) -> ConfigSnapshot:
        confirming_reads = 0
        while True:
            current_bundle = self.capture()
            current = current_bundle.snapshot
            requested = self._max_requested_revision
            if not force and requested <= current.revision:
                return current
            stored, desired = await self._read_projection()
            if self._max_requested_revision > stored.revision:
                if self._max_requested_revision != requested:
                    confirming_reads = 0
                    await asyncio.sleep(self._CONFIRM_BACKOFF)
                    continue
                confirming_reads += 1
                if confirming_reads < self._CONFIRMING_READS:
                    await asyncio.sleep(self._CONFIRM_BACKOFF * (2 ** (confirming_reads - 1)))
                    continue
                logger.warning(
                    "Requested configuration revision %s was never observed in storage "
                    "(stored revision %s) after %s confirming reads; adopting the "
                    "stored revision",
                    self._max_requested_revision,
                    stored.revision,
                    confirming_reads,
                )
                pending_revision = max(
                    self._pending_revision_requests.values(),
                    default=stored.revision,
                )
                self._max_requested_revision = max(stored.revision, pending_revision)
            confirming_reads = 0
            if stored.revision <= current.revision:
                return current
            changed_keys = _changed_keys(current, stored)
            desired_bindings = _runtime_bindings(stored.secret_bindings)
            managed = self._resolve_managed(stored)
            changed_live, _changed_restart = self._partition_changes(changed_keys)
            change = ConfigChange(
                revision=stored.revision,
                changed_keys=changed_live,
                previous=current,
                desired=desired.model_copy(deep=True),
                _desired_bindings=desired_bindings,
                managed=managed,
            )
            prepared, failure = await self._prepare_matching(change)
            if self._max_requested_revision > stored.revision:
                await self._dispose_prepared_map(prepared)
                force = False
                continue
            activation_failure = None
            if failure is None:
                activation_failure = await self._activate_many(prepared, self._subscribers)
                failure = activation_failure
            snapshot, services, handles, old_handles = _project_activation(
                current_bundle,
                stored,
                desired,
                desired_bindings,
                changed_live,
                prepared,
                failure,
                subscribers=self._subscribers,
                repository=self._repository,
                registry=self._registry,
            )
            self._bundle = RuntimeActiveBundle(
                snapshot,
                MappingProxyType(services),
                MappingProxyType(handles),
                managed if failure is None else current_bundle.managed,
            )
            await self._dispose_replaced(old_handles)
            if activation_failure is not None:
                raise activation_failure.error
            if self._revision_publisher is not None:
                await self._revision_publisher(snapshot.revision)
            force = False
            if self._max_requested_revision <= snapshot.revision:
                return snapshot

    async def _prepare_matching(
        self,
        change: ConfigChange,
    ) -> tuple[dict[str, PreparedSubscriber], _PreparationFailure | None]:
        prepared: dict[str, PreparedSubscriber] = {}
        for subscriber in self._subscribers:
            if not subscriber.keys.intersection(change.changed_keys):
                continue
            replacement, failure = await self._prepare_one(subscriber, change)
            if failure is not None:
                return prepared, failure
            assert replacement is not None
            prepared[subscriber.name] = replacement
            if self._max_requested_revision > change.revision:
                return prepared, None
        return prepared, None

    async def _prepare_one(
        self,
        subscriber: ConfigSubscriber,
        change: ConfigChange,
    ) -> tuple[PreparedSubscriber | None, _PreparationFailure | None]:
        try:
            replacement = await self._run_constructor(
                lambda: subscriber.prepare(change),
                timeout=subscriber.prepare_timeout,
            )
            return replacement, None
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            return None, _PreparationFailure(subscriber, exc)

    async def _run_constructor(
        self,
        operation: Callable[[], PreparedSubscriber],
        *,
        timeout: float,
    ) -> PreparedSubscriber:
        try:
            await asyncio.wait_for(
                self._constructor_capacity.acquire(),
                timeout=min(timeout, self._constructor_admission_timeout),
            )
        except TimeoutError as exc:
            raise ConstructorLaneSaturatedError("Constructor lane admission timed out") from exc
        loop = asyncio.get_running_loop()
        try:
            future = self._constructor_executor.submit(operation)
        except BaseException:
            self._constructor_capacity.release()
            raise
        self._release_capacity_when_done(future, self._constructor_capacity, loop)
        wrapped = asyncio.wrap_future(future)
        try:
            return await asyncio.wait_for(asyncio.shield(wrapped), timeout=timeout)
        except TimeoutError:
            future.add_done_callback(_dispose_late_result)
            raise

    async def _run_disposer(self, operation: Callable[[], None], *, timeout: float) -> None:
        try:
            await asyncio.wait_for(
                self._constructor_capacity.acquire(),
                timeout=min(timeout, self._constructor_admission_timeout),
            )
        except TimeoutError:
            logger.warning("Config resource disposal skipped: constructor lane saturated")
            return
        loop = asyncio.get_running_loop()
        try:
            future = self._constructor_executor.submit(operation)
        except BaseException:
            self._constructor_capacity.release()
            raise
        self._release_capacity_when_done(future, self._constructor_capacity, loop)
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout=timeout)
        except TimeoutError:
            logger.warning("Config resource disposal exceeded its deadline")
        except Exception:
            logger.warning("Config resource disposal failed", exc_info=True)

    async def _read_projection(self) -> tuple[StoredConfigSnapshot, DaemonConfig]:
        def read() -> tuple[StoredConfigSnapshot, DaemonConfig]:
            bounded = getattr(self._repository, "read_bounded", None)
            if bounded is None:
                stored = self._repository.read(resolve_secrets=True)
            else:
                stored = bounded(
                    resolve_secrets=True,
                    statement_timeout_ms=self._statement_timeout_ms,
                    lock_timeout_ms=self._lock_timeout_ms,
                )
            return stored, self._repository.runtime_candidate(
                dict(stored.overrides), stored.secret_bindings
            )

        return await self._run_db(read)

    async def _run_db(self, operation: Callable[[], _T]) -> _T:
        try:
            await asyncio.wait_for(self._db_capacity.acquire(), timeout=self._db_timeout)
        except TimeoutError as exc:
            raise ConfigRuntimeError("Database lane admission timed out") from exc
        loop = asyncio.get_running_loop()
        try:
            future = self._db_executor.submit(operation)
        except BaseException:
            self._db_capacity.release()
            raise
        self._release_capacity_when_done(future, self._db_capacity, loop)
        try:
            return await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(future)),
                timeout=self._db_timeout,
            )
        except TimeoutError as exc:
            raise ConfigRuntimeError("Bounded configuration database work timed out") from exc

    @staticmethod
    def _release_capacity_when_done(
        future: ThreadFuture[_T],
        capacity: asyncio.Semaphore,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        def release(_future: ThreadFuture[_T]) -> None:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(capacity.release)

        future.add_done_callback(release)

    async def _verify_secret_identity(self) -> None:
        verifier = self._secret_identity_verifier
        expected = self._expected_secret_identity
        if verifier is None or expected is None:
            return
        observed = await self._run_db(verifier)
        if observed != expected:
            raise SecretIdentityMismatchError(
                f"Remote secret identity mismatch: expected {expected!r}, observed {observed!r}"
            )

    def _partition_changes(
        self,
        changed_keys: frozenset[str],
    ) -> tuple[frozenset[str], frozenset[str]]:
        live: set[str] = set()
        restart: set[str] = set()
        for key in changed_keys:
            activation = self._registry.resolve(key).activation
            if activation is ActivationPolicy.MANAGED:
                if self._managed_resolver is None:
                    raise ConfigRuntimeError(f"Managed configuration key reached runtime: {key}")
                live.add(key)
                continue
            if activation is ActivationPolicy.RESTART_REQUIRED:
                restart.add(key)
            else:
                live.add(key)
        return frozenset(live), frozenset(restart)

    def _resolve_managed(
        self,
        stored: StoredConfigSnapshot,
    ) -> MappingProxyType[str, object]:
        if self._managed_resolver is None:
            return MappingProxyType({})
        return MappingProxyType(dict(self._managed_resolver(stored)))

    async def _dispose_prepared_map(
        self,
        prepared: Mapping[str, PreparedSubscriber],
    ) -> None:
        by_name = {subscriber.name: subscriber for subscriber in self._subscribers}
        for name, replacement in prepared.items():
            subscriber = by_name[name]
            await self._dispose(replacement, subscriber)

    async def _dispose_replaced(
        self,
        resources: Iterable[tuple[PreparedSubscriber, ConfigSubscriber]],
    ) -> None:
        for resource, subscriber in resources:
            await self._dispose(resource, subscriber)

    async def _dispose_many(
        self,
        handles: Mapping[str, PreparedSubscriber],
        subscribers: Iterable[ConfigSubscriber],
    ) -> None:
        by_name = {subscriber.name: subscriber for subscriber in subscribers}
        for name, resource in handles.items():
            await self._dispose(resource, by_name[name])

    async def _activate_many(
        self,
        handles: Mapping[str, PreparedSubscriber],
        subscribers: Iterable[ConfigSubscriber],
    ) -> _PreparationFailure | None:
        by_name = {subscriber.name: subscriber for subscriber in subscribers}
        for name, resource in handles.items():
            activate = getattr(resource, "activate", None)
            if activate is None:
                continue
            subscriber = by_name[name]
            try:
                await self._run_constructor(
                    cast(
                        Callable[[], PreparedSubscriber],
                        lambda activate=activate, resource=resource: (activate(), resource)[1],
                    ),
                    timeout=subscriber.prepare_timeout,
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                return _PreparationFailure(subscriber, exc)
        return None

    async def _dispose(
        self,
        resource: PreparedSubscriber,
        subscriber: ConfigSubscriber,
    ) -> None:
        await self._run_disposer(resource.dispose, timeout=subscriber.dispose_timeout)

    async def _poll_revisions(self) -> None:
        """Heal a silently dead LISTEN socket with a low-frequency revision poll."""
        current_revision = getattr(self._repository, "current_revision", None)
        assert callable(current_revision)
        while not self._closed:
            await asyncio.sleep(self._revision_poll_interval)
            if self._closed:
                return
            try:
                observed = await self._run_db(current_revision)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Configuration revision poll failed", exc_info=True)
                continue
            bundle = self._bundle
            if (
                bundle is None
                or observed <= bundle.snapshot.revision
                or observed <= self._max_requested_revision
            ):
                continue
            self._healthy = False
            try:
                listener = self._listener_task
                if listener is not None and not listener.done():
                    assert self._notifications is not None
                    await self._notifications.close()
                else:
                    await self._reconnect()
                    if not self._closed:
                        self._listener_task = asyncio.create_task(
                            self._listen(), name="config-runtime-listener"
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Configuration revision poll reconnect failed",
                    exc_info=True,
                )

    async def _listen(self) -> None:
        assert self._notifications is not None
        while not self._closed:
            try:
                async for revision in self._notifications.revisions():
                    await self.reconcile_revision(revision)
                    if self._closed:
                        return
                raise ConnectionError("Configuration notification stream ended")
            except asyncio.CancelledError:
                raise
            except Exception:
                self._healthy = False
                logger.warning("Configuration listener disconnected", exc_info=True)
                await self._reconnect()

    async def _reconnect(self) -> None:
        assert self._notifications is not None
        async with self._reconnect_lock:
            if self._healthy or self._closed:
                return
            while not self._closed:
                with suppress(Exception):
                    await self._notifications.close()
                await asyncio.sleep(self._reconnect_backoff)
                try:
                    await self._notifications.connect()
                    async with self._reconcile_lock:
                        await self._reconcile_locked(force=True)
                    self._healthy = True
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Configuration listener reconnect failed", exc_info=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ready = False
        self._healthy = False
        for task in (self._listener_task, self._poll_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        if self._notifications is not None:
            with suppress(Exception):
                await self._notifications.close()
        bundle = self._bundle
        if bundle is not None:
            await self._dispose_many(bundle._handles, self._subscribers)
        self._db_executor.shutdown(wait=False, cancel_futures=True)
        self._constructor_executor.shutdown(wait=False, cancel_futures=True)


def _dispose_late_result(future: ThreadFuture[PreparedSubscriber]) -> None:
    try:
        replacement = future.result()
    except BaseException:
        return
    try:
        replacement.dispose()
    except Exception:
        logger.warning("Late configuration replacement disposal failed", exc_info=True)


__all__ = [
    "ApplyFailure",
    "ConfigChange",
    "ConfigRuntime",
    "ConfigRuntimeError",
    "ConfigSnapshot",
    "ConfigSubscriber",
    "ConstructorLaneSaturatedError",
    "PreparedSubscriber",
    "PreparedValue",
    "RuntimeActiveBundle",
    "RuntimeSecretBinding",
    "SecretIdentityMismatchError",
    "UnavailableService",
]
