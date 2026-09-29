from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, call, patch

import pytest

from gobby.hooks import phase_timing
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.hooks.phase_timing import (
    HOOK_PHASES,
    HookPhaseTimings,
    hook_phase_timing_scope,
    measure_hook_phase,
    observe_hook_phase_timings,
    timed_await,
    timed_to_thread,
)
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


def test_phase_scope_accumulates_nested_hook_work() -> None:
    timings = HookPhaseTimings()

    with hook_phase_timing_scope(timings):
        with measure_hook_phase("handler_body"):
            pass
        with measure_hook_phase("handler_body"):
            pass

    assert timings.snapshot()["handler_body"] > 0


@pytest.mark.asyncio
async def test_prelude_timing_separates_executor_queue_and_work() -> None:
    timings = HookPhaseTimings()

    async def immediate() -> int:
        return 7

    with hook_phase_timing_scope(timings):
        assert await timed_to_thread("prelude_probe", lambda: 42) == 42
        assert await timed_await("prelude_async", immediate()) == 7

    breakdown = timings.breakdown()
    assert breakdown["prelude_probe_queue"] >= 0
    assert breakdown["prelude_probe_work"] >= 0
    assert breakdown["prelude_probe"] >= breakdown["prelude_probe_queue"]
    assert breakdown["prelude_async"] >= 0


def test_hook_scope_reports_hub_query_percentiles(temp_db: HubDatabase) -> None:
    timings = HookPhaseTimings()

    with hook_phase_timing_scope(timings):
        assert temp_db.fetchone("SELECT 1 AS value") is not None
        assert temp_db.fetchone("SELECT 2 AS value") is not None

    summary = timings.query_latency_summary_ms()
    assert summary["count"] >= 2
    assert 0 < summary["p50"] <= summary["p95"]
    # Query timing excludes the wait for a pooled connection; it is its own key.
    assert timings.breakdown()["hub_pool_acquire"] > 0


def test_query_summary_exposes_blocked_query_hidden_past_p95() -> None:
    timings = HookPhaseTimings()
    for _ in range(33):
        timings.add_query_latency(0.002)
    timings.add_query_latency(4.4)

    summary = timings.query_latency_summary_ms()

    assert summary["count"] == 34
    assert summary["p95"] == pytest.approx(2.0)
    assert summary["max"] == pytest.approx(4400.0)


def test_observe_exports_all_phases_and_finds_dominant_phase() -> None:
    timings = HookPhaseTimings()
    timings.add("admission_wait", 6.0)
    timings.add("rule_evaluation", 1.0)

    with patch("gobby.hooks.phase_timing.observe_histogram") as observe:
        dominant, duration, phases = observe_hook_phase_timings(
            timings,
            total_seconds=8.0,
            hook_type="PreToolUse",
            source="codex",
        )

    assert (dominant, duration) == ("admission_wait", 6.0)
    assert phases["response"] == pytest.approx(1.0)
    assert observe.call_count == len(HOOK_PHASES)
    assert (
        call(
            "hook_phase_duration_seconds",
            6.0,
            attributes={
                "hook_type": "PreToolUse",
                "source": "codex",
                "phase": "admission_wait",
            },
        )
        in observe.call_args_list
    )


def _hook_event() -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id="phase-timing-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
        machine_id="21000000-0000-4000-8000-000000000003",
    )


class _Clock:
    """Stands in for the phase clock so a stub's duration is exact, not slept."""

    def __init__(self) -> None:
        self.now = 0.0

    def perf_counter(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(phase_timing, "time", fake)
    return fake


def test_adapter_thread_gate_and_async_run_are_timed(
    manager_with_mocks: HookManager, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """The health gate and the private event loop no longer hide in `response` (#23063)."""
    response = HookResponse(decision="allow")

    def slow_gate(*_args: object) -> None:
        clock.now += 2.0

    async def slow_handler() -> HookResponse:
        clock.now += 3.0
        return response

    monkeypatch.setattr("gobby.hooks.hook_manager.ensure_daemon_ready", slow_gate)
    monkeypatch.setattr(manager_with_mocks, "_handle_after_daemon_ready", lambda _e: slow_handler())
    timings = HookPhaseTimings()

    with hook_phase_timing_scope(timings):
        assert manager_with_mocks._handle_internal(_hook_event()) is response

    breakdown = timings.breakdown()
    assert breakdown["daemon_ready_gate"] == 2.0
    assert breakdown["async_handler_run"] == 3.0


def test_response_enrichment_is_timed(manager_with_mocks: HookManager, clock: _Clock) -> None:
    def slow_enrich(*_args: object, **_kwargs: object) -> None:
        clock.now += 1.5

    enricher = MagicMock()
    enricher.enrich.side_effect = slow_enrich
    manager_with_mocks._enricher = enricher
    timings = HookPhaseTimings()

    with hook_phase_timing_scope(timings):
        manager_with_mocks._complete_response(
            _hook_event(), HookResponse(decision="allow"), workflow_context=None
        )

    assert timings.breakdown()["response_enrich"] == 1.5
