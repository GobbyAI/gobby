"""Spawn timing names the workspace overlay index phase `code_overlay_index`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.agents import spawn_timing
from gobby.agents.code_index import ensure_isolation_code_index

pytestmark = pytest.mark.unit

GCODE = "/usr/local/bin/gcode"


@pytest.mark.asyncio
async def test_spawn_phase_uses_overlay_name(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[list[str]] = []
    phase_timings: dict[str, float] = {}

    async def run_gcode(
        args: list[str],
        *,
        timeout: float,
        timeout_code: str,
        failure_code: str,
        env: dict[str, str] | None = None,
    ) -> None:
        calls.append(list(args))

    with patch("gobby.agents.code_index._run_gcode", side_effect=run_gcode):
        await ensure_isolation_code_index(
            str(workspace), gcode_bin=Path(GCODE), phase_timings_ms=phase_timings
        )

    assert any(call[1] == "index" for call in calls)
    assert "code_overlay_index" in phase_timings
    assert "code_index_index" not in phase_timings
    assert "code_overlay_index" in spawn_timing.TOP_LEVEL_SPAWN_PHASES
    assert "code_overlay_index" in spawn_timing.SPAWN_PHASES
    assert "code_index_index" not in spawn_timing.SPAWN_PHASES
