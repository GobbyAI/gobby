"""Operator quota alerts: edges, persisted dedupe, recovery rules, sink degradation, and carriers.

Edges come from real service admission (observe / observe_pane_hint) with an in-memory
storage fake that keeps the row CAS contract. The sink is a recording fake; delivery
failures are exercised through the real sink builder.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gobby.config.communications import CommunicationsConfig
from gobby.providers.capacity_service import ProviderCapacityService
from gobby.providers.codex_rate_limits import observation_from_rate_limits
from gobby.providers.quota_alerts import (
    AlertEdge,
    AlertKind,
    build_operator_alert_sink,
    render_alert,
)
from gobby.providers.quota_observations import QuotaComponent, QuotaObservation, component_key
from gobby.providers.quota_signals import PaneQuotaHint
from gobby.providers.usage import (
    ProviderUsageReporter,
    ProviderUsageSnapshot,
    TransientUsageRefreshError,
    UsageWindow,
)
from gobby.storage.provider_capacity import PersistedCapacityState, ProviderCapacityRecord

_MACHINE_ID = "dddddddd-dddd-4ddd-8ddd-000000000003"
_T0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
_RESET = _T0 + timedelta(hours=5)
_NOTIFY_COOLDOWN = timedelta(seconds=900)

type _Key = tuple[AlertKind, str | None, int | None, str | None]


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _Storage:
    """In-memory stand-in for ProviderCapacityStorage with the same CAS contract."""

    machine_id = _MACHINE_ID

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self._records: dict[str, ProviderCapacityRecord] = {}

    def get(self, provider: str) -> ProviderCapacityRecord | None:
        record = self._records.get(provider)
        if record is None:
            return None
        age = max(0.0, (self._clock() - record.observed_at).total_seconds())
        return replace(record, age_seconds=age)

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
        current = self._records.get(provider)
        current_revision = None if current is None else current.observed_at
        if current_revision != expected_observed_at:
            return False
        self._records[provider] = ProviderCapacityRecord(
            machine_id=self.machine_id,
            provider=provider,
            state=state,
            observed_at=observed_at,
            windows=tuple(dict(window) for window in windows),
            reason=reason,
            source_version=source_version,
            age_seconds=0.0,
            details=dict(details),
        )
        return True


class _Sink:
    """Records each delivered edge as a comparable key, then clears."""

    def __init__(self) -> None:
        self._edges: list[AlertEdge] = []

    async def __call__(self, edge: AlertEdge) -> None:
        self._edges.append(edge)

    def take(self) -> list[_Key]:
        keys = [(e.kind, e.window, e.percent, e.credit_balance) for e in self._edges]
        self._edges.clear()
        return keys


class _FailingReporter:
    @property
    def provider(self) -> str:
        return "codex"

    async def report(self) -> ProviderUsageSnapshot:
        raise TransientUsageRefreshError("codex", "timeout", "timed out after 15 seconds")


class _Channel:
    """Operator channel fake: records successful sends; can fail or hang on demand."""

    def __init__(self, *, error: BaseException | None = None, hang: bool = False) -> None:
        self.error = error
        self.hang = hang
        self.sent: list[tuple[str, str]] = []

    async def send_message(
        self,
        channel_name: str,
        content: str,
        session_id: str | None = None,
        metadata: dict[str, object] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        if self.hang:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error
        self.sent.append((channel_name, content))


async def _settle() -> None:
    """Let scheduled sink tasks run to completion; the recording sink never awaits."""
    for _ in range(5):
        await asyncio.sleep(0)


def _service(
    clock: _Clock,
    storage: _Storage,
    sink: _Sink | None = None,
    *,
    reporters: Mapping[str, ProviderUsageReporter] | None = None,
) -> ProviderCapacityService:
    return ProviderCapacityService(
        storage,
        reporters=reporters or {},
        support_resolvers={},
        clock=clock,
        alert_sink=sink,
    )


def _window(
    provider: str,
    *,
    limit_id: str,
    used: float,
    resets_at: datetime,
    at: datetime,
) -> QuotaComponent:
    window = UsageWindow(
        label=limit_id,
        used=used,
        limit=100.0,
        unit="percent",
        resets_at=resets_at.isoformat(),
    )
    return QuotaComponent(
        key=component_key(provider, "provider", limit_id, "window:percent"),
        provider=provider,
        limit_id=limit_id,
        scope="provider",
        scope_known=True,
        state="exhausted" if used >= 100.0 else "available",
        source_observed_at=at,
        occurrence=f"{limit_id}@{at.isoformat()}:{used}",
        resets_at=resets_at,
        window=window,
    )


def _hard(
    *,
    scope: str,
    at: datetime,
    occurrence: str,
    resets_at: datetime | None = None,
) -> QuotaComponent:
    return QuotaComponent(
        key=component_key("codex", scope, "codex", "hard:rate_limit_reached"),
        provider="codex",
        limit_id="codex",
        scope=scope,
        scope_known=True,
        state="exhausted",
        source_observed_at=at,
        occurrence=occurrence,
        resets_at=resets_at,
        metadata={"rate_limit_reached_type": "rate_limit_reached"},
    )


def _recovery(*, scope: str, at: datetime, occurrence: str) -> QuotaComponent:
    return QuotaComponent(
        key=component_key("codex", scope, "codex", "hard:rate_limit_reached"),
        provider="codex",
        limit_id="codex",
        scope=scope,
        scope_known=True,
        state="available",
        source_observed_at=at,
        occurrence=occurrence,
        verified_recovery=True,
    )


def _warning(*, scope: str, at: datetime, occurrence: str) -> QuotaComponent:
    return QuotaComponent(
        key=component_key("codex", scope, "codex", "warning"),
        provider="codex",
        limit_id="codex",
        scope=scope,
        scope_known=True,
        state="available",
        source_observed_at=at,
        occurrence=occurrence,
        metadata={"warning": True},
    )


async def test_windows_alert_independently() -> None:
    clock = _Clock(_T0)
    storage = _Storage(clock)
    sink = _Sink()
    service = _service(clock, storage, sink)
    weekly_reset = _T0 + timedelta(days=7)
    five_reset = _T0 + timedelta(hours=5)

    async def observe(*components: QuotaComponent) -> None:
        await service.observe(QuotaObservation(provider="agy", components=components))
        await _settle()

    clock.now = _T0
    await observe(_window("agy", limit_id="weekly", used=95, resets_at=weekly_reset, at=clock.now))
    assert sink.take() == [("warn", "weekly", 95, None)]

    clock.now = _T0 + timedelta(minutes=1)
    await observe(_window("agy", limit_id="weekly", used=100, resets_at=weekly_reset, at=clock.now))
    assert sink.take() == [("exhausted", "weekly", 100, None)]

    clock.now = _T0 + timedelta(minutes=2)
    await observe(_window("agy", limit_id="weekly", used=100, resets_at=weekly_reset, at=clock.now))
    assert sink.take() == []

    clock.now = _T0 + timedelta(minutes=3)
    await observe(
        _window("agy", limit_id="weekly", used=100, resets_at=weekly_reset, at=clock.now),
        _window("agy", limit_id="five_hour", used=92, resets_at=five_reset, at=clock.now),
    )
    assert sink.take() == [("warn", "five_hour", 92, None)]

    clock.now = _T0 + timedelta(minutes=4)
    await observe(
        _window("agy", limit_id="five_hour", used=100, resets_at=five_reset, at=clock.now)
    )
    assert sink.take() == [("exhausted", "five_hour", 100, None)]

    # A later weekly reset is the only reset edge; the exhausted five_hour window stays silent.
    clock.now = _T0 + timedelta(minutes=5)
    later_weekly_reset = weekly_reset + timedelta(days=7)
    await observe(
        _window("agy", limit_id="weekly", used=0, resets_at=later_weekly_reset, at=clock.now)
    )
    assert sink.take() == [("reset", "weekly", None, None)]

    clock.now = _T0 + timedelta(minutes=6)
    await observe(
        _window("agy", limit_id="five_hour", used=100, resets_at=five_reset, at=clock.now)
    )
    assert sink.take() == []


async def test_provider_signal_edges() -> None:
    clock = _Clock(_T0)
    storage = _Storage(clock)
    sink = _Sink()
    scope = "account:acct-1"

    async def observe(service: ProviderCapacityService, *components: QuotaComponent) -> None:
        await service.observe(QuotaObservation(provider="codex", components=components))
        await _settle()

    service = _service(clock, storage, sink)

    clock.now = _T0
    hard_a = _hard(scope=scope, at=clock.now, occurrence="seat-a:1")
    await observe(service, hard_a)
    assert sink.take() == [("exhausted", None, None, None), ("limit_reached", None, None, None)]

    # A second seat reporting the same account limit is the same edge.
    clock.now = _T0 + timedelta(minutes=1)
    await observe(service, _hard(scope=scope, at=clock.now, occurrence="seat-b:1"))
    assert sink.take() == []

    # A restarted service replaying the first reading sends nothing.
    clock.now = _T0 + timedelta(minutes=2)
    restarted = _service(clock, storage, sink)
    await observe(restarted, hard_a)
    assert sink.take() == []

    # An explicit provider warning is metadata: warn, with no fabricated percent.
    clock.now = _T0 + timedelta(minutes=3)
    await observe(restarted, _warning(scope=scope, at=clock.now, occurrence="seat-a:w1"))
    assert sink.take() == [("warn", None, None, None)]

    # Newer positive evidence for the exact same scope and limit resets the episode once.
    clock.now = _T0 + timedelta(minutes=4)
    await observe(restarted, _recovery(scope=scope, at=clock.now, occurrence="seat-a:2"))
    assert sink.take() == [("reset", None, None, None)]

    clock.now = _T0 + timedelta(minutes=5)
    await observe(restarted, _recovery(scope=scope, at=clock.now, occurrence="seat-a:3"))
    assert sink.take() == []

    # A new episode, then positive evidence from an unknown source never recovers it.
    clock.now = _T0 + timedelta(minutes=6)
    await observe(restarted, _hard(scope=scope, at=clock.now, occurrence="seat-a:4"))
    assert sink.take() == [("exhausted", None, None, None), ("limit_reached", None, None, None)]

    clock.now = _T0 + timedelta(minutes=7)
    await observe(
        restarted,
        _recovery(scope="source:unknown-1", at=clock.now, occurrence="unknown:1"),
    )
    assert sink.take() == []


async def test_no_inferred_recovery() -> None:
    clock = _Clock(_T0)
    storage = _Storage(clock)
    sink = _Sink()
    service = _service(clock, storage, sink)
    scope = "account:acct-1"
    predicted_reset = _T0 + timedelta(hours=1)

    clock.now = _T0
    await service.observe(
        QuotaObservation(
            provider="codex",
            components=(
                _hard(scope=scope, at=clock.now, occurrence="seat-a:1", resets_at=predicted_reset),
            ),
        )
    )
    await _settle()
    assert sink.take() == [("exhausted", None, None, None), ("limit_reached", None, None, None)]

    # Expiry: the predicted reset passes, and a read alone sends no reset.
    clock.now = _T0 + timedelta(hours=2)
    await service.get("codex")
    await _settle()
    assert [key for key in sink.take() if key[0] == "reset"] == []

    # A pane hint is a possible limit only; it never clears a verified incident.
    await service.observe_pane_hint(
        PaneQuotaHint(provider="codex", occurrence="pane-1", kind="limit", detected_at=clock.now)
    )
    await _settle()
    assert [key for key in sink.take() if key[0] == "reset"] == []

    # A transient reporter failure keeps the prior row as stale and never recovers it.
    failing = _service(clock, storage, sink, reporters={"codex": _FailingReporter()})
    await failing.get("codex")
    await _settle()
    assert [key for key in sink.take() if key[0] == "reset"] == []

    # An unrelated scope's positive reading never recovers this episode.
    clock.now = _T0 + timedelta(hours=2, minutes=1)
    await service.observe(
        QuotaObservation(
            provider="codex",
            components=(_recovery(scope="account:acct-2", at=clock.now, occurrence="acct-2:1"),),
        )
    )
    await _settle()
    assert [key for key in sink.take() if key[0] == "reset"] == []


@pytest.mark.parametrize(
    "scenario", ["delivers", "no_manager", "empty_channel", "send_raises", "send_hangs"]
)
async def test_sink_degrades_without_raising(scenario: str) -> None:
    edge = AlertEdge(provider="codex", kind="exhausted", window="weekly", percent=100)
    if scenario == "send_raises":
        channel = _Channel(error=RuntimeError("channel down"))
    else:
        channel = _Channel(hang=scenario == "send_hangs")
    manager = None if scenario == "no_manager" else channel
    channel_name = "" if scenario == "empty_channel" else "ops"
    sink = build_operator_alert_sink(
        manager=lambda: manager,
        config=lambda: CommunicationsConfig(enabled=True, operator_alert_channel=channel_name),
        timeout_seconds=0.05,
    )

    # The sink never raises; a failed, hung, or unconfigured channel only logs.
    await sink(edge)

    if scenario == "delivers":
        assert channel.sent == [("ops", render_alert(edge))]
    else:
        assert channel.sent == []


async def test_sink_reads_late_getters_at_send_time() -> None:
    edge = AlertEdge(provider="codex", kind="exhausted", window="weekly", percent=100)
    channel = _Channel()
    manager: list[_Channel] = []
    sink = build_operator_alert_sink(
        manager=lambda: manager[0] if manager else None,
        config=lambda: CommunicationsConfig(enabled=True, operator_alert_channel="ops"),
    )

    await sink(edge)
    assert channel.sent == []

    manager.append(channel)
    await sink(edge)
    assert channel.sent == [("ops", render_alert(edge))]


def test_alert_text_is_minimal() -> None:
    weekly = AlertEdge(
        provider="codex",
        kind="exhausted",
        window="weekly",
        percent=100,
        resets_at="2026-11-10T00:00:00+00:00",
    )
    assert (
        render_alert(weekly) == "codex: weekly exhausted at 100%, resets 2026-11-10T00:00:00+00:00"
    )
    assert render_alert(AlertEdge(provider="agy", kind="exhausted")) == "agy: quota exhausted"
    assert (
        render_alert(AlertEdge(provider="codex", kind="drawing_credits", credit_balance="7.50"))
        == "codex: credits drawn, balance 7.50"
    )
    assert render_alert(AlertEdge(provider="codex", kind="possible_limit")) == (
        "codex: possible quota limit"
    )


def test_operator_alert_config_carriers() -> None:
    assert CommunicationsConfig().operator_alert_channel == ""
    assert CommunicationsConfig(operator_alert_channel="ops").operator_alert_channel == "ops"
    root = Path(__file__).resolve().parents[2]
    contract = (root / "crates/gcore/assets/config/runtime_config_contract.json").read_text(
        encoding="utf-8"
    )
    schema = (root / "tests/contracts/http/config_schema.json").read_text(encoding="utf-8")
    assert '"key": "communications.operator_alert_channel"' in contract
    assert '"communications.operator_alert_channel"' in schema


def _codex_observation(
    clock: _Clock,
    *,
    occurrence: str,
    used: float = 100.0,
    has_credits: bool | None = None,
    balance: str | None = None,
    reached: str | None = None,
    spend_control: bool | None = None,
    individual: bool | None = None,
) -> QuotaObservation | None:
    rate_limits: dict[str, object] = {
        "limit_id": "codex",
        "primary": {
            "used_percent": used,
            "window_minutes": 300,
            "resets_at": int(_RESET.timestamp()),
        },
    }
    if reached is not None:
        rate_limits["rate_limit_reached_type"] = reached
    if spend_control is not None:
        rate_limits["spend_control_reached"] = spend_control
    if individual is not None:
        rate_limits["individual_limit"] = individual
    credits: dict[str, object] = {}
    if has_credits is not None:
        credits["has_credits"] = has_credits
    if balance is not None:
        credits["balance"] = balance
    if credits:
        rate_limits["credits"] = credits
    return observation_from_rate_limits(
        rate_limits,
        source_observed_at=clock.now,
        occurrence=occurrence,
        source_key="seat-a",
    )


async def test_codex_flag_and_credit_predicates() -> None:
    clock = _Clock(_T0)
    storage = _Storage(clock)
    sink = _Sink()
    service = _service(clock, storage, sink)

    async def observe(
        service: ProviderCapacityService, observation: QuotaObservation | None
    ) -> None:
        assert observation is not None
        await service.observe(observation)
        await _settle()

    # Credits: the first balance is a baseline; a lower balance under an exhausted window draws.
    clock.now = _T0
    await observe(
        service, _codex_observation(clock, occurrence="c:1", has_credits=True, balance="10.00")
    )
    assert sink.take() == [("exhausted", "five_hour", 100, None)]

    clock.now = _T0 + timedelta(minutes=1)
    await observe(
        service, _codex_observation(clock, occurrence="c:2", has_credits=True, balance="7.50")
    )
    assert sink.take() == [("drawing_credits", None, None, "7.50")]

    clock.now = _T0 + timedelta(minutes=2)
    await observe(
        service, _codex_observation(clock, occurrence="c:3", has_credits=True, balance="7.50")
    )
    assert sink.take() == []

    # Explicit has_credits=false clears silently and rearms the next falling balance.
    clock.now = _T0 + timedelta(minutes=3)
    await observe(
        service, _codex_observation(clock, occurrence="c:4", has_credits=False, balance="5.00")
    )
    assert sink.take() == []

    clock.now = _T0 + timedelta(minutes=4)
    await observe(
        service, _codex_observation(clock, occurrence="c:5", has_credits=True, balance="4.00")
    )
    assert sink.take() == [("drawing_credits", None, None, "4.00")]

    # A verified rate-limit reached type is a hard episode with an explicit reached edge.
    reached_sink = _Sink()
    reached_service = _service(clock, _Storage(clock), reached_sink)
    clock.now = _T0 + timedelta(minutes=5)
    await observe(
        reached_service,
        _codex_observation(clock, occurrence="r:1", used=40.0, reached="rate_limit_reached"),
    )
    assert reached_sink.take() == [
        ("exhausted", None, None, None),
        ("limit_reached", None, None, None),
    ]

    clock.now = _T0 + timedelta(minutes=6)
    await observe(
        reached_service,
        _codex_observation(clock, occurrence="r:2", used=40.0, reached="rate_limit_reached"),
    )
    assert reached_sink.take() == []

    # Spend control is an explicit reached signal in its own right.
    spend_sink = _Sink()
    spend_service = _service(clock, _Storage(clock), spend_sink)
    clock.now = _T0 + timedelta(minutes=7)
    await observe(
        spend_service, _codex_observation(clock, occurrence="s:1", used=40.0, spend_control=True)
    )
    assert [key[0] for key in spend_sink.take()].count("limit_reached") == 1

    # individual_limit is metadata, never a reached flag.
    individual_sink = _Sink()
    individual_service = _service(clock, _Storage(clock), individual_sink)
    clock.now = _T0 + timedelta(minutes=8)
    await observe(
        individual_service, _codex_observation(clock, occurrence="i:1", used=40.0, individual=True)
    )
    assert [key for key in individual_sink.take() if key[0] == "limit_reached"] == []


async def test_possible_limit_hint_edges() -> None:
    clock = _Clock(_T0)
    storage = _Storage(clock)
    sink = _Sink()
    service = _service(clock, storage, sink)

    def hint(occurrence: str, at: datetime) -> PaneQuotaHint:
        return PaneQuotaHint(provider="codex", occurrence=occurrence, kind="limit", detected_at=at)

    clock.now = _T0
    assert await service.observe_pane_hint(hint("seat-a:pane-1", clock.now)) is True
    await _settle()
    assert sink.take() == [("possible_limit", None, None, None)]

    # Inside the 900s cooldown: a new occurrence from the same seat, and one from another seat.
    clock.now = _T0 + timedelta(seconds=60)
    await service.observe_pane_hint(hint("seat-a:pane-2", clock.now))
    await _settle()
    assert sink.take() == []

    clock.now = _T0 + timedelta(seconds=100)
    await service.observe_pane_hint(hint("seat-b:pane-1", clock.now))
    await _settle()
    assert sink.take() == []

    # A replayed occurrence never notifies, even after the cooldown.
    clock.now = _T0 + timedelta(seconds=1000)
    assert await service.observe_pane_hint(hint("seat-a:pane-1", _T0)) is False
    await _settle()
    assert sink.take() == []

    # A genuinely new occurrence after the bound notifies again.
    clock.now = _T0 + _NOTIFY_COOLDOWN + timedelta(seconds=1)
    await service.observe_pane_hint(hint("seat-a:pane-3", clock.now))
    await _settle()
    assert sink.take() == [("possible_limit", None, None, None)]
    assert render_alert(AlertEdge(provider="codex", kind="possible_limit")) == (
        "codex: possible quota limit"
    )

    # Restart baselines panes again but keeps the persisted dedupe and cooldown.
    clock.now = _T0 + _NOTIFY_COOLDOWN + timedelta(seconds=2)
    restarted = _service(clock, storage, sink)
    assert await restarted.observe_pane_hint(hint("seat-a:pane-3", clock.now)) is False
    await _settle()
    assert sink.take() == []

    clock.now = _T0 + _NOTIFY_COOLDOWN + timedelta(seconds=3)
    await restarted.observe_pane_hint(hint("seat-a:pane-4", clock.now))
    await _settle()
    assert sink.take() == []
