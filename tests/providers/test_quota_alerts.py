"""Operator quota alerts: edges, persisted dedupe, recovery rules, sink degradation, and carriers.

Edges come from real service admission (observe / observe_pane_hint) with an in-memory
storage fake that keeps the row CAS contract. The sink is a recording fake; delivery
failures are exercised through the real sink builder.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gobby.config.communications import CommunicationsConfig
from gobby.providers.capacity_service import ProviderCapacityService, QuotaWriteContentionError
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
_WEEK_RESET = _T0 + timedelta(days=7)

type _Key = tuple[AlertKind, str | None, int | None, str | None]


class _Absent:
    """Marks a reached field the payload omits; None stays an explicit JSON null."""


_ABSENT = _Absent()


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
        metadata={"reached": {"rate_limit_reached_type": "rate_limit_reached"}},
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
        PaneQuotaHint(provider="codex", occurrence="pane-1", kind="limit", detected_at=clock.now),
        generation="seat-a",
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
    primary_reset: datetime = _RESET,
    include_primary: bool = True,
    weekly: float | None = None,
    weekly_reset: datetime = _WEEK_RESET,
    has_credits: bool | None = None,
    balance: str | None = None,
    reached: object = _ABSENT,
    spend_control: object = _ABSENT,
    individual: bool | None = None,
) -> QuotaObservation | None:
    rate_limits: dict[str, object] = {"limit_id": "codex"}
    if include_primary:
        rate_limits["primary"] = {
            "used_percent": used,
            "window_minutes": 300,
            "resets_at": int(primary_reset.timestamp()),
        }
    if weekly is not None:
        rate_limits["secondary"] = {
            "used_percent": weekly,
            "window_minutes": 10080,
            "resets_at": int(weekly_reset.timestamp()),
        }
    if reached is not _ABSENT:
        rate_limits["rate_limit_reached_type"] = reached
    if spend_control is not _ABSENT:
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


def _pane_hint(occurrence: str, at: datetime) -> PaneQuotaHint:
    return PaneQuotaHint(provider="codex", occurrence=occurrence, kind="limit", detected_at=at)


def _details_section(storage: _Storage, name: str) -> Mapping[str, object]:
    record = storage.get("codex")
    assert record is not None
    section = record.details.get(name)
    assert isinstance(section, Mapping)
    return section


def _pane_hint_generations(storage: _Storage) -> Mapping[str, object]:
    generations = _details_section(storage, "pane_hint_alerts").get("generations")
    assert isinstance(generations, Mapping)
    return generations


async def _observe(service: ProviderCapacityService, observation: QuotaObservation | None) -> None:
    assert observation is not None
    await service.observe(observation)
    await _settle()


class _RejectingStorage(_Storage):
    """Every compare-and-set loses, so no row write commits."""

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
        return False


async def test_possible_limit_hint_edges() -> None:
    clock = _Clock(_T0)
    storage = _Storage(clock)
    sink = _Sink()
    service = _service(clock, storage, sink)

    clock.now = _T0
    assert (
        await service.observe_pane_hint(_pane_hint("seat-a:pane-1", clock.now), generation="seat-a")
        is True
    )
    await _settle()
    assert sink.take() == [("possible_limit", None, None, None)]

    # Inside the 900s cooldown: a new occurrence from the same seat, and one from another seat.
    clock.now = _T0 + timedelta(seconds=60)
    assert (
        await service.observe_pane_hint(_pane_hint("seat-a:pane-2", clock.now), generation="seat-a")
        is True
    )
    clock.now = _T0 + timedelta(seconds=100)
    assert (
        await service.observe_pane_hint(_pane_hint("seat-b:pane-1", clock.now), generation="seat-b")
        is True
    )
    await _settle()
    assert sink.take() == []

    # A replay of an occurrence this generation has seen never notifies, even after the cooldown.
    clock.now = _T0 + timedelta(seconds=1000)
    replay = _pane_hint("seat-a:pane-2", _T0 + timedelta(seconds=60))
    assert await service.observe_pane_hint(replay, generation="seat-a") is False
    await _settle()
    assert sink.take() == []

    # A genuinely new occurrence after the bound notifies again.
    clock.now = _T0 + timedelta(seconds=1001)
    assert (
        await service.observe_pane_hint(_pane_hint("seat-a:pane-3", clock.now), generation="seat-a")
        is True
    )
    await _settle()
    assert sink.take() == [("possible_limit", None, None, None)]
    assert render_alert(AlertEdge(provider="codex", kind="possible_limit")) == (
        "codex: possible quota limit"
    )

    # Restart baselines panes again but keeps the persisted dedupe and cooldown.
    clock.now = _T0 + timedelta(seconds=1002)
    restarted = _service(clock, storage, sink)
    assert (
        await restarted.observe_pane_hint(
            _pane_hint("seat-a:pane-3", clock.now), generation="seat-a"
        )
        is False
    )
    await _settle()
    assert sink.take() == []

    clock.now = _T0 + timedelta(seconds=1003)
    assert (
        await restarted.observe_pane_hint(
            _pane_hint("seat-a:pane-4", clock.now), generation="seat-a"
        )
        is True
    )
    await _settle()
    assert sink.take() == []


async def test_pane_hint_dedupe_is_per_live_generation() -> None:
    clock = _Clock(_T0)
    storage = _Storage(clock)
    sink = _Sink()
    service = _service(clock, storage, sink)

    # 70 live generations, one occurrence each, all inside one cooldown window.
    for index in range(70):
        clock.now = _T0 + timedelta(seconds=index)
        generation = f"gen-{index}"
        occurrence = _pane_hint(f"{generation}:occ-1", clock.now)
        assert await service.observe_pane_hint(occurrence, generation=generation) is True
    await _settle()
    assert sink.take() == [("possible_limit", None, None, None)]
    assert len(_pane_hint_generations(storage)) == 70

    # Every live generation keeps the digests of its occurrences, so replays stay silent.
    clock.now = _T0 + timedelta(seconds=1000)
    first = _pane_hint("gen-0:occ-1", _T0)
    assert await service.observe_pane_hint(first, generation="gen-0") is False
    last = _pane_hint("gen-69:occ-1", clock.now)
    assert await service.observe_pane_hint(last, generation="gen-69") is False
    await _settle()
    assert sink.take() == []

    # A genuinely new occurrence after the bound notifies again.
    clock.now = _T0 + timedelta(seconds=1001)
    assert (
        await service.observe_pane_hint(_pane_hint("gen-5:occ-2", clock.now), generation="gen-5")
        is True
    )
    await _settle()
    assert sink.take() == [("possible_limit", None, None, None)]


async def test_no_occurrence_is_evicted_within_a_live_generation() -> None:
    clock = _Clock(_T0)
    sink = _Sink()
    service = _service(clock, _Storage(clock), sink)

    # 70 distinct occurrences in one live generation, all inside one cooldown window.
    for index in range(70):
        clock.now = _T0 + timedelta(seconds=index)
        occurrence = _pane_hint(f"gen-a:occ-{index}", clock.now)
        assert await service.observe_pane_hint(occurrence, generation="gen-a") is True
    await _settle()
    assert sink.take() == [("possible_limit", None, None, None)]

    # The earliest occurrence is still in the digest set, so its replay after the bound stays silent.
    clock.now = _T0 + timedelta(seconds=1000)
    earliest = _pane_hint("gen-a:occ-0", _T0)
    assert await service.observe_pane_hint(earliest, generation="gen-a") is False
    await _settle()
    assert sink.take() == []


async def test_retired_generations_leave_the_bounded_state() -> None:
    clock = _Clock(_T0)
    storage = _Storage(clock)
    service = _service(clock, storage, _Sink())

    for generation in ("gen-a", "gen-b", "gen-c"):
        occurrence = _pane_hint(f"{generation}:occ-1", clock.now)
        assert await service.observe_pane_hint(occurrence, generation=generation) is True
    assert set(_pane_hint_generations(storage)) == {"gen-a", "gen-b", "gen-c"}

    await service.retire_pane_generations("codex", live=("gen-b",))
    assert set(_pane_hint_generations(storage)) == {"gen-b"}


async def test_replayed_occurrence_never_notifies() -> None:
    """Every occurrence stays in its generation's digest set, so a replay never notifies."""
    clock = _Clock(_T0)
    storage = _Storage(clock)
    sink = _Sink()
    service = _service(clock, storage, sink)

    clock.now = _T0
    assert (
        await service.observe_pane_hint(_pane_hint("gen-a:occ-1", clock.now), generation="gen-a")
        is True
    )
    clock.now = _T0 + timedelta(seconds=1)
    assert (
        await service.observe_pane_hint(_pane_hint("gen-a:occ-2", clock.now), generation="gen-a")
        is True
    )
    await _settle()
    assert sink.take() == [("possible_limit", None, None, None)]

    clock.now = _T0 + timedelta(seconds=1000)
    older = _pane_hint("gen-a:occ-1", _T0)
    assert await service.observe_pane_hint(older, generation="gen-a") is False
    await _settle()
    assert sink.take() == []


