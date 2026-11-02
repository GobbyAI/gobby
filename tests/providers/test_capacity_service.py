from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from gobby.providers.capacity_service import ProviderCapacityService
from gobby.providers.quota_observations import decode_quota_details
from gobby.providers.quota_signals import normalize_reporter_snapshot
from gobby.providers.usage import (
    ProviderUsageSnapshot,
    TransientUsageRefreshError,
    UsageWindow,
)
from gobby.storage.provider_capacity import (
    PersistedCapacityState,
    ProviderCapacityRecord,
)

_MACHINE_ID = "dddddddd-dddd-4ddd-8ddd-000000000001"
_OBSERVED_AT = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _clock() -> datetime:
    return _OBSERVED_AT


@dataclass(frozen=True, slots=True)
class _SupportRecord:
    installed_version: str | None
    supported: bool
    reason: str


class _Reporter:
    provider = "agy"

    def __init__(
        self,
        outcome: ProviderUsageSnapshot | BaseException,
        before_return: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.outcome = outcome
        self.before_return = before_return
        self.calls = 0

    async def report(self) -> ProviderUsageSnapshot:
        self.calls += 1
        if self.before_return is not None:
            await self.before_return()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class _Storage:
    machine_id = _MACHINE_ID

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self.records: dict[str, ProviderCapacityRecord] = {}
        self.upsert_calls = 0

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
    ) -> bool:
        self.upsert_calls += 1
        current = self.records.get(provider)
        if (None if current is None else current.observed_at) != expected_observed_at:
            return False
        self.records[provider] = ProviderCapacityRecord(
            machine_id=self.machine_id,
            provider=provider,
            state=state,
            observed_at=observed_at,
            windows=tuple(dict(window) for window in windows),
            reason=reason,
            source_version=source_version,
            age_seconds=max(0.0, (self._clock() - observed_at).total_seconds()),
            details=dict(details),
        )
        return True

    def get(self, provider: str) -> ProviderCapacityRecord | None:
        record = self.records.get(provider)
        if record is None:
            return None
        return ProviderCapacityRecord(
            machine_id=record.machine_id,
            provider=record.provider,
            state=record.state,
            observed_at=record.observed_at,
            windows=record.windows,
            reason=record.reason,
            source_version=record.source_version,
            age_seconds=max(0.0, (self._clock() - record.observed_at).total_seconds()),
            details=record.details,
        )


def _snapshot(*, remaining_fraction: float) -> ProviderUsageSnapshot:
    return ProviderUsageSnapshot(
        provider="agy",
        observed_at=_OBSERVED_AT,
        supported=True,
        windows=(
            UsageWindow(
                label="Gemini Models — Weekly Limit Remaining",
                used=1.0 - remaining_fraction,
                limit=1.0,
                unit="fraction",
                resets_at="2026-09-06T12:00:00Z",
            ),
        ),
        raw={},
    )


async def _supported() -> _SupportRecord:
    return _SupportRecord(installed_version="1.1.18", supported=True, reason="supported")


