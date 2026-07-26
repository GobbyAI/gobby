"""Structured task-validation verdict normalization and messaging."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

from gobby.failure_categories import FailureCategory, classify_failure
from gobby.tasks.validation_models import Issue

_NULLISH_FAILURE_EVIDENCE = frozenset({"n/a", "none", "null"})
_VERDICT_STATUSES = frozenset({"valid", "invalid", "pending"})

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CriterionResult:
    """One criterion's evidence-backed semantic verdict."""

    criterion: str
    status: Literal["satisfied", "gap"]
    evidence_ids: list[str]
    explanation: str

    def to_dict(self) -> dict[str, object]:
        return {
            "criterion": self.criterion,
            "status": self.status,
            "evidence_ids": self.evidence_ids,
            "explanation": self.explanation,
        }


@dataclass
class ValidationResult:
    """Result of task validation with optional deterministic override provenance."""

    status: Literal["valid", "invalid", "pending", "error"]
    feedback: str | None = None
    blocking_reasons: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    diagnostics: list[dict[str, str]] = field(default_factory=list)
    verdict_override: dict[str, object] | None = None
    failure_category: FailureCategory | None = None
    criterion_results: list[CriterionResult] = field(default_factory=list)


def _coerce_blocking_reasons(value: object) -> list[str]:
    if isinstance(value, list):
        return [reason for item in value if (reason := str(item).strip())]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _coerce_issues(value: object) -> list[Issue]:
    """Parse optional validator issues without invalidating the verdict."""
    if value is None:
        return []
    if not isinstance(value, list):
        logger.warning("dropping non-list issues payload from validation verdict")
        return []

    issues: list[Issue] = []
    for index, item in enumerate(value):
        try:
            if not isinstance(item, dict):
                raise TypeError("issue must be an object")
            title = item.get("title")
            location = item.get("location")
            if not isinstance(title, str) or not title.strip():
                raise TypeError("issue title must be a non-empty string")
            if location is not None and not isinstance(location, str):
                raise TypeError("issue location must be a string or null")
            issues.append(Issue.from_dict(item))
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("dropping malformed validation issue at index %d: %s", index, exc)
    return issues


def filter_failure_evidence(raw: object) -> list[str]:
    """Return only non-empty string entries that attest a concrete current failure."""
    if not isinstance(raw, list):
        return []
    filtered: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        evidence = item.strip()
        if not evidence or evidence.casefold() in _NULLISH_FAILURE_EVIDENCE:
            continue
        filtered.append(evidence)
    return filtered


def is_contradictory_valid(status: object, evidence: Sequence[str]) -> bool:
    """Return whether a valid verdict attests that a current failure exists."""
    return status == "valid" and bool(evidence)


def demote_contradictory_valid(payload: Mapping[str, object]) -> dict[str, object]:
    """Normalize failure evidence and deterministically demote a contradictory pass."""
    normalized = dict(payload)
    evidence = filter_failure_evidence(payload.get("current_failure_evidence"))
    normalized["current_failure_evidence"] = evidence
    if not is_contradictory_valid(payload.get("status"), evidence):
        return normalized
    normalized["status"] = "invalid"
    normalized["blocking_reasons"] = evidence
    normalized["verdict_override"] = {
        "from": "valid",
        "to": "invalid",
        "reason": "current_failure_evidence",
        "evidence": evidence,
    }
    return normalized


def contradiction_rejection_message(payload: Mapping[str, object]) -> str:
    """Return deterministic correction guidance for a contradictory submission."""
    del payload
    return (
        "Contradictory validation verdict: current_failure_evidence attests that failures "
        "currently exist. Either return status='invalid' with blocking_reasons, or return "
        "an empty current_failure_evidence array if nothing is currently failing."
    )


def _gap_result(criterion: str, explanation: str) -> CriterionResult:
    return CriterionResult(
        criterion=criterion,
        status="gap",
        evidence_ids=[],
        explanation=explanation,
    )


