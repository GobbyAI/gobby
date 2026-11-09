"""Yielded-cell boundaries: a turn end plus a compaction ends a cell, and the state round-trips."""

from __future__ import annotations

import pytest

from gobby.adapters.codex_impl.cell_boundaries import CellBoundaries
from gobby.adapters.codex_impl.pending_execution import PendingExecution

pytestmark = pytest.mark.unit


def test_turn_end_then_compaction_ends_the_cell_but_keeps_it_revivable() -> None:
    boundaries = CellBoundaries()
    cells = {"cell-y": PendingExecution("launch")}
    boundaries.turn_completed(cells)
    boundaries.compacted(cells)
    assert cells == {}
    assert boundaries.revives("cell-y")


def test_a_cell_that_yields_again_is_live_again() -> None:
    boundaries = CellBoundaries()
    cells = {"cell-y": PendingExecution("launch")}
    boundaries.turn_completed(cells)
    boundaries.compacted(cells)
    boundaries.yielded("cell-y")
    assert not boundaries.revives("cell-y")


def test_state_round_trips_through_a_snapshot() -> None:
    boundaries = CellBoundaries()
    boundaries.turn_completed(["cell-y"])
    restored = CellBoundaries()
    restored.hydrate(boundaries.snapshot())
    cells = {"cell-y": PendingExecution("launch")}
    restored.compacted(cells)
    assert cells == {}
    assert restored.revives("cell-y")


def test_hydrate_ignores_a_malformed_state() -> None:
    boundaries = CellBoundaries()
    boundaries.hydrate("not a mapping")
    assert not boundaries.revives("cell-y")
