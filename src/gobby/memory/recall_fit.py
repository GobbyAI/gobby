"""Offline fit/eval over logged recall-signal rows (#17197, epic #17099).

This module generalizes the offline recall benchmark harness to real labeled
data. It consumes request-aligned per-hit feature rows from the promoted hub
tables and replays the FULL ranking path —
the ``SearchService`` blend ordering from ``build_results`` (semantic-first on
the similarity axis with temporal decay and ``ranking_mode`` semantics, RRF
``ranking_score`` as tiebreak) — under counterfactual parameters, without
re-running retrieval.

Scope and semantics (contract: docs/contracts/memory-usefulness-label.md):

- **Request-balanced evaluation.** Every request containing both relevance
  classes contributes one effective unit regardless of its pair cardinality.
- **Never-retrieved memories are unlabeled, not negative.** The pairwise
  objective forms pairs only between explicitly labeled rows within the same
  recall request. Rows without a label contribute to propensity estimation
  (denominators) only.
- **Scope-specific weighting.** Full shadow cohorts weight pairs uniformly.
  Injected cohorts preserve relative clipped IPS weights within each request.
- **Per-project splits.** Requests are split train/eval within each project.
  The fitting procedure that consumes those splits — grid search with
  per-project shrinkage toward the pooled fit — lives in
  ``recall_fit_shrinkage``; this module owns the replay algebra and the
  metrics both it and the candidate-filter replay score against.

Replay algebra (exact unless noted):

- Temporal decay is exponential (``0.5 ** (age / half_life)``), so a row
  logged under half-life ``h0`` replays under ``h1`` as
  ``decay ** (h0 / h1)`` — exact, no timestamps needed.
- Semantic rows: ``similarity = base * decay`` where ``base`` preserves every
  pre-decay factor (raw score, user-source boost) at its logged value.
- ``graph_synthetic`` rows: ``similarity = graph_score * discount * decay``;
  the logged discount is recovered algebraically when the request row lacks
  it. Re-blending ``COOCCUR_ALPHA``/``COOCCUR_SUPPORT_CAP`` rescales
  ``graph_score`` by the attributed edge's new/old blend ratio — first-order:
  exact for single-edge attribution, approximate for multi-hop aggregates.
  Raw support is recovered from ``edge_support_norm`` under the logging-time
  cap; a saturated norm (1.0) only lower-bounds support, so re-caps upward
  are conservative there.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal

from gobby.memory.services.knowledge_graph.writer import (
    COOCCUR_ALPHA,
    COOCCUR_SUPPORT_CAP,
)

_SORT_NONE_SIM = float("-inf")

# Propensity keys are (injection_group, injection_position); position is the
# rendered ordinal within the injection block, per the label contract §5.
PropensityKey = tuple[str | None, int]
WeightingMode = Literal["full", "injected"]

REQUEST_SPLIT_VERSION = "recall-request-hash-split-v1"
PAIRWISE_EVALUATOR_VERSION = "recall-request-normalized-pairwise-v1"
AUDIT_SAMPLER_VERSION = "recall-training-request-sampler-v1"


def evaluation_protocol_identity(*, split_version: str = REQUEST_SPLIT_VERSION) -> dict[str, str]:
    """Version fields that bind fitting, audit sampling, and holdout evaluation."""
    return {
        "split_version": split_version,
        "evaluator_version": PAIRWISE_EVALUATOR_VERSION,
        "audit_sampler_version": AUDIT_SAMPLER_VERSION,
    }


@dataclass(frozen=True)
class ReplayRow:
    """One injected hit with its logged full-ranking-path features.

    ``judge_useful is None`` means unlabeled: the row informs propensity
    estimation only and never forms preference pairs.
    """

    recall_request_id: str
    memory_id: str
    project_id: str | None
    rank: int
    similarity: float | None
    raw_semantic_score: float | None
    temporal_decay_factor: float | None
    ranking_score: float
    ranking_mode: str | None
    graph_score: float | None
    edge_cosine: float | None
    edge_support_norm: float | None
    edge_weight_blend: float | None
    injection_position: int | None
    injection_group: str | None
    judge_useful: bool | None
    label_source: str | None
    logged_half_life_days: float | None
    logged_graph_discount: float | None
    logged_cooccur_alpha: float = COOCCUR_ALPHA
    logged_cooccur_support_cap: int = COOCCUR_SUPPORT_CAP


@dataclass(frozen=True)
class ReplayParams:
    """Counterfactual recall constants. ``None`` keeps the logged value.

    ``half_life_days`` and ``graph_synthetic_discount`` replay exactly;
    ``cooccur_alpha``/``cooccur_support_cap`` rescale ``graph_synthetic`` rows
    to first order via the attributed edge components.
    """

    half_life_days: float | None = None
    graph_synthetic_discount: float | None = None
    cooccur_alpha: float | None = None
    cooccur_support_cap: int | None = None

    def __post_init__(self) -> None:
        if self.half_life_days is not None and self.half_life_days <= 0:
            raise ValueError(f"half_life_days must be positive, got {self.half_life_days}")
        if self.cooccur_support_cap is not None and self.cooccur_support_cap <= 0:
            raise ValueError(
                f"cooccur_support_cap must be positive, got {self.cooccur_support_cap}"
            )
        if self.cooccur_alpha is not None and not 0.0 < self.cooccur_alpha <= 1.0:
            raise ValueError(f"cooccur_alpha must be in (0, 1], got {self.cooccur_alpha}")


def replay_row_from_signal_row(row: Mapping[str, Any]) -> ReplayRow:
    """Adapt one ``RecallSignalStore.fetch_replay_rows`` dict to a ``ReplayRow``.

    The logging-time half-life comes from the request ``weighting`` snapshot;
    the logging-time co-occurrence constants are not logged (they were frozen
    module constants), so the writer's current values are assumed.
    """
    weighting = row.get("weighting") or {}
    half_life = weighting.get("temporal_decay_half_life_days")
    return ReplayRow(
        recall_request_id=str(row["recall_request_id"]),
        memory_id=str(row["memory_id"]),
        project_id=row.get("project_id"),
        rank=int(row["rank"]),
        similarity=_float_or_none(row.get("similarity")),
        raw_semantic_score=_float_or_none(row.get("raw_semantic_score")),
        temporal_decay_factor=_float_or_none(row.get("temporal_decay_factor")),
        ranking_score=_float_or_none(row.get("ranking_score")) or 0.0,
        ranking_mode=row.get("ranking_mode"),
        graph_score=_float_or_none(row.get("graph_score")),
        edge_cosine=_float_or_none(row.get("edge_cosine")),
        edge_support_norm=_float_or_none(row.get("edge_support_norm")),
        edge_weight_blend=_float_or_none(row.get("edge_weight_blend")),
        injection_position=row.get("injection_position"),
        injection_group=row.get("injection_group"),
        judge_useful=row.get("judge_useful"),
        label_source=row.get("label_source"),
        logged_half_life_days=_float_or_none(half_life),
        logged_graph_discount=_float_or_none(row.get("graph_synthetic_similarity_discount")),
    )


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Replay: counterfactual similarity + build_results ordering                   #
# --------------------------------------------------------------------------- #


def _replayed_decay(row: ReplayRow, params: ReplayParams) -> float | None:
    """Logged decay factor re-exponentiated to the counterfactual half-life."""
    decay = row.temporal_decay_factor
    if decay is None:
        return None
    if params.half_life_days is None or row.logged_half_life_days is None:
        return decay
    if decay <= 0.0:
        return decay
    return float(decay ** (row.logged_half_life_days / params.half_life_days))


def _replayed_edge_blend_ratio(row: ReplayRow, params: ReplayParams) -> float:
    """First-order graph-score rescale from the attributed edge components."""
    if params.cooccur_alpha is None and params.cooccur_support_cap is None:
        return 1.0
    if row.edge_cosine is None or row.edge_support_norm is None or not row.edge_weight_blend:
        return 1.0
    alpha = params.cooccur_alpha if params.cooccur_alpha is not None else row.logged_cooccur_alpha
    cap = (
        params.cooccur_support_cap
        if params.cooccur_support_cap is not None
        else row.logged_cooccur_support_cap
    )
    # Recover raw support under the logging-time cap; a saturated norm only
    # lower-bounds it, so re-caps upward are conservative for those rows.
    raw_support = row.edge_support_norm * row.logged_cooccur_support_cap
    support_norm = min(raw_support, float(cap)) / float(cap)
    new_blend = alpha * row.edge_cosine + (1.0 - alpha) * support_norm
    return new_blend / row.edge_weight_blend


def replayed_similarity(row: ReplayRow, params: ReplayParams) -> float | None:
    """Recompute the blended similarity under counterfactual parameters."""
    decay = _replayed_decay(row, params)
    if row.ranking_mode == "graph_synthetic":
        return _replayed_graph_synthetic(row, params, decay)
    if row.raw_semantic_score is not None and row.similarity is not None:
        logged_decay = row.temporal_decay_factor
        if decay is None or logged_decay is None or logged_decay <= 0.0:
            return row.similarity
        # base preserves every pre-decay factor (raw score, user boost).
        base = row.similarity / logged_decay
        return base * decay
    return row.similarity


def _replayed_graph_synthetic(
    row: ReplayRow, params: ReplayParams, decay: float | None
) -> float | None:
    if row.graph_score is None or row.similarity is None:
        return row.similarity
    logged_decay = row.temporal_decay_factor
    if decay is None or logged_decay is None or logged_decay <= 0.0:
        return row.similarity
    discount = params.graph_synthetic_discount
    if discount is None:
        discount = row.logged_graph_discount
    if discount is None:
        # Recover the logged discount algebraically from the logged blend.
        if row.graph_score <= 0.0:
            return row.similarity
        discount = row.similarity / (row.graph_score * logged_decay)
    graph_score = row.graph_score * _replayed_edge_blend_ratio(row, params)
    return graph_score * discount * decay


def replayed_sort_key(row: ReplayRow, params: ReplayParams) -> tuple[bool, float, float]:
    """The ``build_results`` ordering: semantic-first, RRF as tiebreak."""
    sim = replayed_similarity(row, params)
    return (sim is not None, sim if sim is not None else _SORT_NONE_SIM, row.ranking_score)


# --------------------------------------------------------------------------- #
# IPS position propensities                                                    #
# --------------------------------------------------------------------------- #


def estimate_position_propensities(
    rows: Iterable[ReplayRow], *, smoothing: float = 1.0
) -> dict[PropensityKey, float]:
    """Label coverage per (injection_group, injection_position), smoothed.

    Approximates the examination propensity P(labeled | injected at slot).
    Unlabeled injected rows count in denominators — that is the entire reason
    the replay loader returns them. Rows without an ``injection_position``
    (shouldn't exist for injected outcomes) are ignored.
    """
    injected: dict[PropensityKey, int] = {}
    labeled: dict[PropensityKey, int] = {}
    for row in rows:
        if row.injection_position is None:
            continue
        key = (row.injection_group, row.injection_position)
        injected[key] = injected.get(key, 0) + 1
        if row.judge_useful is not None:
            labeled[key] = labeled.get(key, 0) + 1
    return {
        key: (labeled.get(key, 0) + smoothing) / (count + 2.0 * smoothing)
        for key, count in injected.items()
    }


def ips_weight(
    row: ReplayRow,
    propensities: Mapping[PropensityKey, float],
    *,
    clip: float = 10.0,
) -> float:
    """Clipped inverse-propensity weight for one labeled row."""
    if row.injection_position is None:
        return 1.0
    propensity = propensities.get((row.injection_group, row.injection_position))
    if propensity is None or propensity <= 0.0:
        return clip
    return min(1.0 / propensity, clip)


# --------------------------------------------------------------------------- #
# Pairwise IPS objective                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PairwiseEvalResult:
    """Request-normalized ordering accuracy over labeled preference pairs."""

    pair_count: int
    mixed_request_count: int
    weighted_pair_count: float
    accuracy: float
    per_project: dict[str, float]


def evaluate_pairwise(
    rows: Sequence[ReplayRow],
    params_for_project: Mapping[str | None, ReplayParams],
    propensities: Mapping[PropensityKey, float],
    *,
    default_params: ReplayParams,
    clip: float = 10.0,
    weighting_mode: WeightingMode = "full",
) -> PairwiseEvalResult:
    """Score (useful, not-useful) pairs within each request under replay.

    A pair is correct when the useful row sorts strictly above the not-useful
    row under the ``build_results`` key; exact key ties earn half credit.
    Every mixed request has total weight 1. Full-candidate cohorts weight each
    pair uniformly; injected cohorts preserve relative positive-row IPS weights
    within that request. Unlabeled rows never form preference pairs.
    """
    if weighting_mode not in ("full", "injected"):
        raise ValueError(f"unsupported weighting_mode: {weighting_mode}")

    by_request: dict[str, list[ReplayRow]] = {}
    for row in rows:
        by_request.setdefault(row.recall_request_id, []).append(row)

    pair_count = 0
    mixed_request_count = 0
    weighted_total = 0.0
    weighted_correct = 0.0
    project_totals: dict[str, float] = {}
    project_correct: dict[str, float] = {}

    for request_rows in by_request.values():
        project_id = request_rows[0].project_id
        params = params_for_project.get(project_id, default_params)
        keys = {row.memory_id: replayed_sort_key(row, params) for row in request_rows}
        positives = [r for r in request_rows if r.judge_useful is True]
        negatives = [r for r in request_rows if r.judge_useful is False]
        if not positives or not negatives:
            continue

        mixed_request_count += 1
        if weighting_mode == "full":
            positive_weights = [1.0] * len(positives)
        else:
            positive_weights = [ips_weight(pos, propensities, clip=clip) for pos in positives]
        request_denominator = len(negatives) * sum(positive_weights)

        bucket = project_id or ""
        for pos, positive_weight in zip(positives, positive_weights, strict=True):
            pair_weight = positive_weight / request_denominator
            for neg in negatives:
                pair_count += 1
                credit = _pair_credit(keys[pos.memory_id], keys[neg.memory_id])
                weighted_correct += pair_weight * credit
                project_correct[bucket] = project_correct.get(bucket, 0.0) + pair_weight * credit
        weighted_total += 1.0
        project_totals[bucket] = project_totals.get(bucket, 0.0) + 1.0

    accuracy = weighted_correct / weighted_total if weighted_total > 0 else 0.0
    per_project = {
        project: project_correct[project] / total
        for project, total in project_totals.items()
        if total > 0
    }
    return PairwiseEvalResult(
        pair_count=pair_count,
        mixed_request_count=mixed_request_count,
        weighted_pair_count=weighted_total,
        accuracy=accuracy,
        per_project=per_project,
    )


def _pair_credit(positive_key: tuple[Any, ...], negative_key: tuple[Any, ...]) -> float:
    """Ordering credit for one preference pair; an exact tie splits it."""
    if positive_key > negative_key:
        return 1.0
    if positive_key == negative_key:
        return 0.5
    return 0.0


# --------------------------------------------------------------------------- #
# Per-project split + partial-pooled fit                                       #
# --------------------------------------------------------------------------- #


def split_request_ids_per_project(
    requests: Sequence[tuple[str | None, str]],
    *,
    eval_stride: int = 2,
    split_version: str = REQUEST_SPLIT_VERSION,
) -> tuple[set[str], set[str]]:
    """Return deterministic train/holdout request IDs within each project."""
    if eval_stride < 2:
        raise ValueError(f"eval_stride must be >= 2, got {eval_stride}")
    if not split_version.strip():
        raise ValueError("split_version must be non-empty")
    requests_by_project: dict[str | None, set[str]] = {}
    for project_id, request_id in requests:
        requests_by_project.setdefault(project_id, set()).add(request_id)

    eval_requests: set[str] = set()
    all_requests: set[str] = set()
    for request_ids in requests_by_project.values():
        all_requests.update(request_ids)
        seeded_request_ids = sorted(
            request_ids,
            key=lambda request_id: (
                sha256(f"{split_version}\0{request_id}".encode()).digest(),
                request_id,
            ),
        )
        for index, request_id in enumerate(seeded_request_ids):
            if index % eval_stride == eval_stride - 1:
                eval_requests.add(request_id)
    return all_requests - eval_requests, eval_requests


def split_requests_per_project(
    rows: Sequence[ReplayRow],
    *,
    eval_stride: int = 2,
    split_version: str = REQUEST_SPLIT_VERSION,
) -> tuple[list[ReplayRow], list[ReplayRow]]:
    """Deterministic train/eval split of requests *within* each project.

    Request IDs seed a versioned hash ordering within each project; every
    ``eval_stride``-th request goes to holdout. Input ordering cannot affect
    the frozen partition.
    """
    _train_requests, eval_requests = split_request_ids_per_project(
        [(row.project_id, row.recall_request_id) for row in rows],
        eval_stride=eval_stride,
        split_version=split_version,
    )

    train = [row for row in rows if row.recall_request_id not in eval_requests]
    evaluation = [row for row in rows if row.recall_request_id in eval_requests]
    return train, evaluation


# --------------------------------------------------------------------------- #
# Post-retrieval candidate-filter replay (plan 4.2)                            #
# --------------------------------------------------------------------------- #

CANDIDATE_FILTER_REPLAY_VERSION = "recall-candidate-filter-replay-v1"

DIGEST_CONDITIONED_EVALUATION_NOTE = (
    "This replay is no-digest by construction, and every number in it must be "
    "read that way. A v1 shadow snapshot stores only the scrubbed query text "
    "and the candidate excerpts presented to the judge; it stores neither the "
    "conversation digest nor the assistant response. A digest-conditioned "
    "candidate filter therefore cannot be replayed against v1 labels at all — "
    "not approximated, not bounded — because the inputs it would condition on "
    "were never captured. Evaluating one requires v2 data: a cohort whose "
    "query_construction_version fence post-dates the digest-enrichment cutover."
)

# Tokens shorter than this carry no topical signal in a query-coverage score.
_MIN_TOKEN_CHARS = 3
_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+")
# Function words carry no topical signal, so they must not inflate query coverage.
_QUERY_STOPWORDS = frozenset(
    "about after again against all already also and any are because been before being "
    "both but can cannot could did does doing done down each even every for from get gets "
    "getting had has have having her here him his how into its just like make makes many "
    "may might more most much must need needs not now off once one only our out over own "
    "same seem seems shall she should since some such than that the their them then there "
    "these they this those through too under until very was were what when where which "
    "while who why will with would you your".split()
)


@dataclass(frozen=True)
class CandidateReplayRow:
    """One retrieved candidate reduced to what a v1 snapshot actually stores.

    ``query_text`` and ``excerpt`` are the *only* textual inputs a replayed
    candidate filter may read — they are exactly the two things the shadow
    judge saw. ``similarity`` is the logged blended score the shipped
    static-constant selection ranks and thresholds on.
    """

    recall_request_id: str
    memory_id: str
    project_id: str | None
    rank: int
    query_text: str
    excerpt: str
    similarity: float | None
    judge_useful: bool | None


@dataclass(frozen=True)
class CandidateFilterParams:
    """Tunables of the replayed post-retrieval filter."""

    min_score: float = 0.34
    max_selected: int = 3

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_score <= 1.0:
            raise ValueError(f"min_score must be in [0, 1], got {self.min_score}")
        if self.max_selected < 1:
            raise ValueError(f"max_selected must be positive, got {self.max_selected}")


def _content_tokens(text: str) -> set[str]:
    return {
        token
        for token in _TOKEN_PATTERN.findall(text.lower())
        if len(token) >= _MIN_TOKEN_CHARS and token not in _QUERY_STOPWORDS
    }


def candidate_filter_score(query_text: str, excerpt: str) -> float:
    """Share of the query's content tokens the excerpt covers, in [0, 1].

    Coverage rather than symmetric overlap: a long memory should not be
    penalized for saying more than the query asked, and a short memory should
    not score well merely for being short. A query with no content tokens
    scores 0 — the filter has nothing to match on and abstaining is correct.
    """
    query_tokens = _content_tokens(query_text)
    if not query_tokens:
        return 0.0
    return len(query_tokens & _content_tokens(excerpt)) / len(query_tokens)


def candidate_replay_rows_from_signal_rows(
    rows: Iterable[Mapping[str, Any]],
) -> list[CandidateReplayRow]:
    """Adapt shadow replay rows, dropping any whose snapshot lacks an excerpt.

    The excerpt lives in the request's ``presented`` block keyed by memory id;
    a row whose memory is absent from that block cannot be scored by a
    text-only filter and is dropped rather than scored against empty text.
    """
    adapted: list[CandidateReplayRow] = []
    for row in rows:
        memory_id = str(row.get("memory_id") or "")
        excerpt = _presented_excerpt(row.get("presented"), memory_id)
        query_text = row.get("query_text")
        if excerpt is None or not isinstance(query_text, str):
            continue
        project_id = row.get("project_id")
        judge_useful = row.get("judge_useful")
        adapted.append(
            CandidateReplayRow(
                recall_request_id=str(row["recall_request_id"]),
                memory_id=memory_id,
                project_id=str(project_id) if project_id is not None else None,
                rank=int(row["rank"]),
                query_text=query_text,
                excerpt=excerpt,
                similarity=_float_or_none(row.get("similarity")),
                judge_useful=judge_useful if isinstance(judge_useful, bool) else None,
            )
        )
    return adapted


def _presented_excerpt(presented: Any, memory_id: str) -> str | None:
    if not isinstance(presented, Sequence) or isinstance(presented, str | bytes):
        return None
    for item in presented:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("memory_id") or "") != memory_id:
            continue
        excerpt = item.get("excerpt")
        return excerpt if isinstance(excerpt, str) else None
    return None


Selection = list[tuple[CandidateReplayRow, float]]


def select_by_candidate_filter(
    rows: Sequence[CandidateReplayRow], params: CandidateFilterParams
) -> Selection:
    """Rank one request's candidates by query coverage and admit 0..max."""
    scored = [(row, candidate_filter_score(row.query_text, row.excerpt)) for row in rows]
    admitted = [(row, score) for row, score in scored if score >= params.min_score]
    admitted.sort(key=lambda item: (-item[1], item[0].rank, item[0].memory_id))
    return admitted[: params.max_selected]


def select_by_static_constants(
    rows: Sequence[CandidateReplayRow], *, min_similarity: float, max_selected: int
) -> Selection:
    """Replay the shipped selection: similarity floor, then the rank cap.

    A candidate with no finite similarity is dropped rather than admitted,
    matching ``selection_min_score`` semantics on the live path.
    """
    admitted = [
        (row, row.similarity)
        for row in rows
        if row.similarity is not None and row.similarity >= min_similarity
    ]
    admitted.sort(key=lambda item: (-item[1], item[0].rank, item[0].memory_id))
    return admitted[:max_selected]


@dataclass(frozen=True)
class ArmMetrics:
    """Request-level selection quality for one arm of the replay.

    ``pairwise_accuracy`` carries ``pairwise_requests`` as its own denominator
    precisely so it is never read as a whole-population number: it scores only
    the requests where this arm selected both a useful and a not-useful
    candidate, which a heavily abstaining arm can make vanishingly small.
    """

    arm: str
    selection_threshold: float
    requests_evaluated: int
    abstention_rate: float
    abstain_correct: float
    abstain_regret: float
    mean_selected: float
    pairwise_accuracy: float
    pairwise_requests: int

    def to_record(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "selection_threshold": self.selection_threshold,
            "requests_evaluated": self.requests_evaluated,
            "abstention_rate": self.abstention_rate,
            "abstain_correct": self.abstain_correct,
            "abstain_regret": self.abstain_regret,
            "mean_selected": self.mean_selected,
            "pairwise_accuracy": self.pairwise_accuracy,
            "pairwise_requests": self.pairwise_requests,
        }


def evaluate_candidate_selection(
    arm: str,
    requests: Mapping[str, Sequence[CandidateReplayRow]],
    selections: Mapping[str, Selection],
    *,
    selection_threshold: float,
) -> ArmMetrics:
    """Score one arm's selections request-by-request against v1 labels."""
    evaluated = len(requests)
    if evaluated == 0:
        return ArmMetrics(
            arm=arm,
            selection_threshold=selection_threshold,
            requests_evaluated=0,
            abstention_rate=0.0,
            abstain_correct=0.0,
            abstain_regret=0.0,
            mean_selected=0.0,
            pairwise_accuracy=0.0,
            pairwise_requests=0,
        )

    abstentions = 0
    regretted = 0
    selected_total = 0
    pairwise_requests = 0
    pairwise_correct = 0.0

    for request_id, candidates in requests.items():
        selection = selections.get(request_id, [])
        selected_total += len(selection)
        if not selection:
            abstentions += 1
            if any(row.judge_useful is True for row in candidates):
                regretted += 1
            continue
        positives = [(row, score) for row, score in selection if row.judge_useful is True]
        negatives = [(row, score) for row, score in selection if row.judge_useful is False]
        if not positives or not negatives:
            continue
        pairwise_requests += 1
        request_credit = 0.0
        for _positive, positive_score in positives:
            for _negative, negative_score in negatives:
                request_credit += _pair_credit((positive_score,), (negative_score,))
        pairwise_correct += request_credit / (len(positives) * len(negatives))

    return ArmMetrics(
        arm=arm,
        selection_threshold=selection_threshold,
        requests_evaluated=evaluated,
        abstention_rate=abstentions / evaluated,
        abstain_correct=(abstentions - regretted) / abstentions if abstentions else 0.0,
        abstain_regret=regretted / abstentions if abstentions else 0.0,
        mean_selected=selected_total / evaluated,
        pairwise_accuracy=pairwise_correct / pairwise_requests if pairwise_requests else 0.0,
        pairwise_requests=pairwise_requests,
    )


@dataclass(frozen=True)
class CandidateFilterReplayReport:
    """One no-digest candidate-filter replay against a fenced v1 cohort."""

    cohort_identity: dict[str, Any]
    filter_params: CandidateFilterParams
    rows_total: int
    rows_scored: int
    requests_total: int
    requests_evaluated: int
    requests_skipped_unlabeled: int
    mean_selected_match_tolerance: float
    candidate_filter: ArmMetrics
    static_constants: ArmMetrics
    static_constants_matched: ArmMetrics | None

    def to_record(self) -> dict[str, Any]:
        matched = self.static_constants_matched
        return {
            "report_version": CANDIDATE_FILTER_REPLAY_VERSION,
            "cohort_identity": dict(self.cohort_identity),
            "filter_params": {
                "min_score": self.filter_params.min_score,
                "max_selected": self.filter_params.max_selected,
            },
            "rows_total": self.rows_total,
            "rows_scored": self.rows_scored,
            "requests_total": self.requests_total,
            "requests_evaluated": self.requests_evaluated,
            "requests_skipped_unlabeled": self.requests_skipped_unlabeled,
            "mean_selected_match_tolerance": self.mean_selected_match_tolerance,
            "arms": {
                "candidate_filter": self.candidate_filter.to_record(),
                "static_constants": self.static_constants.to_record(),
                "static_constants_matched": matched.to_record() if matched else None,
            },
            "digest_conditioned_evaluation": DIGEST_CONDITIONED_EVALUATION_NOTE,
        }


def _mean_selected_at(
    requests: Mapping[str, Sequence[CandidateReplayRow]],
    *,
    min_similarity: float,
    max_selected: int,
) -> float:
    total = sum(
        len(
            select_by_static_constants(
                rows, min_similarity=min_similarity, max_selected=max_selected
            )
        )
        for rows in requests.values()
    )
    return total / len(requests)


def _match_static_threshold(
    requests: Mapping[str, Sequence[CandidateReplayRow]],
    *,
    target: float,
    max_selected: int,
    tolerance: float,
) -> float | None:
    """Find the similarity floor whose mean selected count matches ``target``.

    Mean selected count is non-increasing in the floor, so a binary search over
    the observed similarity values finds the closest achievable match. Returns
    ``None`` when even the closest floor misses by more than ``tolerance`` —
    the arms simply cannot be matched on that cohort.
    """
    thresholds = [0.0] + sorted(
        {row.similarity for rows in requests.values() for row in rows if row.similarity is not None}
    )
    low, high = 0, len(thresholds) - 1
    best = 0
    while low <= high:
        mid = (low + high) // 2
        mean = _mean_selected_at(
            requests, min_similarity=thresholds[mid], max_selected=max_selected
        )
        if mean >= target:
            best = mid
            low = mid + 1
        else:
            high = mid - 1

    neighbours = {best, min(best + 1, len(thresholds) - 1)}
    chosen = min(
        neighbours,
        key=lambda index: (
            abs(
                _mean_selected_at(
                    requests, min_similarity=thresholds[index], max_selected=max_selected
                )
                - target
            ),
            thresholds[index],
        ),
    )
    achieved = _mean_selected_at(
        requests, min_similarity=thresholds[chosen], max_selected=max_selected
    )
    if abs(achieved - target) > tolerance:
        return None
    return thresholds[chosen]


def replay_candidate_filter(
    signal_rows: Iterable[Mapping[str, Any]],
    *,
    cohort_identity: Mapping[str, Any],
    static_min_similarity: float,
    params: CandidateFilterParams | None = None,
    mean_selected_match_tolerance: float = 0.05,
) -> CandidateFilterReplayReport:
    """Replay a no-digest candidate filter against static constants on v1 labels.

    Both arms see the same requests and are scored by the same request-level
    metrics. Requests carrying no label at all are excluded outright: silence
    on a request whose candidates were never judged is neither a right silence
    nor a missed injection, and counting it as either would flatter one arm.

    The cohort identity must carry the ``query_construction_version`` fence, so
    a report can never be written without recording which query-construction
    era produced the rows it scored.
    """
    filter_params = params or CandidateFilterParams()
    if mean_selected_match_tolerance < 0.0:
        raise ValueError("mean_selected_match_tolerance must be non-negative")
    fence = cohort_identity.get("query_construction_version")
    if not isinstance(fence, str) or not fence.strip():
        raise ValueError("cohort_identity must carry a non-empty query_construction_version fence")

    materialized = list(signal_rows)
    rows = candidate_replay_rows_from_signal_rows(materialized)
    by_request: dict[str, list[CandidateReplayRow]] = {}
    for row in rows:
        by_request.setdefault(row.recall_request_id, []).append(row)

    labeled = {
        request_id: candidates
        for request_id, candidates in by_request.items()
        if any(row.judge_useful is not None for row in candidates)
    }

    filter_selections = {
        request_id: select_by_candidate_filter(candidates, filter_params)
        for request_id, candidates in labeled.items()
    }
    static_selections = {
        request_id: select_by_static_constants(
            candidates,
            min_similarity=static_min_similarity,
            max_selected=filter_params.max_selected,
        )
        for request_id, candidates in labeled.items()
    }

    filter_metrics = evaluate_candidate_selection(
        "candidate_filter",
        labeled,
        filter_selections,
        selection_threshold=filter_params.min_score,
    )
    static_metrics = evaluate_candidate_selection(
        "static_constants",
        labeled,
        static_selections,
        selection_threshold=static_min_similarity,
    )

    matched_metrics: ArmMetrics | None = None
    if labeled:
        matched_threshold = _match_static_threshold(
            labeled,
            target=filter_metrics.mean_selected,
            max_selected=filter_params.max_selected,
            tolerance=mean_selected_match_tolerance,
        )
        if matched_threshold is not None:
            matched_metrics = evaluate_candidate_selection(
                "static_constants_matched",
                labeled,
                {
                    request_id: select_by_static_constants(
                        candidates,
                        min_similarity=matched_threshold,
                        max_selected=filter_params.max_selected,
                    )
                    for request_id, candidates in labeled.items()
                },
                selection_threshold=matched_threshold,
            )

    return CandidateFilterReplayReport(
        cohort_identity=dict(cohort_identity),
        filter_params=filter_params,
        rows_total=len(materialized),
        rows_scored=len(rows),
        requests_total=len(by_request),
        requests_evaluated=len(labeled),
        requests_skipped_unlabeled=len(by_request) - len(labeled),
        mean_selected_match_tolerance=mean_selected_match_tolerance,
        candidate_filter=filter_metrics,
        static_constants=static_metrics,
        static_constants_matched=matched_metrics,
    )
