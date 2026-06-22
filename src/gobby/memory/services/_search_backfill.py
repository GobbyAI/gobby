"""Candidate over-fetch/backfill loop for memory search."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from gobby.memory.services._search_constants import (
    _BACKFILL_GROWTH,
    _MAX_BACKFILL_ROUNDS,
    _OVERFETCH_FACTOR,
)
from gobby.memory.services._search_models import _Candidates
from gobby.storage.memories import Memory


async def collect_active_results(
    *,
    limit: int,
    collect: Callable[[int], Awaitable[_Candidates]],
    build: Callable[[_Candidates], list[Memory]],
) -> tuple[list[Memory], _Candidates]:
    """Over-fetch ranked candidates and backfill until ``limit`` active results."""
    candidate_limit = max(limit, 1) * _OVERFETCH_FACTOR
    candidates = await collect(candidate_limit)
    results = build(candidates)
    rounds = 0
    while len(results) < limit and not candidates.exhausted and rounds < _MAX_BACKFILL_ROUNDS:
        rounds += 1
        candidate_limit *= _BACKFILL_GROWTH
        candidates = await collect(candidate_limit)
        results = build(candidates)
    return results, candidates
