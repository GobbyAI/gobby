from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

import pytest

from gobby.providers.capacity_service import ProviderCapacityService
from gobby.providers.quota_observations import (
    QuotaComponent,
    QuotaObservation,
    component_key,
    decode_quota_details,
    encode_quota_details,
    merge_components,
)
from gobby.providers.usage import UsageWindow
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.provider_capacity import (
    PersistedCapacityState,
    ProviderCapacityRecord,
    ProviderCapacityStorage,
)
from gobby.utils.machine_id import require_machine_id


@pytest.mark.integration
def test_provider_capacity_upsert_replaces_latest_machine_snapshot(
    postgres_db: HubDatabase,
) -> None:
    first_observed = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    second_observed = datetime(2026, 8, 30, 11, 0, tzinfo=UTC)
    now = datetime(2026, 8, 30, 11, 1, tzinfo=UTC)
    storage = ProviderCapacityStorage(
        postgres_db,
        machine_id=require_machine_id(),
        clock=lambda: now,
    )

    storage.upsert(
        provider="agy",
        state="available",
        observed_at=first_observed,
        windows=(
            {
                "label": "Gemini Models — Weekly Limit Remaining",
                "used": 0.3,
                "limit": 1.0,
                "unit": "fraction",
                "resets_at": "2026-09-06T10:00:00Z",
            },
        ),
        reason=None,
        source_version="1.1.18",
        details={},
        expected_observed_at=None,
    )
    storage.upsert(
        provider="agy",
        state="exhausted",
        observed_at=second_observed,
        windows=(
            {
                "label": "Gemini Models — Weekly Limit Remaining",
                "used": 1.0,
                "limit": 1.0,
                "unit": "fraction",
                "resets_at": "2026-09-06T10:00:00Z",
            },
        ),
        reason="weekly limit exhausted",
        source_version="1.1.19",
        details={},
        expected_observed_at=first_observed,
    )

    record = storage.get("agy")
    row = postgres_db.fetchone(
        "SELECT COUNT(*) AS count FROM provider_capacity_snapshots "
        "WHERE machine_id = %s AND provider = %s",
        (require_machine_id(), "agy"),
    )

    assert record is not None
    assert record.state == "exhausted"
    assert record.observed_at == second_observed
    assert record.windows[0]["used"] == 1.0
    assert record.reason == "weekly limit exhausted"
    assert record.source_version == "1.1.19"
    assert record.age_seconds == pytest.approx(60.0)
    assert row is not None
    assert row["count"] == 1


_T0 = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _window(
    *,
    minutes: int,
    used: float,
    observed_at: datetime,
    resets_at: datetime,
    occurrence: str,
    scope: str = "provider",
) -> QuotaObservation:
    """Build one numeric percent-window observation for a Codex limit family."""
    label = "five_hour" if minutes == 300 else "weekly"
    component = QuotaComponent(
        key=component_key("codex", scope, "codex", f"w:{minutes}"),
        provider="codex",
        limit_id="codex",
        scope=scope,
        scope_known=scope == "provider",
        state="exhausted" if used >= 100.0 else "available",
        source_observed_at=observed_at,
        occurrence=occurrence,
        resets_at=resets_at,
        window=UsageWindow(label=label, used=used, limit=100.0, unit="percent", resets_at=None),
    )
    return QuotaObservation(provider="codex", components=(component,))


class _RivalWriteStorage:
    """Lets a rival writer commit a different component just before the first upsert."""

    def __init__(self, inner: ProviderCapacityStorage, rival: QuotaObservation) -> None:
        self._inner = inner
        self._rival = rival
        self.upsert_calls = 0

    @property
    def machine_id(self) -> str:
        return self._inner.machine_id

    def get(self, provider: str) -> ProviderCapacityRecord | None:
        return self._inner.get(provider)

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
        if self.upsert_calls == 1:
            record = self._inner.get(provider)
            components, pane_hints = decode_quota_details(record.details if record else {})
            merged, _ = merge_components(components, self._rival.components)
            rival_revision = (record.observed_at if record else _T0) + timedelta(microseconds=1)
            self._inner.upsert(
                provider=provider,
                state="available",
                observed_at=rival_revision,
                windows=(),
                reason=None,
                source_version="unknown",
                details=encode_quota_details(merged, pane_hints),
                expected_observed_at=record.observed_at if record else None,
            )
        return self._inner.upsert(
            provider=provider,
            state=state,
            observed_at=observed_at,
            windows=windows,
            reason=reason,
            source_version=source_version,
            details=details,
            expected_observed_at=expected_observed_at,
        )


