"""Dimension bounds at Python ingress (plan 2.2.5)."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from gobby.terminals.dimensions import (
    MAX_CELLS,
    MAX_COLS,
    MAX_FRAME_SIZE,
    MAX_ROWS,
    WORST_CELL_BYTES,
    InvalidTerminalDimensionsError,
    validate_dimensions,
)
from gobby.terminals.runtime import TerminalSpawnRequest
from gobby.terminals.tmux_runtime import TmuxTerminalRuntime
from tests.terminals.fakes import make_memory_terminal

pytestmark = pytest.mark.unit


def test_bounds_rejected_before_side_effects() -> None:
    assert MAX_FRAME_SIZE == 2 * 1024 * 1024
    assert WORST_CELL_BYTES * MAX_CELLS <= MAX_FRAME_SIZE
    assert MAX_ROWS * MAX_COLS > MAX_CELLS

    invalid: list[tuple[object, object]] = [
        (0, 80),
        (-1, 80),
        (24, 0),
        (24, -8),
        (True, 80),
        (24, False),
        (1.5, 80),
        (24, 3.2),
        (MAX_ROWS + 1, 1),
        (1, MAX_COLS + 1),
        (MAX_ROWS, MAX_COLS),
    ]
    for rows, cols in invalid:
        with pytest.raises(InvalidTerminalDimensionsError):
            validate_dimensions(cast(int, rows), cast(int, cols))

    validate_dimensions(1, 1)
    validate_dimensions(24, 80)


@pytest.mark.asyncio
async def test_prepare_spawn_and_resize_validate_before_backend() -> None:
    sessions = MagicMock()
    sessions.create_session = AsyncMock()
    sessions._run = AsyncMock(return_value=(0, "", ""))
    runtime = TmuxTerminalRuntime(sessions)
    terminal = make_memory_terminal()
    request = TerminalSpawnRequest(
        terminal_id=uuid4(),
        spawn_key="gobby-key",
        command=["echo", "hi"],
        rows=0,
        cols=80,
    )
    with pytest.raises(InvalidTerminalDimensionsError):
        await runtime.prepare_spawn(request)
    sessions.create_session.assert_not_awaited()

    sessions._run = AsyncMock()
    with pytest.raises(InvalidTerminalDimensionsError):
        await runtime.resize(terminal, 0, 80)
    sessions._run.assert_not_awaited()
    sessions.create_session.assert_not_awaited()