async def test_hint_and_incident_sections_survive_each_other() -> None:
    clock = _Clock(_T0)
    storage = _Storage(clock)
    sink = _Sink()
    service = _service(clock, storage, sink)
    scope = "account:acct-1"

    clock.now = _T0
    await _observe(
        service,
        QuotaObservation(
            provider="codex", components=(_hard(scope=scope, at=clock.now, occurrence="m:1"),)
        ),
    )
    assert sink.take() == [("exhausted", None, None, None), ("limit_reached", None, None, None)]

    clock.now = _T0 + timedelta(seconds=1)
    assert (
        await service.observe_pane_hint(_pane_hint("m-gen:occ-1", clock.now), generation="m-gen")
        is True
    )
    await _settle()
    assert sink.take() == [("possible_limit", None, None, None)]

    # The hint stamp lives in its own section; the incident section stays free of it.
    assert "hint_notified_at" not in _details_section(storage, "quota_alerts")
    assert "notified_at" in _details_section(storage, "pane_hint_alerts")

    # A component write carries the hint section forward, so the hint replay stays deduped.
    clock.now = _T0 + timedelta(seconds=2)
    await _observe(
        service,
        QuotaObservation(
            provider="codex", components=(_warning(scope=scope, at=clock.now, occurrence="m:2"),)
        ),
    )
    assert sink.take() == [("warn", None, None, None)]
    assert "m-gen" in _pane_hint_generations(storage)

    # A hint write carries the incident forward: the verified recovery still resets it.
    clock.now = _T0 + timedelta(seconds=3)
    await _observe(
        service,
        QuotaObservation(
            provider="codex", components=(_recovery(scope=scope, at=clock.now, occurrence="m:3"),)
        ),
    )
    assert sink.take() == [("reset", None, None, None)]