def _support_resolver(record: _SupportRecord) -> Callable[[], Awaitable[_SupportRecord]]:
    async def resolve() -> _SupportRecord:
        return record

    return resolve


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_persists_exhausted_snapshot_and_reuses_fresh_row() -> None:
    storage = _Storage(_clock)
    reporter = _Reporter(_snapshot(remaining_fraction=0.0))
    service = ProviderCapacityService(
        storage,
        reporters={"agy": reporter},
        support_resolvers={"agy": _supported},
        clock=_clock,
    )

    first = await service.get("agy")
    second = await service.get("agy")

    assert first == second
    assert first.state == "exhausted"
    assert first.supported is True
    assert first.observed_at == _OBSERVED_AT
    assert first.windows == _snapshot(remaining_fraction=0.0).windows
    assert first.source_version == "1.1.18"
    assert reporter.calls == 1
    assert storage.upsert_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fresh_persisted_snapshot_survives_service_restart_without_probe() -> None:
    storage = _Storage(_clock)
    first_reporter = _Reporter(_snapshot(remaining_fraction=0.7))
    first_service = ProviderCapacityService(
        storage,
        reporters={"agy": first_reporter},
        support_resolvers={"agy": _supported},
        clock=_clock,
    )
    await first_service.get("agy")
    restarted_reporter = _Reporter(AssertionError("fresh persisted row must avoid re-probe"))
    restarted_service = ProviderCapacityService(
        storage,
        reporters={"agy": restarted_reporter},
        support_resolvers={"agy": _supported},
        clock=_clock,
    )

    result = await restarted_service.get("agy")

    assert result.state == "available"
    assert result.observed_at == _OBSERVED_AT
    assert restarted_reporter.calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transient_failure_keeps_prior_windows_as_stale() -> None:
    now = _OBSERVED_AT + timedelta(seconds=61)
    storage = _Storage(lambda: now)
    seeder = ProviderCapacityService(
        storage,
        reporters={},
        support_resolvers={},
        clock=lambda: now,
    )
    await seeder.observe(normalize_reporter_snapshot(_snapshot(remaining_fraction=0.7)))
    reporter = _Reporter(TransientUsageRefreshError("agy", "timeout", "timed out after 15 seconds"))
    service = ProviderCapacityService(
        storage,
        reporters={"agy": reporter},
        support_resolvers={"agy": _supported},
        clock=lambda: now,
    )

    result = await service.get("agy")

    assert result.state == "stale"
    assert result.supported is True
    assert result.observed_at == _OBSERVED_AT
    assert result.windows == _snapshot(remaining_fraction=0.7).windows
    assert result.reason == "timed out after 15 seconds"
    assert storage.upsert_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transient_failure_without_prior_snapshot_is_unknown() -> None:
    storage = _Storage(lambda: _OBSERVED_AT)
    reporter = _Reporter(TransientUsageRefreshError("agy", "command_failed", "Please sign in"))
    service = ProviderCapacityService(
        storage,
        reporters={"agy": reporter},
        support_resolvers={"agy": _supported},
        clock=lambda: _OBSERVED_AT,
    )

    result = await service.get("agy")

    assert result.state == "unknown"
    assert result.supported is False
    assert result.windows == ()
    assert result.reason == "Please sign in"
    assert storage.upsert_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("installed_version", "reason"),
    [
        pytest.param("1.1.17", "AGY 1.1.18+ required", id="sub-floor"),
        pytest.param(None, "AGY binary not found", id="absent"),
        pytest.param("2.5.0", "AGY 2.5 is unsupported", id="unsupported-major"),
    ],
)
async def test_unsupported_record_returns_unknown_before_reporter(
    installed_version: str | None,
    reason: str,
) -> None:
    storage = _Storage(lambda: _OBSERVED_AT)
    reporter = _Reporter(AssertionError("unsupported provider must not run reporter"))
    support = _SupportRecord(
        installed_version=installed_version,
        supported=False,
        reason=reason,
    )
    service = ProviderCapacityService(
        storage,
        reporters={"agy": reporter},
        support_resolvers={"agy": _support_resolver(support)},
        clock=lambda: _OBSERVED_AT,
    )

    result = await service.get("agy")

    assert result.state == "unknown"
    assert result.supported is False
    assert result.reason == reason
    assert reporter.calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_without_reporter_is_unknown() -> None:
    service = ProviderCapacityService(
        _Storage(lambda: _OBSERVED_AT),
        reporters={},
        support_resolvers={},
        clock=lambda: _OBSERVED_AT,
    )

    result = await service.get("claude")

    assert result.to_dict() == {
        "provider": "claude",
        "supported": False,
        "state": "unknown",
        "observed_at": None,
        "windows": [],
        "reason": "no claude quota observation",
        "source_version": None,
        "details": {},
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_success_joins_one_refresh_task() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_release() -> None:
        started.set()
        await release.wait()

    storage = _Storage(lambda: _OBSERVED_AT)
    reporter = _Reporter(_snapshot(remaining_fraction=0.7), wait_for_release)
    service = ProviderCapacityService(
        storage,
        reporters={"agy": reporter},
        support_resolvers={"agy": _supported},
        clock=lambda: _OBSERVED_AT,
    )

    first = asyncio.create_task(service.get("agy"))
    await started.wait()
    second = asyncio.create_task(service.get("agy"))
    await asyncio.sleep(0)
    assert reporter.calls == 1
    release.set()

    first_result, second_result = await asyncio.gather(first, second)

    assert first_result == second_result
    assert first_result.state == "available"
    assert reporter.calls == 1
    assert storage.upsert_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["timeout", "command_failed"])
