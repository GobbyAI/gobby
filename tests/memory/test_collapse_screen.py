"""Near-duplicate collapse must stay exactly equivalent while skipping clear non-folds (#22910)."""

from __future__ import annotations

import math
import random

import pytest

from gobby.memory.services import _search_results
from gobby.memory.services._search_results import collapse_near_duplicates
from gobby.storage.memories import Memory

pytestmark = pytest.mark.unit

THRESHOLD = 0.92
DIM = 768


def _memory(memory_id: str) -> Memory:
    memory = Memory.__new__(Memory)
    object.__setattr__(memory, "id", memory_id)
    object.__setattr__(memory, "collapsed_duplicates", None)
    return memory


def _reference_cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def _reference_collapse(
    ids: list[str], vectors: dict[str, list[float]], threshold: float
) -> list[tuple[str, list[str] | None]]:
    """The pre-screen greedy fold, computing every pair exactly."""
    kept: list[tuple[str, list[str] | None]] = []
    for memory_id in ids:
        vector = vectors.get(memory_id)
        representative: int | None = None
        if vector is not None:
            for index, (candidate_id, _) in enumerate(kept):
                candidate_vector = vectors.get(candidate_id)
                if candidate_vector is None:
                    continue
                if _reference_cosine(vector, candidate_vector) >= threshold:
                    representative = index
                    break
        if representative is None:
            kept.append((memory_id, None))
            continue
        candidate_id, folded = kept[representative]
        kept[representative] = (candidate_id, [*(folded or []), memory_id])
    return kept


def _collapse(
    ids: list[str], vectors: dict[str, list[float]], threshold: float
) -> list[tuple[str, list[str] | None]]:
    kept = collapse_near_duplicates([_memory(mid) for mid in ids], vectors, threshold)
    return [(memory.id, memory.collapsed_duplicates) for memory in kept]


def _gauss(rng: random.Random, dim: int = DIM) -> list[float]:
    return [rng.gauss(0.0, 1.0) for _ in range(dim)]


def _at_cosine(rng: random.Random, anchor: list[float], cosine: float) -> list[float]:
    """A vector whose cosine to ``anchor`` is ``cosine`` up to rounding."""
    anchor_norm = math.sqrt(sum(a * a for a in anchor))
    unit = [a / anchor_norm for a in anchor]
    other = _gauss(rng, len(anchor))
    projection = sum(a * b for a, b in zip(other, unit, strict=True))
    orthogonal = [b - projection * a for a, b in zip(unit, other, strict=True)]
    orthogonal_norm = math.sqrt(sum(a * a for a in orthogonal))
    sine = math.sqrt(1.0 - cosine * cosine)
    return [cosine * a + sine * b / orthogonal_norm for a, b in zip(unit, orthogonal, strict=True)]


def test_distinct_hits_skip_the_exact_pairwise_cosine(monkeypatch: pytest.MonkeyPatch) -> None:
    rng = random.Random(22910)
    ids = [f"m{index}" for index in range(60)]
    vectors = {memory_id: _gauss(rng) for memory_id in ids}
    exact = _search_results._cosine_from_norms
    calls = 0

    def counted(
        left: list[float], right: list[float], left_norm: float, right_norm: float
    ) -> float:
        nonlocal calls
        calls += 1
        return exact(left, right, left_norm, right_norm)

    monkeypatch.setattr(_search_results, "_cosine_from_norms", counted)

    assert _collapse(ids, vectors, THRESHOLD) == [(memory_id, None) for memory_id in ids]
    # Random 768-dim directions sit near cosine 0, far below the threshold, so no
    # pair needs the exact arithmetic; before the screen all 1,770 pairs did.
    assert calls == 0


