"""Frozen cohort identity and gate-reconstructed recall audit primitives."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import sqrt
from typing import TYPE_CHECKING, Any

from gobby.memory.recall_fit import (
    REQUEST_SPLIT_VERSION,
    WeightingMode,
    evaluation_protocol_identity,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from datetime import datetime

AUDIT_SAMPLE_REQUESTS = 50
AUDIT_MIN_AGREEMENT = 0.80
AUDIT_MIN_WILSON_LOWER_BOUND = 0.65
DECISION_SCHEMA_VERSION = "recall-ship-decision-v2"


def canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GateCohort:
    """Immutable cohort fences shared by audit, fitting, and holdout evaluation."""

    label_source: str
    candidate_scope: str
    judge_protocol_version: str
    weighting_regime_key: str
    judge_model_key: str
    judge_config_fingerprint: str
    data_cutoff: datetime
    completion_cutoff: datetime
    project_id: str | None = None
    weighting_mode: WeightingMode = "full"
    split_version: str = REQUEST_SPLIT_VERSION
    # The query-construction era this cohort is fenced to. ``None`` is the legacy
    # era — requests logged before the version was persisted at all — so absence
    # is a real value here and blankness is not.
    query_construction_version: str | None = None

    def __post_init__(self) -> None:
        required = {
            "label_source": self.label_source,
            "judge_protocol_version": self.judge_protocol_version,
            "weighting_regime_key": self.weighting_regime_key,
            "judge_model_key": self.judge_model_key,
            "judge_config_fingerprint": self.judge_config_fingerprint,
            "split_version": self.split_version,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(f"cohort fields must be nonblank: {', '.join(missing)}")
        if self.query_construction_version is not None and not (
            self.query_construction_version.strip()
        ):
            raise ValueError("query_construction_version must be nonblank or None")
        if self.candidate_scope not in {"full", "injected"}:
            raise ValueError("candidate_scope must be 'full' or 'injected'")
        if self.weighting_mode not in {"full", "injected"}:
            raise ValueError("weighting_mode must be 'full' or 'injected'")
        if self.data_cutoff.tzinfo is None or self.completion_cutoff.tzinfo is None:
            raise ValueError("data_cutoff and completion_cutoff must be timezone-aware")

    def identity(self) -> dict[str, Any]:
        """Canonical non-tunable cohort identity."""
        return {
            "label_source": self.label_source,
            "candidate_scope": self.candidate_scope,
            "judge_protocol_version": self.judge_protocol_version,
            "query_construction_version": self.query_construction_version,
            "weighting_regime_key": self.weighting_regime_key,
            "judge_model_key": self.judge_model_key,
            "judge_config_fingerprint": self.judge_config_fingerprint,
            "data_cutoff": self.data_cutoff.isoformat(),
            "completion_cutoff": self.completion_cutoff.isoformat(),
            "project_id": self.project_id,
            "weighting_mode": self.weighting_mode,
            **evaluation_protocol_identity(split_version=self.split_version),
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.identity())


@dataclass(frozen=True)
class ShipAuditTarget:
    request_id: str
    memory_id: str
    prompt_hash: str
    judge_useful: bool


@dataclass(frozen=True)
class ShipAuditSample:
    cohort_digest: str
    sample_digest: str
    targets: tuple[ShipAuditTarget, ...]


@dataclass(frozen=True)
class ShipAuditResult:
    """Gate-reconstructed human agreement over independent request units."""

    status: str
    cohort_digest: str
    sample_digest: str
    unit_count: int
    agreement: float | None = None
    wilson_lower_bound: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == "passed"


def build_ship_audit_sample(
    rows: Sequence[Mapping[str, Any]],
    *,
    cohort: GateCohort,
    train_request_ids: Collection[str],
) -> ShipAuditSample:
    """Build the deterministic one-target-per-training-request ship audit."""
    allowed_requests = set(train_request_ids)
    candidates: dict[str, dict[str, ShipAuditTarget]] = {}
    for row in rows:
        request_id = str(row.get("recall_request_id") or row.get("request_id") or "")
        if request_id not in allowed_requests:
            continue
        memory_id = str(row.get("memory_id") or "")
        prompt_hash = str(row.get("prompt_hash") or "")
        judge_useful = row.get("judge_useful")
        if not memory_id or not prompt_hash or not isinstance(judge_useful, bool):
            raise ValueError(
                f"audit candidate {request_id}/{memory_id} lacks prompt hash or bool verdict"
            )
        candidates.setdefault(request_id, {})[memory_id] = ShipAuditTarget(
            request_id=request_id,
            memory_id=memory_id,
            prompt_hash=prompt_hash,
            judge_useful=judge_useful,
        )

    cohort_digest = cohort.digest
    ordered_request_ids = sorted(
        candidates,
        key=lambda request_id: (
            hashlib.sha256(f"{cohort_digest}\0{request_id}".encode()).digest(),
            request_id,
        ),
    )[:AUDIT_SAMPLE_REQUESTS]
    targets = tuple(
        min(
            candidates[request_id].values(),
            key=lambda target: (
                hashlib.sha256(
                    f"{cohort_digest}\0{request_id}\0{target.memory_id}".encode()
                ).digest(),
                target.memory_id,
            ),
        )
        for request_id in ordered_request_ids
    )
    sample_digest = canonical_digest(
        [
            {
                "request_id": target.request_id,
                "memory_id": target.memory_id,
                "prompt_hash": target.prompt_hash,
            }
            for target in targets
        ]
    )
    return ShipAuditSample(
        cohort_digest=cohort_digest,
        sample_digest=sample_digest,
        targets=targets,
    )


def _wilson_lower_bound(successes: int, total: int) -> float:
    if total <= 0:
        return 0.0
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1.0 + z * z / total
    center = proportion + z * z / (2.0 * total)
    margin = z * sqrt(proportion * (1.0 - proportion) / total + z * z / (4.0 * total * total))
    return (center - margin) / denominator


def evaluate_ship_audit(
    sample: ShipAuditSample,
    verdicts: Sequence[Mapping[str, Any]],
) -> ShipAuditResult:
    """Validate exact-sample verdicts and recompute the ship statistic."""
    if len(sample.targets) != AUDIT_SAMPLE_REQUESTS:
        return ShipAuditResult(
            status="insufficient_training_sample",
            cohort_digest=sample.cohort_digest,
            sample_digest=sample.sample_digest,
            unit_count=len(sample.targets),
        )

    expected = {(target.request_id, target.memory_id): target for target in sample.targets}
    observed: dict[tuple[str, str], bool] = {}
    for row in verdicts:
        if row.get("cohort_digest") not in {None, sample.cohort_digest}:
            return _audit_failure("digest_mismatch", sample, len(observed))
        if row.get("sample_digest") not in {None, sample.sample_digest}:
            return _audit_failure("digest_mismatch", sample, len(observed))
        key = (str(row.get("request_id") or ""), str(row.get("memory_id") or ""))
        target = expected.get(key)
        if target is None:
            continue
        if str(row.get("prompt_hash") or "") != target.prompt_hash:
            return _audit_failure("stale_prompt_hash", sample, len(observed))
        human_verdict = row.get("human_verdict")
        if not isinstance(human_verdict, bool):
            return _audit_failure("invalid_verdict", sample, len(observed))
        if key in observed:
            return _audit_failure("duplicate_verdict", sample, len(observed))
        observed[key] = human_verdict

    if len(observed) != AUDIT_SAMPLE_REQUESTS:
        return _audit_failure("incomplete", sample, len(observed))
    agreements = sum(
        observed[(target.request_id, target.memory_id)] == target.judge_useful
        for target in sample.targets
    )
    agreement = agreements / AUDIT_SAMPLE_REQUESTS
    lower_bound = _wilson_lower_bound(agreements, AUDIT_SAMPLE_REQUESTS)
    status = (
        "passed"
        if agreement >= AUDIT_MIN_AGREEMENT and lower_bound >= AUDIT_MIN_WILSON_LOWER_BOUND
        else "below_threshold"
    )
    return ShipAuditResult(
        status=status,
        cohort_digest=sample.cohort_digest,
        sample_digest=sample.sample_digest,
        unit_count=AUDIT_SAMPLE_REQUESTS,
        agreement=agreement,
        wilson_lower_bound=lower_bound,
    )


def _audit_failure(status: str, sample: ShipAuditSample, unit_count: int) -> ShipAuditResult:
    return ShipAuditResult(
        status=status,
        cohort_digest=sample.cohort_digest,
        sample_digest=sample.sample_digest,
        unit_count=unit_count,
    )
