from __future__ import annotations

from unittest.mock import call, patch

import pytest

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
