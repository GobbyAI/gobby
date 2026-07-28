"""Proof-based classification for finalized plan-review rounds."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from gobby.plans.review_evidence_models import (
    PlanReviewEvidence,
    ReviewEvidenceError,
    validate_round_result,
)
from gobby.plans.review_findings import validate_plan_review_findings
from gobby.plans.review_ledger import validate_quality_ledger

PlanLessonType = Literal["reviewer-miss", "fixer-induced-defect", "no-fix-policy"]
PlanLessonDecision = Literal["confirmed", "no-fix-policy"]
PlanLessonSource = Literal["finding", "quality_ledger"]
_CLASS_ORDER: dict[PlanLessonType, int] = {
    "reviewer-miss": 0,
    "fixer-induced-defect": 1,
    "no-fix-policy": 2,
}


@dataclass(frozen=True)
class PlanReviewLessonCandidate:
    """One independently mintable lesson class with its server-owned proof."""

    lesson_type: PlanLessonType
    evidence_id: str
    round_number: int
    finding: dict[str, object]
    proof: dict[str, object]
    metric: int
    decision: PlanLessonDecision = "confirmed"
    source: PlanLessonSource = "finding"


def classify_plan_review_rounds(
    rows: Sequence[PlanReviewEvidence],
    *,
    task_id: str,
    stage: str,
) -> list[PlanReviewLessonCandidate]:
    """Classify blocking findings within one finalized task/stage lineage."""
    lineage = _lineage_rows(rows, task_id=task_id, stage=stage)
    if not lineage:
        return []
    canonical_findings = {row.evidence_id: _validated_findings(row) for row in lineage}
    candidates: list[PlanReviewLessonCandidate] = []
    for index, row in enumerate(lineage):
        findings = canonical_findings[row.evidence_id]
        if findings is None:
            continue
        prior = lineage[:index]
        for finding in findings:
            if finding.get("severity") != "blocking":
                continue
            reviewer = _reviewer_miss_candidate(row, prior, finding)
            if reviewer is not None:
                candidates.append(reviewer)
            fixer = _fixer_induced_candidate(
                row,
                prior,
                finding,
                canonical_findings=canonical_findings,
            )
            if fixer is not None:
                candidates.append(fixer)
    candidates.extend(_no_fix_policy_candidates(lineage[-1]))
    return candidates


def _no_fix_policy_candidates(row: PlanReviewEvidence) -> list[PlanReviewLessonCandidate]:
    candidates: list[PlanReviewLessonCandidate] = []
    for entry in validate_quality_ledger(row.quality_ledger or []):
        rounds_carried = entry["rounds_carried"]
        if (
            entry["kind"] != "finding"
            or entry["stale"] is not False
            or not isinstance(rounds_carried, int)
            or rounds_carried < 3
        ):
            continue
        ledger_entry_id = str(entry["ledger_entry_id"])
        finding = {
            key: value
            for key, value in entry.items()
            if key
            in {
                "check_key",
                "category",
                "severity",
                "location",
                "description",
                "minimal_repair",
                "prevention",
                "principle",
                "root_cause",
            }
        }
        finding["finding_id"] = ledger_entry_id
        finding["section_id"] = cast(list[str], entry["source_section_ids"])[0]
        candidates.append(
            PlanReviewLessonCandidate(
                lesson_type="no-fix-policy",
                evidence_id=row.evidence_id,
                round_number=row.round_number,
                finding=finding,
                proof={
                    "source": "quality_ledger",
                    "ledger_entry_id": ledger_entry_id,
                    "rounds_carried": rounds_carried,
                },
                metric=rounds_carried,
                decision="no-fix-policy",
                source="quality_ledger",
            )
        )
    return candidates


def select_plan_review_candidates(
    candidates: Sequence[PlanReviewLessonCandidate],
    *,
    limit: int = 5,
) -> list[PlanReviewLessonCandidate]:
    """Apply the deterministic cap while reserving one slot per present class."""
    capped_limit = max(0, min(limit, 5))
    if capped_limit == 0:
        return []
    ranked = sorted(candidates, key=_candidate_sort_key)
    present = [
        lesson_type
        for lesson_type in _CLASS_ORDER
        if any(candidate.lesson_type == lesson_type for candidate in ranked)
    ]
    selected: list[PlanReviewLessonCandidate] = []
    for lesson_type in present[:capped_limit]:
        selected.append(
            next(candidate for candidate in ranked if candidate.lesson_type == lesson_type)
        )
    if len(selected) == capped_limit:
        return selected
    selected_ids = {id(candidate) for candidate in selected}
    selected.extend(candidate for candidate in ranked if id(candidate) not in selected_ids)
    return selected[:capped_limit]


def _lineage_rows(
    rows: Sequence[PlanReviewEvidence],
    *,
    task_id: str,
    stage: str,
) -> list[PlanReviewEvidence]:
    eligible = sorted(
        (
            row
            for row in rows
            if row.task_id == task_id
            and row.stage == stage
            and row.finalized_at is not None
            and row.expired_at is None
            and row.round_result is not None
        ),
        key=lambda row: (row.round_number, row.created_at, row.evidence_id),
    )
    if not eligible:
        return []
    project_id = eligible[0].project_id
    plan_path = eligible[0].plan_path
    return [row for row in eligible if row.project_id == project_id and row.plan_path == plan_path]


def _validated_findings(
    row: PlanReviewEvidence,
) -> list[dict[str, object]] | None:
    if row.round_result is None:
        return None
    try:
        payload = validate_round_result(row.round_result)
        raw_findings = payload["findings"]
        if not isinstance(raw_findings, list):
            return None
        mappings = [finding for finding in raw_findings if isinstance(finding, dict)]
        if len(mappings) != len(raw_findings):
            return None
        return validate_plan_review_findings(mappings, evidence=row)
    except (ReviewEvidenceError, TypeError, ValueError):
        return None


def _reviewer_miss_candidate(
    row: PlanReviewEvidence,
    prior: Sequence[PlanReviewEvidence],
    finding: dict[str, object],
) -> PlanReviewLessonCandidate | None:
    section_ids = _nonempty_string_list(finding.get("participating_section_ids"))
    if section_ids is None:
        return None
    current_hashes = _section_hashes(row)
    matching = [
        earlier
        for earlier in prior
        if earlier.round_number < row.round_number
        and all(
            section_id in current_hashes
            and _section_hashes(earlier).get(section_id) == current_hashes[section_id]
            for section_id in section_ids
        )
    ]
    if not matching:
        return None
    earliest = min(matching, key=lambda item: (item.round_number, item.created_at))
    rounds_missed = row.round_number - earliest.round_number
    return PlanReviewLessonCandidate(
        lesson_type="reviewer-miss",
        evidence_id=row.evidence_id,
        round_number=row.round_number,
        finding=finding,
        proof={
            "participating_section_ids": section_ids,
            "earliest_reviewed_round": earliest.round_number,
            "rounds_missed": rounds_missed,
            "section_hashes": {
                section_id: current_hashes[section_id] for section_id in section_ids
            },
        },
        metric=rounds_missed,
    )


def _fixer_induced_candidate(
    row: PlanReviewEvidence,
    prior: Sequence[PlanReviewEvidence],
    finding: dict[str, object],
    *,
    canonical_findings: dict[str, list[dict[str, object]] | None],
) -> PlanReviewLessonCandidate | None:
    section_ids = _nonempty_string_list(finding.get("causal_section_ids"))
    causal_finding_id = finding.get("causal_finding_id")
    introduced_in_round = finding.get("introduced_in_round")
    if (
        section_ids is None
        or not isinstance(causal_finding_id, str)
        or not causal_finding_id
        or not isinstance(introduced_in_round, int)
        or isinstance(introduced_in_round, bool)
        or introduced_in_round >= row.round_number
    ):
        return None
    introduction = next(
        (
            earlier
            for earlier in prior
            if earlier.round_number == introduced_in_round
            and any(
                prior_finding.get("finding_id") == causal_finding_id
                for prior_finding in canonical_findings.get(earlier.evidence_id) or []
            )
        ),
        None,
    )
    if introduction is None:
        return None
    current_hashes = _section_hashes(row)
    introduction_hashes = _section_hashes(introduction)
    if not all(
        section_id in current_hashes
        and section_id in introduction_hashes
        and current_hashes[section_id] != introduction_hashes[section_id]
        for section_id in section_ids
    ):
        return None
    causal_occurrences = sum(
        1
        for earlier in prior
        for prior_finding in canonical_findings.get(earlier.evidence_id) or []
        if prior_finding.get("finding_id") == causal_finding_id
    )
    return PlanReviewLessonCandidate(
        lesson_type="fixer-induced-defect",
        evidence_id=row.evidence_id,
        round_number=row.round_number,
        finding=finding,
        proof={
            "causal_section_ids": section_ids,
            "causal_finding_id": causal_finding_id,
            "introduced_in_round": introduced_in_round,
            "causal_occurrences": causal_occurrences,
            "before_hashes": {
                section_id: introduction_hashes[section_id] for section_id in section_ids
            },
            "after_hashes": {section_id: current_hashes[section_id] for section_id in section_ids},
        },
        metric=causal_occurrences,
    )


def _section_hashes(row: PlanReviewEvidence) -> dict[str, str]:
    return {section.section_id: section.section_hash for section in row.section_manifest}


def _nonempty_string_list(value: object) -> list[str] | None:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
    ):
        return None
    return value


def _candidate_sort_key(candidate: PlanReviewLessonCandidate) -> tuple[object, ...]:
    return (
        -candidate.metric,
        _CLASS_ORDER[candidate.lesson_type],
        str(candidate.finding.get("check_key", "")),
        str(candidate.finding.get("finding_id", "")),
        candidate.evidence_id,
    )