@pytest.mark.parametrize(
    ("arm_reached", "arm_spend"),
    [("rate_limit_reached", _ABSENT), (_ABSENT, True)],
    ids=["reached_type", "spend_control"],
)
async def test_reached_clear_rearms_limit_reached(arm_reached: object, arm_spend: object) -> None:
    clock = _Clock(_T0)
    sink = _Sink()
    service = _service(clock, _Storage(clock), sink)

    clock.now = _T0
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="r:1", used=40.0, reached=arm_reached, spend_control=arm_spend
        ),
    )
    assert sink.take() == [("exhausted", None, None, None), ("limit_reached", None, None, None)]

    # Explicit null and false clear the flag; the hard incident stays open, so no reset edge.
    clock.now = _T0 + timedelta(minutes=1)
    await _observe(
        service,
        _codex_observation(clock, occurrence="r:2", used=40.0, reached=None, spend_control=False),
    )
    assert sink.take() == []

    # The same verified signal after the clear rearms limit_reached once; exhausted never repeats.
    clock.now = _T0 + timedelta(minutes=2)
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="r:3", used=40.0, reached=arm_reached, spend_control=arm_spend
        ),
    )
    assert sink.take() == [("limit_reached", None, None, None)]


async def test_clear_without_incident_is_silent() -> None:
    clock = _Clock(_T0)
    sink = _Sink()
    service = _service(clock, _Storage(clock), sink)

    clock.now = _T0
    await _observe(
        service,
        _codex_observation(clock, occurrence="c:1", used=40.0, reached=None, spend_control=False),
    )
    assert sink.take() == []


