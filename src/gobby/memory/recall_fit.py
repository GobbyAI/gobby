"""Offline fit/eval over logged recall-signal rows (#17197, epic #17099).

This module generalizes the offline recall benchmark harness to real labeled
data. It consumes request-aligned per-hit feature rows from the promoted hub
tables and replays the FULL ranking path — counterfactual scores ordered by the
policy live ``build_results`` orders on — without re-running retrieval. The
replay model itself (rows, parameters, algebra, ordering) lives in
``recall_replay``.

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
  ``recall_fit_shrinkage``; this module owns the metrics both it and the
  candidate-filter replay score against.

Selection replay reproduces two admission axes, because live selection has
had two since #20873. A graph-expander find -- a memory the graph surfaced and
the vector leg's own window missed, identified by a ``graph_score`` on a
``search_via`` without ``semantic`` -- is admitted on that raw entity-match
confidence; every other candidate is admitted on its undecayed cosine. Both
floors are parameters of the replayed arm, since the confidence floor is
provisional and the Phase 4 refit owns it (#20879).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal

from gobby.memory.recall_replay import (
    ReplayParams,
    ReplayRow,
    float_or_none,
    replayed_order,
    replayed_scores,
)
from gobby.memory.services._search_constants import _GRAPH_CONFIDENCE_SELECTION_FLOOR

# Propensity keys are (injection_group, injection_position); position is the
# rendered ordinal within the injection block, per the label contract §5.
PropensityKey = tuple[str | None, int]
WeightingMode = Literal["full", "injected"]

REQUEST_SPLIT_VERSION = "recall-request-hash-split-v1"
# v2 (#22491): pairs are credited on the live interleaved order, where v1 compared a
# decayed-similarity key live search stopped ordering on in #21010.
PAIRWISE_EVALUATOR_VERSION = "recall-request-normalized-pairwise-v2"
AUDIT_SAMPLER_VERSION = "recall-training-request-sampler-v1"


def evaluation_protocol_identity(*, split_version: str = REQUEST_SPLIT_VERSION) -> dict[str, str]:
    """Version fields that bind fitting, audit sampling, and holdout evaluation."""
    return {
        "split_version": split_version,
        "evaluator_version": PAIRWISE_EVALUATOR_VERSION,
        "audit_sampler_version": AUDIT_SAMPLER_VERSION,
    }


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

    A pair is correct when the useful row lands above the not-useful row in the
    request's replayed live order; two rows with equal replayed scores have no
    order of their own and earn half credit. Every mixed request has total
    weight 1. Full-candidate cohorts weight each pair uniformly; injected
    cohorts preserve relative positive-row IPS weights within that request.
    Unlabeled rows never form preference pairs.
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
        scores = {row.memory_id: replayed_scores(row, params) for row in request_rows}
        standing = {
            row.memory_id: -position
            for position, row in enumerate(replayed_order(request_rows, params))
        }
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
                if scores[pos.memory_id] == scores[neg.memory_id]:
                    credit = 0.5
                else:
                    credit = 1.0 if standing[pos.memory_id] > standing[neg.memory_id] else 0.0
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

# v2: the static arm gates a graph-expander find on its entity-match confidence
# rather than its cosine, matching live selection since #20873 (#20879). A v1
# report scored a materially different arm and must not be read as comparable.
CANDIDATE_FILTER_REPLAY_VERSION = "recall-candidate-filter-replay-v2"

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
    judge saw. ``similarity`` is the logged decayed score the shipped
    static-constant selection ranks on; dividing ``temporal_decay_factor`` back
    out of it gives the undecayed score that selection thresholds on (#20831).
    """

    recall_request_id: str
    memory_id: str
    project_id: str | None
    rank: int
    query_text: str
    excerpt: str
    similarity: float | None
    judge_useful: bool | None
    temporal_decay_factor: float | None = None
    search_via: str | None = None
    graph_score: float | None = None

    @property
    def undecayed_similarity(self) -> float | None:
        """``similarity`` with the age penalty divided back out, if it has one."""
        if self.similarity is None:
            return None
        decay = self.temporal_decay_factor
        if decay is None or decay <= 0.0:
            return self.similarity
        return self.similarity / decay

    @property
    def graph_confidence(self) -> float | None:
        """The entity-match confidence this row is admitted on, if it has one.

        A graph-expander find is a memory the graph surfaced and the vector
        leg's own window missed, and since #20873 that is what live selection
        judges on confidence rather than cosine. ``graph_score`` is logged for
        every graph-sourced hit including one the vector leg also returned, so
        ``search_via`` is what separates the two: live sets no confidence for a
        candidate already in the semantic window, because letting entity
        confidence rescue a sub-floor cosine there would widen the semantic
        axis under cover of the expander. ``None`` for every other row (#20879).
        """
        if self.graph_score is None:
            return None
        if "semantic" in (self.search_via or "").split("|"):
            return None
        return self.graph_score


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
        search_via = row.get("search_via")
        adapted.append(
            CandidateReplayRow(
                recall_request_id=str(row["recall_request_id"]),
                memory_id=memory_id,
                project_id=str(project_id) if project_id is not None else None,
                rank=int(row["rank"]),
                query_text=query_text,
                excerpt=excerpt,
                similarity=float_or_none(row.get("similarity")),
                judge_useful=judge_useful if isinstance(judge_useful, bool) else None,
                temporal_decay_factor=float_or_none(row.get("temporal_decay_factor")),
                search_via=str(search_via) if isinstance(search_via, str) else None,
                graph_score=float_or_none(row.get("graph_score")),
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
    rows: Sequence[CandidateReplayRow],
    *,
    min_similarity: float,
    max_selected: int,
    graph_confidence_min_score: float = _GRAPH_CONFIDENCE_SELECTION_FLOOR,
) -> Selection:
    """Replay the shipped selection: per-axis floor, then the rank cap.

    The cosine floor reads the undecayed score and the order reads the decayed
    one, matching ``selection_min_score`` semantics on the live path since
    #20831 -- the arm exists to model what actually ships, so an arm left on
    the old axis would have phase-4 fitting ratify a selection nothing
    performs. A candidate with no similarity is dropped rather than admitted.

    A graph-expander find is admitted on its entity-match confidence instead,
    which is the axis live selection has judged it on since #20873; its cosine
    only ranks it. ``graph_confidence_min_score`` defaults to the shipped floor
    and is a parameter because that constant is provisional and the Phase 4
    refit owns it -- fitting it needs an arm that can sweep it (#20879).
    """
    admitted: Selection = []
    for row in rows:
        if row.similarity is None:
            continue
        confidence = row.graph_confidence
        if confidence is not None:
            if confidence < graph_confidence_min_score:
                continue
        else:
            undecayed = row.undecayed_similarity
            if undecayed is None or undecayed < min_similarity:
                continue
        admitted.append((row, row.similarity))
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
    static_graph_confidence_min_score: float
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
            # The static arm gates two axes, and only the cosine one is carried
            # on ArmMetrics.selection_threshold. Recording the confidence floor
            # beside it is what lets a later reader tell which gate a report
            # ran under -- the 0.65 fossil #20771 unwound happened because a
            # constant's calibration context lived nowhere near it (#20879).
            "static_graph_confidence_min_score": self.static_graph_confidence_min_score,
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
    graph_confidence_min_score: float,
) -> float:
    total = sum(
        len(
            select_by_static_constants(
                rows,
                min_similarity=min_similarity,
                max_selected=max_selected,
                graph_confidence_min_score=graph_confidence_min_score,
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
    graph_confidence_min_score: float,
) -> float | None:
    """Find the cosine floor whose mean selected count matches ``target``.

    The grid is the set of undecayed similarities of the rows this floor
    actually gates, because those are the only values at which mean selected
    count changes. Searching the decayed values instead put every grid point
    below the breakpoints it was looking for: on an aged cohort the arm then
    reports the same mean at every threshold the grid can offer and the match
    fails outright (#20879). Graph-expander finds are excluded from the grid
    for the same reason -- the confidence floor decides them at every
    threshold, so their cosines are not breakpoints.

    Mean selected count is non-increasing in the floor, so a binary search over
    that grid finds the closest achievable match. Returns ``None`` when even
    the closest floor misses by more than ``tolerance`` -- the arms simply
    cannot be matched on that cohort.
    """

    def mean_at(threshold: float) -> float:
        return _mean_selected_at(
            requests,
            min_similarity=threshold,
            max_selected=max_selected,
            graph_confidence_min_score=graph_confidence_min_score,
        )

    thresholds = [0.0] + sorted(
        {
            undecayed
            for rows in requests.values()
            for row in rows
            if row.graph_confidence is None and (undecayed := row.undecayed_similarity) is not None
        }
    )
    low, high = 0, len(thresholds) - 1
    best = 0
    while low <= high:
        mid = (low + high) // 2
        if mean_at(thresholds[mid]) >= target:
            best = mid
            low = mid + 1
        else:
            high = mid - 1

    neighbours = {best, min(best + 1, len(thresholds) - 1)}
    chosen = min(
        neighbours,
        key=lambda index: (abs(mean_at(thresholds[index]) - target), thresholds[index]),
    )
    achieved = mean_at(thresholds[chosen])
    if abs(achieved - target) > tolerance:
        return None
    return thresholds[chosen]


def replay_candidate_filter(
    signal_rows: Iterable[Mapping[str, Any]],
    *,
    cohort_identity: Mapping[str, Any],
    static_min_similarity: float,
    static_graph_confidence_min_score: float = _GRAPH_CONFIDENCE_SELECTION_FLOOR,
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
            graph_confidence_min_score=static_graph_confidence_min_score,
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
            graph_confidence_min_score=static_graph_confidence_min_score,
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
                        graph_confidence_min_score=static_graph_confidence_min_score,
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
        static_graph_confidence_min_score=static_graph_confidence_min_score,
        candidate_filter=filter_metrics,
        static_constants=static_metrics,
        static_constants_matched=matched_metrics,
    )