@pytest.mark.integration
async def test_observation_write_admission(postgres_db: HubDatabase) -> None:
    clock = _Clock(_T0)
    storage = ProviderCapacityStorage(postgres_db, machine_id=require_machine_id(), clock=clock)
    service = ProviderCapacityService(storage, reporters={}, support_resolvers={}, clock=clock)
    t9 = _T0
    t10 = _T0 + timedelta(minutes=1)
    weekly_key = component_key("codex", "provider", "codex", "w:10080")
    five_key = component_key("codex", "provider", "codex", "w:300")

    def stored() -> dict[str, QuotaComponent]:
        record = storage.get("codex")
        assert record is not None
        components, _ = decode_quota_details(record.details)
        return components

    # Disjoint out-of-order readings are both admitted, each on its own clock.
    await service.observe(
        _window(
            minutes=10080,
            used=40.0,
            observed_at=t10,
            resets_at=t10 + timedelta(days=7),
            occurrence="weekly-10",
        )
    )
    snapshot = await service.observe(
        _window(
            minutes=300,
            used=10.0,
            observed_at=t9,
            resets_at=t9 + timedelta(hours=5),
            occurrence="five-9",
        )
    )
    assert snapshot.state == "available"
    assert set(stored()) == {weekly_key, five_key}
    assert stored()[five_key].source_observed_at == t9

    # An older timestamp for the same component is rejected.
    await service.observe(
        _window(
            minutes=300,
            used=5.0,
            observed_at=t9 - timedelta(minutes=1),
            resets_at=t9 + timedelta(hours=5),
            occurrence="five-8",
        )
    )
    five_window = stored()[five_key].window
    assert five_window is not None
    assert five_window.used == 10.0

    # An earlier supplied reset for the same component is rejected.
    await service.observe(
        _window(
            minutes=300,
            used=12.0,
            observed_at=t9 + timedelta(seconds=30),
            resets_at=t9 + timedelta(hours=4),
            occurrence="five-9-early-reset",
        )
    )
    assert stored()[five_key].resets_at == t9 + timedelta(hours=5)

    # A duplicate occurrence writes nothing, so the row revision and component clocks hold.
    revision = storage.get("codex")
    assert revision is not None
    await service.observe(
        _window(
            minutes=10080,
            used=40.0,
            observed_at=t10,
            resets_at=t10 + timedelta(days=7),
            occurrence="weekly-10",
        )
    )
    after_duplicate = storage.get("codex")
    assert after_duplicate is not None
    assert after_duplicate.observed_at == revision.observed_at

    # Equal timestamps with contradictory values are ambiguous and rejected.
    await service.observe(
        _window(
            minutes=10080,
            used=55.0,
            observed_at=t10,
            resets_at=t10 + timedelta(days=7),
            occurrence="weekly-10-conflict",
        )
    )
    weekly = stored()[weekly_key]
    assert weekly.window is not None
    assert weekly.window.used == 40.0

    # A later weekly refresh leaves the five-hour component to age on its own clock.
    refreshed_at = _T0 + timedelta(seconds=1000)
    clock.now = refreshed_at
    await service.observe(
        _window(
            minutes=10080,
            used=45.0,
            observed_at=refreshed_at,
            resets_at=refreshed_at + timedelta(days=7),
            occurrence="weekly-refresh",
        )
    )
    assert stored()[five_key].source_observed_at == t9
    assert (await service.get("codex")).state == "stale"

    # A model-scoped exhaustion is not cleared by a provider-wide positive reading.
    model_key = component_key("codex", "model:gpt-x", "codex", "w:10080")
    await service.observe(
        _window(
            minutes=10080,
            used=100.0,
            observed_at=refreshed_at,
            resets_at=refreshed_at + timedelta(days=1),
            occurrence="model-x-exhausted",
            scope="model:gpt-x",
        )
    )
    snapshot = await service.observe(
        _window(
            minutes=10080,
            used=20.0,
            observed_at=refreshed_at + timedelta(minutes=1),
            resets_at=refreshed_at + timedelta(days=7),
            occurrence="provider-positive",
        )
    )
    assert model_key in stored()
    assert stored()[model_key].state == "exhausted"
    assert snapshot.state == "exhausted"

    # A competing write between this service's read and its upsert forces a remerge and retry;
    # neither writer's component is lost.
    rival_key = component_key("codex", "provider", "codex", "w:300-rival")
    rival = QuotaObservation(
        provider="codex",
        components=(
            QuotaComponent(
                key=rival_key,
                provider="codex",
                limit_id="codex-rival",
                scope="provider",
                scope_known=True,
                state="available",
                source_observed_at=refreshed_at,
                occurrence="rival-occ",
                resets_at=refreshed_at + timedelta(hours=5),
            ),
        ),
    )
    racing = _RivalWriteStorage(storage, rival)
    racing_service = ProviderCapacityService(
        racing, reporters={}, support_resolvers={}, clock=clock
    )
    await racing_service.observe(
        _window(
            minutes=300,
            used=20.0,
            observed_at=refreshed_at + timedelta(minutes=2),
            resets_at=refreshed_at + timedelta(hours=5),
            occurrence="five-racing",
        )
    )
    assert racing.upsert_calls >= 2
    assert {rival_key, five_key}.issubset(stored())
