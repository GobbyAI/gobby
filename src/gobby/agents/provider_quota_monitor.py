"""Spawned-run provider quota observation (plan 4.4).

Pane captures from spawned terminals feed the shared capacity service as hints
only. A hint records a possible limit; it never terminalizes a run. Only the
verified transcript path in the idle handler fails a run.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from datetime import datetime
from typing import TYPE_CHECKING

from gobby.providers.quota_signals import PaneOccurrenceTracker, pane_generation

if TYPE_CHECKING:
    from gobby.providers.capacity_service import ProviderCapacityService
    from gobby.storage.terminals import Terminal
    from gobby.terminals.runtime import SnapshotResult

logger = logging.getLogger(__name__)


class ProviderQuotaMonitor:
    """Feeds spawned-run pane captures into the shared capacity service as hints only."""

    def __init__(self, service: ProviderCapacityService) -> None:
        self._service = service
        self._tracker = PaneOccurrenceTracker()
        self._live: dict[str, frozenset[str]] = {}

    async def observe_capture(
        self,
        *,
        provider: str,
        terminal: Terminal,
        snapshot: SnapshotResult,
        detected_at: datetime,
    ) -> None:
        """Admit one capture; a replayed or unmatched occurrence records nothing."""
        generation = pane_generation(terminal)
        hint = self._tracker.observe(
            provider=provider,
            generation=generation,
            snapshot=snapshot,
            detected_at=detected_at,
        )
        if hint is None:
            return
        await self._service.observe_pane_hint(hint, generation=generation)

    def rebaseline(self, terminal: Terminal) -> None:
        """Drop a terminal's prior capture after an outage, so its next capture starts fresh."""
        self._tracker.rebaseline(pane_generation(terminal))

    async def retire_dead(
        self, live_by_provider: Mapping[str, Collection[str]], *, owner: str
    ) -> None:
        """Retire pane generations whose terminal left this owner's live set since the last pass.

        A provider's generations are retired only when its live set changed, so a
        steady pass writes nothing. Failed retirements keep their state and retry.
        The sink keeps any generation another owner still reports live.
        """
        current = {provider: frozenset(live) for provider, live in live_by_provider.items()}
        for provider in self._live.keys() | current.keys():
            before = self._live.get(provider, frozenset())
            after = current.get(provider, frozenset())
            if before == after:
                continue
            await self._service.retire_pane_generations(provider, after, owner=owner)
            for generation in before - after:
                self._tracker.rebaseline(generation)
            if after:
                self._live[provider] = after
            else:
                self._live.pop(provider, None)
