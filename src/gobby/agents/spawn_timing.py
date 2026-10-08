"""Shared phase timing for managed agent spawns."""

from __future__ import annotations

import time
from collections.abc import Mapping, MutableMapping

TOP_LEVEL_SPAWN_PHASES = (
    "isolation_prepare",
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

ISOLATION_REPAIR_PHASES = (
    "isolation_hook_copy",
    "isolation_project_marker",
    "python_env_seed",
    "isolation_mcp_config",
    "isolation_git_hygiene",
)

SPAWN_PHASES = TOP_LEVEL_SPAWN_PHASES + ISOLATION_REPAIR_PHASES + ("spawn_wall", "unattributed")


def start_spawn_phase() -> float:
    return time.perf_counter()


def finish_spawn_phase(
    timings_ms: MutableMapping[str, float] | None,
    phase: str,
    started_at: float,
) -> None:
    if timings_ms is not None:
        timings_ms[phase] = (time.perf_counter() - started_at) * 1000


def complete_spawn_phase_timings(
    timings_ms: Mapping[str, float], *, started_at: float
) -> dict[str, float]:
    wall_ms = (time.perf_counter() - started_at) * 1000
    result = {phase: round(timings_ms.get(phase, 0.0), 3) for phase in SPAWN_PHASES}
    result["spawn_wall"] = round(wall_ms, 3)
    result["unattributed"] = round(
        wall_ms - sum(timings_ms.get(phase, 0.0) for phase in TOP_LEVEL_SPAWN_PHASES), 3
    )
    return result
