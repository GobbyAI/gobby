"""Shared persisted provider-capacity service for HTTP and MCP consumers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Literal, Protocol, cast

from gobby.providers.usage import (
    AgyUsageReporter,
    ProviderUsageReporter,
    TransientUsageRefreshError,
    UsageWindow,
)
from gobby.providers.version_gate import ensure_agy_support
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.provider_capacity import (
    PersistedCapacityState,
    ProviderCapacityRecord,
    ProviderCapacityStorage,
)
from gobby.utils.datetime import datetime_to_iso, utc_now

type ProviderCapacityState = Literal["available", "exhausted", "stale", "unknown"]
type RunDatabase = Callable[..., Awaitable[object]]
type Clock = Callable[[], datetime]


class ProviderSupportRecord(Protocol):
    @property
    def installed_version(self) -> str | None: ...

    @property
    def supported(self) -> bool: ...

    @property
    def reason(self) -> str: ...


type SupportResolver = Callable[[], Awaitable[ProviderSupportRecord]]


class CapacityStorage(Protocol):
    @property
    def machine_id(self) -> str: ...

    def get(self, provider: str) -> ProviderCapacityRecord | None: ...

    def upsert(
        self,
        *,
        provider: str,
        state: PersistedCapacityState,
        observed_at: datetime,
        windows: Sequence[Mapping[str, object]],
        reason: str | None,
        source_version: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderCapacitySnapshot:
    """Four-state provider capacity shape shared by every consumer."""

    provider: str
    supported: bool
    state: ProviderCapacityState
    observed_at: datetime | None
    windows: tuple[UsageWindow, ...]
    reason: str | None
    source_version: str | None

    @classmethod
    def unknown(cls, provider: str, reason: str) -> ProviderCapacitySnapshot:
        """Build the shared unavailable/unsupported response shape."""
        return cls(
            provider=_provider_name(provider),
            supported=False,
            state="unknown",
            observed_at=None,
            windows=(),
            reason=reason,
            source_version=None,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "supported": self.supported,
            "state": self.state,
            "observed_at": datetime_to_iso(self.observed_at),
            "windows": [window.to_dict() for window in self.windows],
            "reason": self.reason,
            "source_version": self.source_version,
        }


class ProviderCapacityService:
    """Single owner of refresh, persistence, normalization, and single-flight."""

    def __init__(
        self,
        storage: CapacityStorage,
        *,
        reporters: Mapping[str, ProviderUsageReporter] | None = None,
        support_resolvers: Mapping[str, SupportResolver] | None = None,
        freshness_seconds: float = 60.0,
        clock: Clock = utc_now,
        run_db: RunDatabase | None = None,
    ) -> None:
        self.storage = storage
        self._reporters = dict({"agy": AgyUsageReporter()} if reporters is None else reporters)
        self._support_resolvers = dict(
            {"agy": ensure_agy_support} if support_resolvers is None else support_resolvers
        )
        self._freshness_seconds = freshness_seconds
        self._clock = clock
        self._run_db = run_db
        self._inflight: dict[tuple[str, str], asyncio.Task[ProviderCapacitySnapshot]] = {}

    @classmethod
    def create_default(
        cls,
        db: HubDatabase,
        *,
        machine_id: str,
        run_db: RunDatabase | None = None,
    ) -> ProviderCapacityService:
        """Build the daemon's database-backed service with AGY registered."""
        return cls(
            ProviderCapacityStorage(db, machine_id=machine_id),
            run_db=run_db,
        )

    async def get(self, provider: str) -> ProviderCapacitySnapshot:
        """Return a fresh row or refresh and normalize a stale/missing one."""
        provider = _provider_name(provider)
        return await self._join_operation(provider, partial(self._get_once, provider))

    async def _get_once(self, provider: str) -> ProviderCapacitySnapshot:
        persisted = await self._run_store(self.storage.get, provider)
        if persisted is not None and self._age_seconds(persisted) <= self._freshness_seconds:
            return _from_record(persisted, state=persisted.state, reason=persisted.reason)
        return await self._refresh_with_fallback(provider, persisted)

    async def refresh(self, provider: str) -> ProviderCapacitySnapshot:
        """Force one shared refresh attempt, preserving last success on failure."""
        provider = _provider_name(provider)
        return await self._join_operation(provider, partial(self._refresh_once, provider))

    async def _refresh_once(self, provider: str) -> ProviderCapacitySnapshot:
        persisted = await self._run_store(self.storage.get, provider)
        return await self._refresh_with_fallback(provider, persisted)

    async def _refresh_with_fallback(
        self,
        provider: str,
        persisted: ProviderCapacityRecord | None,
    ) -> ProviderCapacitySnapshot:
        reporter = self._reporters.get(provider)
        if reporter is None:
            return _unknown(provider, "no usage reporter")

        source_version = "unknown"
        support_resolver = self._support_resolvers.get(provider)
        if support_resolver is not None:
            support = await support_resolver()
            if not support.supported:
                return _unknown(provider, support.reason)
            if support.installed_version is not None:
                source_version = support.installed_version

        try:
            return await self._refresh_and_persist(provider, reporter, source_version)
        except TransientUsageRefreshError as error:
            if persisted is None:
                return _unknown(provider, error.reason)
            return _from_record(persisted, state="stale", reason=error.reason)

    async def _join_operation(
        self,
        provider: str,
        operation: Callable[[], Awaitable[ProviderCapacitySnapshot]],
    ) -> ProviderCapacitySnapshot:
        key = (self.storage.machine_id, provider)
        task = self._inflight.get(key)
        if task is None:

            async def run_operation() -> ProviderCapacitySnapshot:
                return await operation()

            task = asyncio.create_task(run_operation())
            self._inflight[key] = task

            def clear(completed: asyncio.Task[ProviderCapacitySnapshot]) -> None:
                if self._inflight.get(key) is completed:
                    self._inflight.pop(key, None)
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(clear)
        return await asyncio.shield(task)

    async def _refresh_and_persist(
        self,
        provider: str,
        reporter: ProviderUsageReporter,
        source_version: str,
    ) -> ProviderCapacitySnapshot:
        snapshot = await reporter.report()
        if snapshot.provider != provider:
            raise TransientUsageRefreshError(
                provider,
                "provider_mismatch",
                f"reporter returned provider {snapshot.provider!r}",
            )
        if not snapshot.supported:
            raise TransientUsageRefreshError(
                provider,
                "unsupported_snapshot",
                "reporter returned an unsupported success snapshot",
            )
        state: PersistedCapacityState = (
            "exhausted"
            if any(_is_exhausted(window) for window in snapshot.windows)
            else "available"
        )
        reason = "one or more usage windows exhausted" if state == "exhausted" else None
        await self._run_store(
            self.storage.upsert,
            provider=provider,
            state=state,
            observed_at=snapshot.observed_at,
            windows=tuple(window.to_dict() for window in snapshot.windows),
            reason=reason,
            source_version=source_version,
        )
        return ProviderCapacitySnapshot(
            provider=provider,
            supported=True,
            state=state,
            observed_at=snapshot.observed_at,
            windows=snapshot.windows,
            reason=reason,
            source_version=source_version,
        )

    async def _run_store[T](
        self,
        operation: Callable[..., T],
        *args: object,
        **kwargs: object,
    ) -> T:
        if self._run_db is None:
            return await asyncio.to_thread(partial(operation, *args, **kwargs))
        result = await self._run_db(operation, *args, **kwargs)
        return cast(T, result)

    def _age_seconds(self, record: ProviderCapacityRecord) -> float:
        return max(0.0, (self._clock() - record.observed_at).total_seconds())