def test_near_threshold_pairs_fold_exactly_as_the_reference() -> None:
    rng = random.Random(7)
    anchor = _gauss(rng)
    vectors = {"anchor": anchor}
    offsets = (-1e-9, -1e-12, -1e-15, -2e-16, 0.0, 2e-16, 1e-15, 1e-12, 1e-9)
    for index, offset in enumerate(offsets):
        vectors[f"near{index}"] = _at_cosine(rng, anchor, THRESHOLD + offset)
    ids = list(vectors)
    reference = _reference_collapse(ids, vectors, THRESHOLD)

    assert _collapse(ids, vectors, THRESHOLD) == reference
    # The band is genuinely exercised: both outcomes occur among the near pairs.
    folded = reference[0][1] or []
    assert 0 < len(folded) < len(offsets)


def test_greedy_chain_keeps_the_first_kept_representative() -> None:
    rng = random.Random(11)
    first = _gauss(rng)
    middle = _at_cosine(rng, first, 0.93)
    # Close to ``middle`` but not to ``first``: it must fold into ``middle`` only
    # if ``middle`` was kept, and here ``middle`` folds into ``first``.
    last = _at_cosine(rng, middle, 0.95)
    vectors = {"first": first, "middle": middle, "last": last}
    ids = list(vectors)

    assert _collapse(ids, vectors, THRESHOLD) == _reference_collapse(ids, vectors, THRESHOLD)


@pytest.mark.parametrize(
    "special",
    [
        pytest.param([0.0] * DIM, id="zero"),
        pytest.param([math.nan] + [1.0] * (DIM - 1), id="nan"),
        pytest.param([math.inf] + [1.0] * (DIM - 1), id="inf"),
        pytest.param([1e300] * DIM, id="overflowing-norm"),
        pytest.param([1e-320] * DIM, id="subnormal"),
        pytest.param([1.0] * (DIM // 2), id="short-dimension"),
    ],
)
def test_unscreenable_vectors_take_the_exact_path(special: list[float]) -> None:
    rng = random.Random(3)
    anchor = _gauss(rng)
    vectors = {
        "anchor": anchor,
        "special": special,
        "near": _at_cosine(rng, anchor, 0.95),
        "far": _gauss(rng),
    }
    ids = ["anchor", "special", "near", "far", "missing"]

    for threshold in (THRESHOLD, 0.0, -0.5):
        assert _collapse(ids, vectors, threshold) == _reference_collapse(ids, vectors, threshold)


def test_random_clusters_match_the_reference() -> None:
    rng = random.Random(22642)
    centers = [_gauss(rng) for _ in range(6)]
    vectors: dict[str, list[float]] = {}
    for index in range(90):
        center = centers[index % len(centers)]
        vectors[f"m{index}"] = _at_cosine(rng, center, rng.uniform(0.85, 0.99))
    ids = list(vectors)
    rng.shuffle(ids)

    assert _collapse(ids, vectors, THRESHOLD) == _reference_collapse(ids, vectors, THRESHOLD)


def test_vectors_over_the_dimension_cap_take_the_exact_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rng = random.Random(5)
    ids = [f"m{index}" for index in range(12)]
    vectors = {memory_id: _gauss(rng) for memory_id in ids}
    vectors["m11"] = _at_cosine(rng, vectors["m0"], 0.95)
    exact = _search_results._cosine_from_norms
    calls = 0

    def counted(
        left: list[float], right: list[float], left_norm: float, right_norm: float
    ) -> float:
        nonlocal calls
        calls += 1
        return exact(left, right, left_norm, right_norm)

    monkeypatch.setattr(_search_results, "_cosine_from_norms", counted)
    monkeypatch.setattr(_search_results, "_SCREEN_MAX_DIM", DIM - 1)

    assert _collapse(ids, vectors, THRESHOLD) == _reference_collapse(ids, vectors, THRESHOLD)
    # m0..m10 are all kept, so each of them compares against every earlier one, and
    # m11 folds into m0 on its first comparison: 55 + 1 exact calls, none screened.
    assert calls == 56
