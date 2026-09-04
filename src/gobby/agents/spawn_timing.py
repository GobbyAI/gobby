"""Shared phase timing for managed agent spawns."""

from __future__ import annotations

import time
from collections.abc import Mapping, MutableMapping

SPAWN_PHASES = (
    "prepare_terminal_spawn",
    "code_index_status",
    "code_index_index",
    "code_index_search_content",
    "prepare_sandbox_run_paths",
    "compute_sandbox_paths",
    "verify_srt_installation",
    "_preflight_srt",
    "provider_post_sandbox",
    "runtime_prepare_spawn",
)


def start_spawn_phase() -> float:
    return time.perf_counter()


def finish_spawn_phase(
    timings_ms: MutableMapping[str, float] | None,
    phase: str,
    started_at: float,
) -> None:
    if timings_ms is not None:
        timings_ms[phase] = (time.perf_counter() - started_at) * 1000


def complete_spawn_phase_timings(timings_ms: Mapping[str, float]) -> dict[str, float]:
    return {phase: round(timings_ms.get(phase, 0.0), 3) for phase in SPAWN_PHASES}