def _from_record(
    record: ProviderCapacityRecord,
    *,
    state: ProviderCapacityState,
    reason: str | None,
) -> ProviderCapacitySnapshot:
    return ProviderCapacitySnapshot(
        provider=record.provider,
        supported=True,
        state=state,
        observed_at=record.observed_at,
        windows=tuple(_window_from_dict(window) for window in record.windows),
        reason=reason,
        source_version=record.source_version,
    )


def _unknown(provider: str, reason: str) -> ProviderCapacitySnapshot:
    return ProviderCapacitySnapshot.unknown(provider, reason)


def _window_from_dict(value: Mapping[str, object]) -> UsageWindow:
    label = value.get("label")
    unit = value.get("unit")
    used = value.get("used")
    limit = value.get("limit")
    resets_at = value.get("resets_at")
    if not isinstance(label, str) or not label:
        raise TypeError("persisted usage window label must be a non-empty string")
    if not isinstance(unit, str) or not unit:
        raise TypeError("persisted usage window unit must be a non-empty string")
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        raise TypeError("persisted usage window used must be numeric")
    if not isinstance(limit, (int, float)) or isinstance(limit, bool):
        raise TypeError("persisted usage window limit must be numeric")
    if resets_at is not None and not isinstance(resets_at, str):
        raise TypeError("persisted usage window resets_at must be a string or null")
    return UsageWindow(
        label=label,
        used=float(used),
        limit=float(limit),
        unit=unit,
        resets_at=resets_at,
    )


def _is_exhausted(window: UsageWindow) -> bool:
    return window.limit > 0 and window.used >= window.limit


def _provider_name(provider: str) -> str:
    normalized = provider.strip().casefold()
    if not normalized:
        raise ValueError("provider must be non-empty")
    return normalized