async def test_partial_reached_fields_preserve_the_incident() -> None:
    clock = _Clock(_T0)
    sink = _Sink()
    service = _service(clock, _Storage(clock), sink)

    clock.now = _T0
    await _observe(
        service,
        _codex_observation(clock, occurrence="p:1", used=40.0, reached="rate_limit_reached"),
    )
    assert sink.take() == [("exhausted", None, None, None), ("limit_reached", None, None, None)]

    # One reached key alone is not a clear, and a flag still set is not a rearm.
    clock.now = _T0 + timedelta(minutes=1)
    await _observe(service, _codex_observation(clock, occurrence="p:2", used=40.0, reached=None))
    assert sink.take() == []

    clock.now = _T0 + timedelta(minutes=2)
    await _observe(
        service, _codex_observation(clock, occurrence="p:3", used=40.0, spend_control=False)
    )
    assert sink.take() == []

    clock.now = _T0 + timedelta(minutes=3)
    await _observe(
        service,
        _codex_observation(clock, occurrence="p:4", used=40.0, reached="rate_limit_reached"),
    )
    assert sink.take() == []


async def test_weekly_credit_incident_and_reset() -> None:
    clock = _Clock(_T0)
    sink = _Sink()
    service = _service(clock, _Storage(clock), sink)
    next_week = _WEEK_RESET + timedelta(days=7)

    clock.now = _T0
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="w:1", used=40.0, weekly=95.0, has_credits=True, balance="10.00"
        ),
    )
    assert sink.take() == [("warn", "weekly", 95, None)]

    clock.now = _T0 + timedelta(minutes=1)
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="w:2", used=40.0, weekly=100.0, has_credits=True, balance="7.50"
        ),
    )
    assert sink.take() == [
        ("exhausted", "weekly", 100, None),
        ("drawing_credits", None, None, "7.50"),
    ]

    # A repeated exhausted reading with the same balance stays silent.
    clock.now = _T0 + timedelta(minutes=2)
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="w:3", used=40.0, weekly=100.0, has_credits=True, balance="7.50"
        ),
    )
    assert sink.take() == []

    # The weekly reset is the only edge; it clears the drawing state silently.
    clock.now = _T0 + timedelta(minutes=3)
    await _observe(
        service,
        _codex_observation(
            clock,
            occurrence="w:4",
            used=40.0,
            weekly=0.0,
            weekly_reset=next_week,
            has_credits=True,
            balance="7.50",
        ),
    )
    assert sink.take() == [("reset", "weekly", None, None)]

    # A fresh weekly exhaustion with a falling balance draws again.
    clock.now = _T0 + timedelta(minutes=4)
    await _observe(
        service,
        _codex_observation(
            clock,
            occurrence="w:5",
            used=40.0,
            weekly=100.0,
            weekly_reset=next_week + timedelta(days=7),
            has_credits=True,
            balance="6.00",
        ),
    )
    assert sink.take() == [
        ("exhausted", "weekly", 100, None),
        ("drawing_credits", None, None, "6.00"),
    ]


async def test_equal_balance_clears_drawing_and_rearms() -> None:
    clock = _Clock(_T0)
    sink = _Sink()
    service = _service(clock, _Storage(clock), sink)

    clock.now = _T0
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="e:1", used=40.0, weekly=100.0, has_credits=True, balance="10.00"
        ),
    )
    assert sink.take() == [("exhausted", "weekly", 100, None)]

    clock.now = _T0 + timedelta(minutes=1)
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="e:2", used=40.0, weekly=100.0, has_credits=True, balance="9.00"
        ),
    )
    assert sink.take() == [("drawing_credits", None, None, "9.00")]

    # An equal valid balance clears the drawing state silently.
    clock.now = _T0 + timedelta(minutes=2)
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="e:3", used=40.0, weekly=100.0, has_credits=True, balance="9.00"
        ),
    )
    assert sink.take() == []

    # The next lower balance in the same exhausted window draws again.
    clock.now = _T0 + timedelta(minutes=3)
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="e:4", used=40.0, weekly=100.0, has_credits=True, balance="8.00"
        ),
    )
    assert sink.take() == [("drawing_credits", None, None, "8.00")]