async def test_concurrent_failure_joins_one_refresh_task(code: str) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_release() -> None:
        started.set()
        await release.wait()

    storage = _Storage(lambda: _OBSERVED_AT)
    reporter = _Reporter(
        TransientUsageRefreshError("agy", code, f"{code} reason"),
        wait_for_release,
    )
    service = ProviderCapacityService(
        storage,
        reporters={"agy": reporter},
        support_resolvers={"agy": _supported},
        clock=lambda: _OBSERVED_AT,
    )

    first = asyncio.create_task(service.get("agy"))
    await started.wait()
    second = asyncio.create_task(service.get("agy"))
    await asyncio.sleep(0)
    assert reporter.calls == 1
    release.set()

    first_result, second_result = await asyncio.gather(first, second)

    assert first_result == second_result
    assert first_result.state == "unknown"
    assert first_result.reason == f"{code} reason"
    assert reporter.calls == 1
    assert storage.upsert_calls == 0


class _MovableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


_REPORTER_T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def _agy_reporter_snapshot(
    *, at: datetime, windows: Sequence[tuple[str, float, str]]
) -> ProviderUsageSnapshot:
    """Build a successful AGY reporter snapshot from (label, used, resets_at) percent windows."""
    return ProviderUsageSnapshot(
        provider="agy",
        observed_at=at,
        supported=True,
        windows=tuple(
            UsageWindow(label=label, used=used, limit=100.0, unit="percent", resets_at=resets_at)
            for label, used, resets_at in windows
        ),
        raw={},
    )


