from __future__ import annotations

from unittest.mock import call, patch

import pytest

from gobby.hooks.phase_timing import (
    HOOK_PHASES,
    HookPhaseTimings,
    hook_phase_timing_scope,
    measure_hook_phase,
    observe_hook_phase_timings,
)

pytestmark = pytest.mark.unit


def test_phase_scope_accumulates_nested_hook_work() -> None:
    timings = HookPhaseTimings()

    with hook_phase_timing_scope(timings):
        with measure_hook_phase("handler_body"):
            pass
        with measure_hook_phase("handler_body"):
            pass

    assert timings.snapshot()["handler_body"] > 0


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
