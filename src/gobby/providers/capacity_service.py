"""Shared persisted provider-capacity service for HTTP and MCP consumers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import partial
from typing import Literal, Protocol, cast

from gobby.hooks.background_tasks import create_background_task
from gobby.providers.quota_alerts import (
    HINT_SECTION,
    QUOTA_SECTION,
    AlertEdge,
    AlertSink,
    retire_pane_hint_generations,
    transition,
    transition_hint,
)
from gobby.providers.quota_observations import (
    QuotaAggregate,
    QuotaComponent,
    QuotaObservation,
    aggregate_components,
    decode_quota_details,
    encode_quota_details,
    merge_components,
)
from gobby.providers.quota_signals import PaneQuotaHint, normalize_reporter_snapshot
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

logger = logging.getLogger(__name__)

_MAX_CAS_ATTEMPTS = 5
_REVISION_STEP = timedelta(microseconds=1)


class QuotaWriteContentionError(RuntimeError):
    """Concurrent writers kept moving one provider row past this writer's compare-and-set."""

    def __init__(self, provider: str) -> None:
        super().__init__(f"provider capacity write for {provider!r} kept losing compare-and-set")
        self.provider = provider


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
        details: Mapping[str, object],
        expected_observed_at: datetime | None,
    ) -> bool: ...


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
    details: Mapping[str, object] = field(default_factory=dict)

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
            "details": dict(self.details),
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
        alert_sink: AlertSink | None = None,
    ) -> None:
        self.storage = storage
        self._reporters = dict({"agy": AgyUsageReporter()} if reporters is None else reporters)
        self._support_resolvers = dict(
            {"agy": ensure_agy_support} if support_resolvers is None else support_resolvers
        )
        self._freshness_seconds = freshness_seconds
        self._clock = clock
        self._run_db = run_db
        self._alert_sink = alert_sink
        self._inflight: dict[tuple[str, str], asyncio.Task[ProviderCapacitySnapshot]] = {}
        # Each pane-scan owner's last reported live generations, per provider.
        self._pane_live: dict[tuple[str, str], frozenset[str]] = {}
        self._write_locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def create_default(
        cls,
        db: HubDatabase,
        *,
        machine_id: str,
        run_db: RunDatabase | None = None,
        alert_sink: AlertSink | None = None,
    ) -> ProviderCapacityService:
        """Build the daemon's database-backed service with AGY registered."""
        return cls(
            ProviderCapacityStorage(db, machine_id=machine_id),
            run_db=run_db,
            alert_sink=alert_sink,
        )

    async def get(self, provider: str) -> ProviderCapacitySnapshot:
        """Read persisted components; reporter providers refresh when their last read is stale."""
        provider = _provider_name(provider)
        return await self._join_operation(provider, partial(self._get_once, provider))

    async def _get_once(self, provider: str) -> ProviderCapacitySnapshot:
        record = await self._run_store(self.storage.get, provider)
        if provider not in self._reporters:
            return self._read(provider, record)
        aggregate = _aggregate(record, now=self._clock())
        if (
            aggregate.observed_at is not None
            and self._age_seconds(aggregate.observed_at) <= self._freshness_seconds
        ):
            return _snapshot(provider, aggregate, _source_version(record))
        return await self._refresh_with_fallback(provider, record)

    async def refresh(self, provider: str) -> ProviderCapacitySnapshot:
        """Force one shared refresh attempt, preserving last success on failure."""
        provider = _provider_name(provider)
        return await self._join_operation(provider, partial(self._refresh_once, provider))

    async def _refresh_once(self, provider: str) -> ProviderCapacitySnapshot:
        record = await self._run_store(self.storage.get, provider)
        if provider not in self._reporters:
            return self._read(provider, record)
        return await self._refresh_with_fallback(provider, record)

    async def observe(self, observation: QuotaObservation) -> ProviderCapacitySnapshot:
        """Admit verified components through the per-provider CAS path and return the read."""
        provider = _provider_name(observation.provider)
        edges: tuple[AlertEdge, ...] = ()
        async with self._write_lock(provider):
            for _ in range(_MAX_CAS_ATTEMPTS):
                record = await self._run_store(self.storage.get, provider)
                components, pane_hints = _decode(record)
                merged, outcomes = merge_components(components, observation.components)
                if "accepted" not in outcomes.values():
                    snapshot = self._read(provider, record)
                    break
                admitted = QuotaObservation(
                    provider=observation.provider,
                    components=tuple(
                        component
                        for component in observation.components
                        if outcomes.get(component.key) == "accepted"
                    ),
                    source_version=observation.source_version,
                )
                alerts = transition(_row_details(record), admitted)
                aggregate = await self._write(
                    provider,
                    record,
                    merged,
                    pane_hints,
                    observation.source_version,
                    quota_alerts=alerts.details,
                    pane_hint_alerts=_row_section(record, HINT_SECTION),
                )
                if aggregate is not None:
                    snapshot = _snapshot(provider, aggregate, observation.source_version)
                    edges = alerts.edges
                    break
            else:
                raise QuotaWriteContentionError(provider)
        self._schedule_alerts(edges)
        return snapshot

    async def retire_pane_generations(
        self, provider: str, live: Collection[str], *, owner: str = ""
    ) -> None:
        """Drop pane-hint generations that no pane-scan owner still reports live.

        Each owner (the spawned-run and placed-seat scans) reports its own live set, so
        one owner's pass never drops a generation the other owner is still watching.
        """
        name = _provider_name(provider)
        async with self._write_lock(name):
            self._pane_live[(name, owner)] = frozenset(live)
            keep = frozenset().union(
                *(
                    gens
                    for (provider_name, _), gens in self._pane_live.items()
                    if provider_name == name
                )
            )
            for _ in range(_MAX_CAS_ATTEMPTS):
                record = await self._run_store(self.storage.get, name)
                section = _row_section(record, HINT_SECTION)
                retired = retire_pane_hint_generations(section, keep)
                if retired == section:
                    return
                components, pane_hints = _decode(record)
                written = await self._write(
                    name,
                    record,
                    components,
                    pane_hints,
                    _source_version(record) or "unknown",
                    quota_alerts=_row_section(record, QUOTA_SECTION),
                    pane_hint_alerts=retired,
                )
                if written is not None:
                    return
            raise QuotaWriteContentionError(name)

    async def observe_pane_hint(self, hint: PaneQuotaHint, *, generation: str) -> bool:
        """Record one pane hint for its live generation; False when its occurrence is a replay."""
        provider = _provider_name(hint.provider)
        edges: tuple[AlertEdge, ...] = ()
        async with self._write_lock(provider):
            for _ in range(_MAX_CAS_ATTEMPTS):
                record = await self._run_store(self.storage.get, provider)
                components, pane_hints = _decode(record)
                alerts = transition_hint(
                    _row_details(record), hint, generation=generation, now=self._clock()
                )
                if alerts.duplicate:
                    return False
                written = await self._write(
                    provider,
                    record,
                    components,
                    pane_hints,
                    _source_version(record) or "unknown",
                    quota_alerts=_row_section(record, QUOTA_SECTION),
                    pane_hint_alerts=alerts.details,
                )
                if written is not None:
                    edges = alerts.edges
                    break
            else:
                raise QuotaWriteContentionError(provider)
        self._schedule_alerts(edges)
        return True

    def _schedule_alerts(self, edges: Sequence[AlertEdge]) -> None:
        """Hand committed edges to the sink as one background task, after the lock is released."""
        if self._alert_sink is None or not edges:
            return
        create_background_task(_deliver_alerts(self._alert_sink, edges))

    async def _refresh_with_fallback(
        self,
        provider: str,
        record: ProviderCapacityRecord | None,
    ) -> ProviderCapacitySnapshot:
        reporter = self._reporters[provider]
        source_version = "unknown"
        support_resolver = self._support_resolvers.get(provider)
        if support_resolver is not None:
            support = await support_resolver()
            if not support.supported:
                return ProviderCapacitySnapshot.unknown(provider, support.reason)
            if support.installed_version is not None:
                source_version = support.installed_version

        try:
            return await self._refresh_and_persist(provider, reporter, source_version)
        except TransientUsageRefreshError as error:
            return self._stale(provider, record, error.reason)

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
        observation = replace(normalize_reporter_snapshot(snapshot), source_version=source_version)
        return await self.observe(observation)

    async def _write(
        self,
        provider: str,
        record: ProviderCapacityRecord | None,
        components: Mapping[str, QuotaComponent],
        pane_hints: Mapping[str, Mapping[str, str]],
        source_version: str,
        *,
        quota_alerts: Mapping[str, object],
        pane_hint_alerts: Mapping[str, object],
    ) -> QuotaAggregate | None:
        """Compare-and-set one row revision; None when a concurrent writer won the race."""
        now = self._clock()
        aggregate = aggregate_components(components, now=now)
        revision = now if record is None else max(now, record.observed_at + _REVISION_STEP)
        written = await self._run_store(
            self.storage.upsert,
            provider=provider,
            state=(
                "exhausted"
                if any(component.state == "exhausted" for component in components.values())
                else "available"
            ),
            observed_at=revision,
            windows=tuple(window.to_dict() for window in aggregate.windows),
            reason=aggregate.reason,
            source_version=source_version,
            details={
                **encode_quota_details(components, pane_hints),
                QUOTA_SECTION: dict(quota_alerts),
                HINT_SECTION: dict(pane_hint_alerts),
            },
            expected_observed_at=None if record is None else record.observed_at,
        )
        return aggregate if written else None

    def _read(
        self,
        provider: str,
        record: ProviderCapacityRecord | None,
    ) -> ProviderCapacitySnapshot:
        aggregate = _aggregate(record, now=self._clock())
        return _snapshot(provider, aggregate, _source_version(record))

    def _stale(
        self,
        provider: str,
        record: ProviderCapacityRecord | None,
        reason: str,
    ) -> ProviderCapacitySnapshot:
        """Keep the last success after a transient failure; aged components read stale."""
        aggregate = _aggregate(record, now=self._clock())
        source_version = _source_version(record)
        if aggregate.state == "unknown":
            return ProviderCapacitySnapshot.unknown(provider, reason)
        if aggregate.state == "exhausted":
            return _snapshot(provider, aggregate, source_version)
        return ProviderCapacitySnapshot(
            provider=provider,
            supported=True,
            state="stale",
            observed_at=aggregate.observed_at,
            windows=aggregate.windows,
            reason=reason,
            source_version=source_version,
            details=aggregate.details,
        )

    def _write_lock(self, provider: str) -> asyncio.Lock:
        lock = self._write_locks.get(provider)
        if lock is None:
            lock = self._write_locks[provider] = asyncio.Lock()
        return lock

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

    def _age_seconds(self, observed_at: datetime) -> float:
        return max(0.0, (self._clock() - observed_at).total_seconds())


