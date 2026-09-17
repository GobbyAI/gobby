"""The memory search ordering policy, and the graded cohort that selected it.

``fixtures/ranking_cohort.json`` holds 24 queries, each with the need it serves
and the hits live search had to order at ``limit=20``, graded A to D against
that need; the fixture's ``source`` field says how that pool was drawn. The
closed candidate set below is scored on it and one stated rule picks the winner;
``order_results`` must be that winner. To change the ordering, re-collect the
cohort and let the rule choose -- do not edit the policy to taste.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

import pytest

from gobby.memory.services._search_ranking import HitScores, order_results

pytestmark = pytest.mark.unit

_FIXTURE = Path(__file__).parent / "fixtures" / "ranking_cohort.json"
# Three searches each ranking a hit first, at the default ``rrf_k`` of 60.
_FUSED_CEILING = 3 / 61
_GAIN = {"A": 3, "B": 1, "C": 0, "D": 0}
_EVIDENCE_QUERY = "M1"
_EVIDENCE_MEMORY = "002c13ae"


class Hit(TypedDict):
    id: str
    grade: str
    grader: str
    undecayed: float | None
    decayed: float | None
    fused: float
    # None when the logged search did not return the hit, so no leg list was recorded.
    search_via: str | None


class Query(TypedDict):
    id: str
    style: str
    query: str
    need: str
    hits: list[Hit]


class Cohort(TypedDict):
    policy: str
    surface_min_score: float
    queries: list[Query]


Ordering = Callable[[list[Hit]], list[Hit]]


def _by_key(key: Callable[[Hit], tuple[float, ...]]) -> Ordering:
    def order(hits: list[Hit]) -> list[Hit]:
        scored = [hit for hit in hits if hit["undecayed"] is not None]
        unscored = [hit for hit in hits if hit["undecayed"] is None]
        return sorted(scored, key=key, reverse=True) + sorted(
            unscored, key=lambda hit: hit["fused"], reverse=True
        )

    return order


def _undecayed(hit: Hit) -> float:
    assert hit["undecayed"] is not None
    return hit["undecayed"]


def _decayed(hit: Hit) -> float:
    assert hit["decayed"] is not None
    return hit["decayed"]


def _blend(weight: float) -> Ordering:
    return _by_key(
        lambda hit: (
            _undecayed(hit) + weight * hit["fused"] / _FUSED_CEILING,
            _undecayed(hit),
            _decayed(hit),
        )
    )


_COSINE = _by_key(lambda hit: (_undecayed(hit), _decayed(hit), hit["fused"]))
_FUSED = _by_key(lambda hit: (hit["fused"], _undecayed(hit), _decayed(hit)))


def _interleave(hits: list[Hit]) -> list[Hit]:
    merged: list[Hit] = []
    seen: set[str] = set()
    for pair in zip(_COSINE(hits), _FUSED(hits), strict=True):
        for hit in pair:
            if hit["id"] not in seen:
                seen.add(hit["id"])
                merged.append(hit)
    return merged


# Listed order is the selection rule's final tiebreak: the earlier candidate wins.
_CANDIDATES: dict[str, Ordering] = {
    "cosine": _COSINE,
    "fused": _FUSED,
    "blend-0.1": _blend(0.1),
    "blend-0.2": _blend(0.2),
    "blend-0.3": _blend(0.3),
    "interleave": _interleave,
}


def _shipped(hits: list[Hit]) -> list[Hit]:
    return order_results(
        hits, lambda hit: HitScores(hit["undecayed"], hit["decayed"], hit["fused"])
    )


def _hit(name: str, undecayed: float | None, decayed: float | None, fused: float) -> Hit:
    return {
        "id": name,
        "grade": "D",
        "grader": "test",
        "undecayed": undecayed,
        "decayed": decayed,
        "fused": fused,
        "search_via": None,
    }


def _dcg(grades: list[str]) -> float:
    return sum(_GAIN[grade] / math.log2(rank + 2) for rank, grade in enumerate(grades))


def _score(order: Ordering, queries: list[Query]) -> tuple[int, int, float]:
    """(A@3, A@1, mean nDCG@5): the selection rule compares these left to right."""
    a_at_3 = a_at_1 = 0
    ndcg: list[float] = []
    for query in queries:
        grades = [hit["grade"] for hit in order(query["hits"])]
        a_at_1 += grades[0] == "A"
        a_at_3 += "A" in grades[:3]
        ideal = _dcg(sorted(grades, key=lambda grade: _GAIN[grade], reverse=True)[:5])
        if ideal:
            ndcg.append(_dcg(grades[:5]) / ideal)
    return a_at_3, a_at_1, sum(ndcg) / len(ndcg)


def _winner(queries: list[Query]) -> str:
    names = list(_CANDIDATES)
    return max(names, key=lambda name: (_score(_CANDIDATES[name], queries), -names.index(name)))


@pytest.fixture(scope="module")
def cohort() -> Cohort:
    loaded: Cohort = json.loads(_FIXTURE.read_text())
    return loaded


def test_an_unscored_hit_sorts_after_every_scored_hit() -> None:
    """A hit no search could score backfills; its fused score never lifts it past a scored one."""
    hits = [
        _hit("unscored", None, None, _FUSED_CEILING),
        _hit("weakest-scored", 0.01, 0.001, 0.0),
    ]

    assert [hit["id"] for hit in _shipped(hits)] == ["weakest-scored", "unscored"]


def test_age_only_breaks_ties_between_otherwise_equal_hits() -> None:
    """Decay stays off the primary axis (#21010): it orders equals and buries nothing."""
    hits = [
        _hit("aged", 0.70, 0.30, 0.0164),
        _hit("fresh", 0.70, 0.69, 0.0164),
        _hit("stronger-but-aged", 0.75, 0.20, 0.0164),
    ]

    assert [hit["id"] for hit in _shipped(hits)] == ["stronger-but-aged", "fresh", "aged"]


def test_the_top_fused_hit_lands_second_behind_the_top_similarity_hit() -> None:
    """The two orders alternate, similarity first, and a hit both rank takes one slot."""
    hits = [
        _hit("similar", 0.80, 0.80, 0.0150),
        _hit("middling", 0.70, 0.70, 0.0160),
        _hit("confirmed", 0.60, 0.60, 0.0480),
    ]

    assert [hit["id"] for hit in _shipped(hits)] == ["similar", "confirmed", "middling"]


def test_cohort_is_complete_and_graded(cohort: Cohort) -> None:
    queries = cohort["queries"]

    assert len(queries) == 24
    assert len({query["id"] for query in queries}) == 24
    for query in queries:
        assert query["need"] and query["query"] and query["style"] in {"intent", "keyword"}
        assert query["hits"], query["id"]
        assert {hit["grade"] for hit in query["hits"]} <= set(_GAIN), query["id"]


def test_shipped_policy_is_selection_rule_winner(cohort: Cohort) -> None:
    winner = _winner(cohort["queries"])

    assert cohort["policy"] == winner
    for query in cohort["queries"]:
        shipped = [hit["id"] for hit in _shipped(query["hits"])]
        selected = [hit["id"] for hit in _CANDIDATES[winner](query["hits"])]
        assert shipped == selected, query["id"]


def test_shipped_policy_does_not_regress_cosine(cohort: Cohort) -> None:
    shipped_a3, shipped_a1, _ = _score(_shipped, cohort["queries"])
    cosine_a3, cosine_a1, _ = _score(_COSINE, cohort["queries"])

    assert shipped_a3 >= cosine_a3
    assert shipped_a1 >= cosine_a1


def test_evidence_memory_reaches_top_three(cohort: Cohort) -> None:
    """Task #22410's case: the memory that answered the query ranked last of eight."""
    evidence = next(query for query in cohort["queries"] if query["id"] == _EVIDENCE_QUERY)
    top_three = [hit["id"][:8] for hit in _shipped(evidence["hits"])[:3]]

    assert _EVIDENCE_MEMORY in top_three


def test_surface_floor_admits_nine_in_ten_a_grade_hits(cohort: Cohort) -> None:
    floor = cohort["surface_min_score"]
    a_grade = [
        _undecayed(hit)
        for query in cohort["queries"]
        for hit in query["hits"]
        if hit["grade"] == "A" and hit["undecayed"] is not None
    ]
    admitted = sum(score >= floor for score in a_grade) / len(a_grade)
    admitted_one_step_up = sum(score >= floor + 0.01 for score in a_grade) / len(a_grade)

    assert admitted >= 0.9
    assert admitted_one_step_up < 0.9, "the floor is not the largest two-decimal value"
