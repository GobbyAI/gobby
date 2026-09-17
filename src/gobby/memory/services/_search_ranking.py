"""The ordering policy for memory search results.

``build_results`` and the offline recall replay both order hits through
``order_results``, so live and replayed order cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import NamedTuple


class HitScores(NamedTuple):
    """The scores a hit is ordered on; both similarities are None for an unscored hit."""

    undecayed: float | None
    decayed: float | None
    fused: float


def order_results[T](hits: Sequence[T], scores: Callable[[T], HitScores]) -> list[T]:
    """Return ``hits`` best first: the similarity and fused orders interleaved.

    Scored hits are ranked twice -- by undecayed similarity, and by the fused
    score -- and the two rankings alternate, similarity first, each hit keeping
    its earlier slot. A hit several searches confirm therefore reaches the top
    few without displacing the best semantic match, and neither order can bury
    the other's leader. Decayed similarity only breaks ties (#21010). Unscored
    hits follow every scored one, by fused score.

    The graded cohort in ``tests/memory/fixtures/ranking_cohort.json`` selected
    this policy over similarity alone, fused alone, and three weighted blends.
    """
    ranked: dict[int, tuple[float, float, float]] = {}
    unranked: dict[int, float] = {}
    for index, hit in enumerate(hits):
        undecayed, decayed, fused = scores(hit)
        if undecayed is None or decayed is None:
            unranked[index] = fused
        else:
            ranked[index] = (undecayed, decayed, fused)
    by_similarity = sorted(ranked, key=lambda index: ranked[index], reverse=True)
    by_fused = sorted(
        ranked,
        key=lambda index: (ranked[index][2], ranked[index][0], ranked[index][1]),
        reverse=True,
    )
    alternating = (index for pair in zip(by_similarity, by_fused, strict=True) for index in pair)
    order = list(dict.fromkeys(alternating))
    order += sorted(unranked, key=lambda index: unranked[index], reverse=True)
    return [hits[index] for index in order]