def _row_details(record: ProviderCapacityRecord | None) -> Mapping[str, object]:
    """The full persisted row details; transitions read their own alert section."""
    return {} if record is None else record.details


def _row_section(record: ProviderCapacityRecord | None, name: str) -> dict[str, object]:
    """A copy of one named alert section of the row; empty when absent or malformed."""
    section = _row_details(record).get(name)
    return dict(section) if isinstance(section, Mapping) else {}


async def _deliver_alerts(sink: AlertSink, edges: Sequence[AlertEdge]) -> None:
    for edge in edges:
        try:
            await sink(edge)
        except Exception as exc:
            logger.warning("quota alert sink failed: %s", type(exc).__name__)


def _snapshot(
    provider: str,
    aggregate: QuotaAggregate,
    source_version: str | None,
) -> ProviderCapacitySnapshot:
    if aggregate.state == "unknown":
        return ProviderCapacitySnapshot.unknown(provider, _no_observation_reason(provider))
    return ProviderCapacitySnapshot(
        provider=provider,
        supported=True,
        state=aggregate.state,
        observed_at=aggregate.observed_at,
        windows=aggregate.windows,
        reason=aggregate.reason,
        source_version=source_version,
        details=aggregate.details,
    )


def _aggregate(record: ProviderCapacityRecord | None, *, now: datetime) -> QuotaAggregate:
    components, _ = _decode(record)
    return aggregate_components(components, now=now)


def _decode(
    record: ProviderCapacityRecord | None,
) -> tuple[dict[str, QuotaComponent], dict[str, dict[str, str]]]:
    return decode_quota_details({} if record is None else record.details)


def _source_version(record: ProviderCapacityRecord | None) -> str | None:
    return None if record is None else record.source_version


def _no_observation_reason(provider: str) -> str:
    return f"no {provider} quota observation"


def _provider_name(provider: str) -> str:
    normalized = provider.strip().casefold()
    if not normalized:
        raise ValueError("provider must be non-empty")
    return normalized