def _parse_criterion_results(
    raw: object,
    *,
    expected_criteria: Sequence[str],
    admissible_evidence_ids: frozenset[str],
) -> tuple[list[CriterionResult], list[str]]:
    errors: list[str] = []
    if not isinstance(raw, list):
        return (
            [
                _gap_result(criterion, "Validator response omitted structured criterion coverage")
                for criterion in expected_criteria
            ],
            ["Validation response did not provide criterion_results as a list"],
        )

    by_criterion: dict[str, CriterionResult] = {}
    duplicate_criteria: set[str] = set()
    expected_set = set(expected_criteria)
    sole_expected = expected_criteria[0] if len(expected_criteria) == 1 and len(raw) == 1 else None
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"criterion_results[{index}] is not an object")
            continue
        criterion = item.get("criterion")
        status = item.get("status")
        explanation = item.get("explanation")
        evidence = item.get("evidence_ids")
        if (
            sole_expected is not None
            and isinstance(criterion, str)
            and criterion.strip()
            and criterion not in expected_set
        ):
            criterion = sole_expected
        if not isinstance(criterion, str) or criterion not in expected_set:
            errors.append(f"criterion_results[{index}] does not cite an exact task criterion")
            continue
        if criterion in by_criterion:
            duplicate_criteria.add(criterion)
            continue
        if status not in {"satisfied", "gap"}:
            errors.append(f"{criterion}: status must be 'satisfied' or 'gap'")
            by_criterion[criterion] = _gap_result(criterion, "Malformed criterion status")
            continue
        if not isinstance(explanation, str) or not explanation.strip():
            errors.append(f"{criterion}: explanation must be nonempty")
            by_criterion[criterion] = _gap_result(criterion, "Missing criterion explanation")
            continue
        if not isinstance(evidence, list) or not all(
            isinstance(evidence_id, str) and evidence_id.strip() for evidence_id in evidence
        ):
            errors.append(f"{criterion}: evidence_ids must be a list of receipt IDs")
            by_criterion[criterion] = _gap_result(criterion, "Malformed evidence citations")
            continue
        cited_ids = list(dict.fromkeys(str(evidence_id).strip() for evidence_id in evidence))
        invented = [
            evidence_id for evidence_id in cited_ids if evidence_id not in admissible_evidence_ids
        ]
        if invented:
            errors.append(f"{criterion}: cited inadmissible evidence IDs: {', '.join(invented)}")
            by_criterion[criterion] = _gap_result(
                criterion,
                f"Validator cited evidence outside the admissible packet: {', '.join(invented)}",
            )
            continue
        if status == "satisfied" and not cited_ids:
            errors.append(f"{criterion}: satisfied verdict has no evidence citation")
            by_criterion[criterion] = _gap_result(
                criterion,
                "No admissible evidence was cited for the satisfied verdict",
            )
            continue
        by_criterion[criterion] = CriterionResult(
            criterion=criterion,
            status=cast(Literal["satisfied", "gap"], status),
            evidence_ids=cited_ids,
            explanation=explanation.strip(),
        )

    for criterion in duplicate_criteria:
        errors.append(f"{criterion}: criterion appears more than once")
        by_criterion[criterion] = _gap_result(criterion, "Duplicate criterion verdicts")
    results = [
        by_criterion.get(
            criterion,
            _gap_result(criterion, "Validator response did not cover this criterion"),
        )
        for criterion in expected_criteria
    ]
    return results, errors


def _validation_result_from_data(
    result_data: Mapping[str, object],
    *,
    expected_criteria: Sequence[str],
    admissible_evidence_ids: Sequence[str],
) -> ValidationResult:
    normalized = demote_contradictory_valid(result_data)
    reported_status = str(normalized.get("status", "pending")).strip().lower()
    criterion_results, parse_errors = _parse_criterion_results(
        normalized.get("criterion_results"),
        expected_criteria=expected_criteria,
        admissible_evidence_ids=frozenset(admissible_evidence_ids),
    )
    reasons = [
        f"{result.criterion}: {result.explanation}"
        for result in criterion_results
        if result.status == "gap"
    ]
    all_satisfied = bool(expected_criteria) and all(
        result.status == "satisfied" for result in criterion_results
    )
    derived_status = "valid" if all_satisfied and not parse_errors else "invalid"
    if reported_status not in _VERDICT_STATUSES:
        parse_errors.append(f"Unknown overall validation status: {reported_status}")
    elif reported_status == "valid" and not all_satisfied:
        parse_errors.append("Overall valid verdict contradicts one or more criterion gaps")
    elif reported_status != "valid" and all_satisfied:
        parse_errors.append("Overall non-valid verdict contradicts complete criterion satisfaction")
    status = "valid" if derived_status == "valid" and reported_status == "valid" else "invalid"
    reasons.extend(parse_errors)
    feedback = normalized.get("feedback")
    if not isinstance(feedback, str):
        feedback = None
    issues = _coerce_issues(normalized.get("issues"))
    if status == "valid":
        reasons = []
    elif not reasons:
        reasons = ["Validation response did not name unmet criteria or failing gates"]
    override = normalized.get("verdict_override")
    if not isinstance(override, dict):
        override = None
    failure_category = None
    if status != "valid":
        failure_category = classify_failure(
            "\n".join(part for part in (feedback, *reasons) if part)
        )
    return ValidationResult(
        status=cast(Literal["valid", "invalid", "pending", "error"], status),
        feedback=feedback,
        blocking_reasons=reasons,
        issues=issues,
        verdict_override=override,
        failure_category=failure_category,
        criterion_results=criterion_results,
    )


def format_close_validation_message(
    status: str,
    narrative: str | None,
    blocking_reasons: Sequence[str],
    verdict_override: dict[str, object] | None,
    *,
    lead: str = "Close blocked",
) -> str:
    """Format one mechanical-first message for any non-valid close verdict."""
    first_line = f"{lead}: validation verdict '{status}'"
    if verdict_override is not None:
        evidence = filter_failure_evidence(verdict_override.get("evidence"))
        if evidence:
            first_line += (
                " — verdict overridden: validator attested current failures: " + "; ".join(evidence)
            )
    parts = [first_line]
    reasons = [reason.strip() for reason in blocking_reasons if reason.strip()]
    if reasons:
        parts.append(f"Blocking reasons: {'; '.join(reasons)}")
    message = "\n".join(parts)
    if narrative and narrative.strip():
        message += f"\n\nValidator feedback:\n{narrative}"
    return message


__all__ = [
    "ValidationResult",
    "CriterionResult",
    "_coerce_blocking_reasons",
    "_validation_result_from_data",
    "contradiction_rejection_message",
    "demote_contradictory_valid",
    "filter_failure_evidence",
    "format_close_validation_message",
    "is_contradictory_valid",
]