async def test_missing_has_credits_retains_drawing_state() -> None:
    clock = _Clock(_T0)
    sink = _Sink()
    service = _service(clock, _Storage(clock), sink)

    clock.now = _T0
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="m:1", used=40.0, weekly=100.0, has_credits=True, balance="10.00"
        ),
    )
    assert sink.take() == [("exhausted", "weekly", 100, None)]

    clock.now = _T0 + timedelta(minutes=1)
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="m:2", used=40.0, weekly=100.0, has_credits=True, balance="9.00"
        ),
    )
    assert sink.take() == [("drawing_credits", None, None, "9.00")]

    # A reading without has_credits records the balance but keeps the drawing state.
    clock.now = _T0 + timedelta(minutes=2)
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="m:3", used=40.0, weekly=100.0, has_credits=None, balance="8.00"
        ),
    )
    assert sink.take() == []

    # The retained drawing state means a lower explicit balance does not draw a second time.
    clock.now = _T0 + timedelta(minutes=3)
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="m:4", used=40.0, weekly=100.0, has_credits=True, balance="7.50"
        ),
    )
    assert sink.take() == []

    # Only an explicit equal balance clears it, and the next lower balance draws again.
    clock.now = _T0 + timedelta(minutes=4)
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="m:5", used=40.0, weekly=100.0, has_credits=True, balance="7.50"
        ),
    )
    assert sink.take() == []

    clock.now = _T0 + timedelta(minutes=5)
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="m:6", used=40.0, weekly=100.0, has_credits=True, balance="7.00"
        ),
    )
    assert sink.take() == [("drawing_credits", None, None, "7.00")]


async def test_windowless_reading_never_clears_drawing() -> None:
    clock = _Clock(_T0)
    sink = _Sink()
    service = _service(clock, _Storage(clock), sink)

    clock.now = _T0
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="wl:1", used=40.0, weekly=100.0, has_credits=True, balance="10.00"
        ),
    )
    assert sink.take() == [("exhausted", "weekly", 100, None)]

    clock.now = _T0 + timedelta(minutes=1)
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="wl:2", used=40.0, weekly=100.0, has_credits=True, balance="9.00"
        ),
    )
    assert sink.take() == [("drawing_credits", None, None, "9.00")]

    # The weekly reading is now stale and this windowless reading shows no window, so the
    # drawing state stays as it was.
    clock.now = _T0 + timedelta(minutes=1, seconds=901)
    await _observe(
        service,
        _codex_observation(
            clock,
            occurrence="wl:3",
            include_primary=False,
            reached="rate_limit_reached",
            has_credits=True,
            balance="8.00",
        ),
    )
    assert sink.take() == [("exhausted", None, None, None), ("limit_reached", None, None, None)]

    # The retained drawing state suppresses a lower exhausted balance: no second alert.
    clock.now = _T0 + timedelta(minutes=1, seconds=902)
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="wl:4", used=40.0, weekly=100.0, has_credits=True, balance="7.75"
        ),
    )
    assert sink.take() == []

    # A later weekly reset admits the windowed reading with no exhausted window, which clears
    # drawing silently; the reset itself is the only edge.
    next_week = _WEEK_RESET + timedelta(days=7)
    clock.now = _T0 + timedelta(minutes=1, seconds=903)
    await _observe(
        service,
        _codex_observation(
            clock,
            occurrence="wl:5",
            used=40.0,
            weekly=0.0,
            weekly_reset=next_week,
            has_credits=True,
            balance="7.50",
        ),
    )
    assert sink.take() == [("reset", "weekly", None, None)]

    clock.now = _T0 + timedelta(minutes=1, seconds=904)
    await _observe(
        service,
        _codex_observation(
            clock,
            occurrence="wl:6",
            used=40.0,
            weekly=100.0,
            weekly_reset=next_week,
            has_credits=True,
            balance="7.00",
        ),
    )
    assert sink.take() == [
        ("exhausted", "weekly", 100, None),
        ("drawing_credits", None, None, "7.00"),
    ]


async def test_windowless_reached_clear_rearms_limit_reached() -> None:
    clock = _Clock(_T0)
    sink = _Sink()
    service = _service(clock, _Storage(clock), sink)

    clock.now = _T0
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="wc:1", include_primary=False, reached="rate_limit_reached"
        ),
    )
    assert sink.take() == [("exhausted", None, None, None), ("limit_reached", None, None, None)]

    # An explicit windowless clear is an admitted reading: it clears limit_reached, and no reset.
    clock.now = _T0 + timedelta(minutes=1)
    await _observe(
        service,
        _codex_observation(
            clock,
            occurrence="wc:2",
            include_primary=False,
            reached=None,
            spend_control=False,
        ),
    )
    assert sink.take() == []

    clock.now = _T0 + timedelta(minutes=2)
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="wc:3", include_primary=False, reached="rate_limit_reached"
        ),
    )
    assert sink.take() == [("limit_reached", None, None, None)]


