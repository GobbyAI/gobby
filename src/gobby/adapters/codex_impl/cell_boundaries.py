"""Yielded cells that a Codex rollout boundary ends.

A yielded cell that is never waited on has no completion record, so without a boundary it
would refuse every literal cwd proof for the rest of the rollout. The turn's task_complete,
followed by a compaction, ends it per cell: a cell yielded after the turn end survives. The
rollout cannot resume an ended cell, but a wait on its ID still can, so ended IDs stay
revivable and a re-yielding wait keeps the cell tracked.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any

from gobby.adapters.codex_impl.pending_execution import PendingExecution


class CellBoundaries:
    def __init__(self) -> None:
        self._turn_done: set[str] = set()
        self._ended: set[str] = set()

    def yielded(self, cell_id: str) -> None:
        """A cell that yields again is live again, whatever boundary it passed before."""
        self._turn_done.discard(cell_id)
        self._ended.discard(cell_id)

    def turn_completed(self, cell_ids: Iterable[str]) -> None:
        self._turn_done.update(cell_ids)

    def compacted(self, cells: MutableMapping[str, PendingExecution]) -> None:
        for cell_id in [cell_id for cell_id in cells if cell_id in self._turn_done]:
            del cells[cell_id]
            self._ended.add(cell_id)
        self._turn_done.clear()

    def revives(self, cell_id: str) -> bool:
        return cell_id in self._ended

    def snapshot(self) -> dict[str, list[str]]:
        return {"turn_done": sorted(self._turn_done), "ended": sorted(self._ended)}

    def hydrate(self, value: Any) -> None:
        if not isinstance(value, Mapping):
            return
        turn_done = value.get("turn_done")
        ended = value.get("ended")
        self._turn_done = (
            {str(cell) for cell in turn_done} if isinstance(turn_done, list) else set()
        )
        self._ended = {str(cell) for cell in ended} if isinstance(ended, list) else set()