def _stored_details(storage: _Storage) -> Mapping[str, object]:
    record = storage.get("agy")
    assert record is not None
    return record.details


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reporter_omitted_window_keeps_its_clock() -> None:
    clock = _MovableClock(_REPORTER_T0)
    storage = _Storage(clock)
    service = ProviderCapacityService(storage, reporters={}, support_resolvers={}, clock=clock)
    five = ("five_hour", 10.0, "2026-11-02T17:00:00Z")
    seven = ("seven_day", 20.0, "2026-11-09T12:00:00Z")
    sonnet = ("sonnet", 5.0, "2026-11-09T12:00:00Z")
    await service.observe(
        normalize_reporter_snapshot(
            _agy_reporter_snapshot(at=_REPORTER_T0, windows=(five, seven, sonnet))
        )
    )

    clock.now = _REPORTER_T0 + timedelta(minutes=17)
    snapshot = await service.observe(
        normalize_reporter_snapshot(_agy_reporter_snapshot(at=clock.now, windows=(five, seven)))
    )

    components, _ = decode_quota_details(_stored_details(storage))
    sonnet_components = [c for c in components.values() if c.limit_id == "sonnet"]
    assert [c.source_observed_at for c in sonnet_components] == [_REPORTER_T0]
    assert snapshot.state == "stale"
    assert {window.label for window in snapshot.windows} == {"five_hour", "seven_day"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reporter_earlier_reset_is_rejected() -> None:
    clock = _MovableClock(_REPORTER_T0)
    storage = _Storage(clock)
    service = ProviderCapacityService(storage, reporters={}, support_resolvers={}, clock=clock)
    await service.observe(
        normalize_reporter_snapshot(
            _agy_reporter_snapshot(
                at=_REPORTER_T0, windows=(("five_hour", 10.0, "2026-11-02T17:00:00Z"),)
            )
        )
    )

    clock.now = _REPORTER_T0 + timedelta(minutes=2)
    await service.observe(
        normalize_reporter_snapshot(
            _agy_reporter_snapshot(
                at=clock.now, windows=(("five_hour", 10.0, "2026-11-02T16:00:00Z"),)
            )
        )
    )

    components, _ = decode_quota_details(_stored_details(storage))
    (five,) = components.values()
    assert five.resets_at == datetime(2026, 11, 2, 17, 0, tzinfo=UTC)
    assert five.source_observed_at == _REPORTER_T0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_reset_positive_reading_does_not_clear_exhaustion() -> None:
    clock = _MovableClock(_REPORTER_T0)
    storage = _Storage(clock)
    service = ProviderCapacityService(storage, reporters={}, support_resolvers={}, clock=clock)
    await service.observe(
        normalize_reporter_snapshot(
            _agy_reporter_snapshot(
                at=_REPORTER_T0, windows=(("five_hour", 100.0, "2026-11-02T17:00:00Z"),)
            )
        )
    )

    clock.now = _REPORTER_T0 + timedelta(minutes=1)
    snapshot = await service.observe(
        normalize_reporter_snapshot(
            _agy_reporter_snapshot(
                at=clock.now, windows=(("five_hour", 20.0, "2026-11-02T17:00:00Z"),)
            )
        )
    )

    assert snapshot.state == "exhausted"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_later_reset_recovers_exhaustion() -> None:
    clock = _MovableClock(_REPORTER_T0)
    storage = _Storage(clock)
    service = ProviderCapacityService(storage, reporters={}, support_resolvers={}, clock=clock)
    await service.observe(
        normalize_reporter_snapshot(
            _agy_reporter_snapshot(
                at=_REPORTER_T0, windows=(("five_hour", 100.0, "2026-11-02T17:00:00Z"),)
            )
        )
    )

    clock.now = _REPORTER_T0 + timedelta(minutes=1)
    snapshot = await service.observe(
        normalize_reporter_snapshot(
            _agy_reporter_snapshot(
                at=clock.now, windows=(("five_hour", 20.0, "2026-11-02T22:00:00Z"),)
            )
        )
    )

    assert snapshot.state == "available"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_details_carry_component_metadata() -> None:
    clock = _MovableClock(_REPORTER_T0)
    storage = _Storage(clock)
    service = ProviderCapacityService(storage, reporters={}, support_resolvers={}, clock=clock)
    five = ("five_hour", 10.0, "2026-11-02T17:00:00Z")
    seven = ("seven_day", 20.0, "2026-11-09T12:00:00Z")
    sonnet = ("sonnet", 5.0, "2026-11-09T12:00:00Z")
    await service.observe(
        normalize_reporter_snapshot(
            _agy_reporter_snapshot(at=_REPORTER_T0, windows=(five, seven, sonnet))
        )
    )

    clock.now = _REPORTER_T0 + timedelta(minutes=17)
    await service.observe(
        normalize_reporter_snapshot(_agy_reporter_snapshot(at=clock.now, windows=(five, seven)))
    )
    snapshot = await service.get("agy")

    entries = snapshot.details["components"]
    assert isinstance(entries, dict)
    by_limit = {entry["limit_id"]: entry for entry in entries.values()}
    assert set(by_limit["sonnet"]) == {
        "limit_id",
        "scope",
        "state",
        "fresh",
        "stale_reason",
        "source_observed_at",
        "resets_at",
    }
    assert by_limit["sonnet"]["fresh"] is False
    assert by_limit["sonnet"]["stale_reason"] == "source_older_than_900s"
    assert by_limit["five_hour"]["fresh"] is True
    assert by_limit["five_hour"]["stale_reason"] is None
    assert snapshot.to_dict()["details"] == snapshot.details