async def test_five_hour_later_reset_leaves_weekly_untouched() -> None:
    clock = _Clock(_T0)
    sink = _Sink()
    service = _service(clock, _Storage(clock), sink)
    later_five_reset = _RESET + timedelta(hours=5)

    clock.now = _T0
    await _observe(service, _codex_observation(clock, occurrence="f:1", used=100.0, weekly=20.0))
    assert sink.take() == [("exhausted", "five_hour", 100, None)]

    clock.now = _T0 + timedelta(minutes=1)
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="f:2", used=0.0, primary_reset=later_five_reset, weekly=20.0
        ),
    )
    assert sink.take() == [("reset", "five_hour", None, None)]

    clock.now = _T0 + timedelta(minutes=2)
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="f:3", used=0.0, primary_reset=later_five_reset, weekly=20.0
        ),
    )
    assert sink.take() == []


async def test_stale_exhausted_window_never_draws() -> None:
    clock = _Clock(_T0)
    sink = _Sink()
    service = _service(clock, _Storage(clock), sink)

    clock.now = _T0
    await _observe(
        service,
        _codex_observation(
            clock, occurrence="s:1", used=40.0, weekly=100.0, has_credits=True, balance="10.00"
        ),
    )
    assert sink.take() == [("exhausted", "weekly", 100, None)]

    # The weekly window is absent 901s later; its stored exhaustion is past the freshness bound.
    clock.now = _T0 + timedelta(seconds=901)
    await _observe(
        service,
        _codex_observation(clock, occurrence="s:2", used=40.0, has_credits=True, balance="7.50"),
    )
    assert sink.take() == []


async def test_disabled_communications_never_send(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="gobby.providers.quota_alerts")
    edge = AlertEdge(provider="codex", kind="exhausted", window="weekly", percent=100)
    channel = _Channel()
    sink = build_operator_alert_sink(
        manager=lambda: channel,
        config=lambda: CommunicationsConfig(enabled=False, operator_alert_channel="ops"),
    )

    await sink(edge)

    assert channel.sent == []
    assert render_alert(edge) in caplog.text


async def test_lost_write_sends_nothing() -> None:
    clock = _Clock(_T0)
    sink = _Sink()
    service = _service(clock, _RejectingStorage(clock), sink)

    with pytest.raises(QuotaWriteContentionError):
        await service.observe(
            QuotaObservation(
                provider="codex",
                components=(_hard(scope="account:acct-1", at=_T0, occurrence="lost:1"),),
            )
        )
    await _settle()
    assert sink.take() == []


async def test_alerts_deliver_after_the_write_returns() -> None:
    clock = _Clock(_T0)
    sink = _Sink()
    service = _service(clock, _Storage(clock), sink)

    await service.observe(
        QuotaObservation(
            provider="codex",
            components=(_hard(scope="account:acct-1", at=_T0, occurrence="o:1"),),
        )
    )
    # The row write has committed and the lock is released; delivery is scheduled, not inline.
    assert sink.take() == []
    await _settle()
    assert sink.take() == [("exhausted", None, None, None), ("limit_reached", None, None, None)]


@pytest.mark.parametrize(
    ("second_has_credits", "second_balance"),
    [(True, None), (None, "7.50")],
    ids=["missing_balance", "missing_has_credits"],
)
async def test_missing_credit_inputs_never_draw(
    second_has_credits: bool | None,
    second_balance: str | None,
) -> None:
    clock = _Clock(_T0)
    sink = _Sink()
    service = _service(clock, _Storage(clock), sink)

    # The first balance is a baseline under an exhausted window; it sends only the window edge.
    clock.now = _T0
    await _observe(
        service, _codex_observation(clock, occurrence="m:1", has_credits=True, balance="10.00")
    )
    assert sink.take() == [("exhausted", "five_hour", 100, None)]

    # A reading missing either credit input never draws, even under an exhausted window.
    clock.now = _T0 + timedelta(minutes=1)
    await _observe(
        service,
        _codex_observation(
            clock,
            occurrence="m:2",
            has_credits=second_has_credits,
            balance=second_balance,
        ),
    )
    assert sink.take() == []

    # A later complete falling reading draws once.
    clock.now = _T0 + timedelta(minutes=2)
    await _observe(
        service, _codex_observation(clock, occurrence="m:3", has_credits=True, balance="6.00")
    )
    assert sink.take() == [("drawing_credits", None, None, "6.00")]
