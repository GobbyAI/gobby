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


def _validation_result_from_data(result_data: Mapping[str, object]) -> ValidationResult:
    normalized = demote_contradictory_valid(result_data)
    status = str(normalized.get("status", "pending")).strip().lower()
    if status not in _VERDICT_STATUSES:
        status = "pending"
    reasons = _coerce_blocking_reasons(normalized.get("blocking_reasons"))
    feedback = normalized.get("feedback")
    if not isinstance(feedback, str):
        feedback = None
    issues = _coerce_issues(normalized.get("issues"))
    if status == "valid":
        reasons = []
    elif not reasons:
        status = "pending"
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
    "_coerce_blocking_reasons",
    "_validation_result_from_data",
    "contradiction_rejection_message",
    "demote_contradictory_valid",
    "filter_failure_evidence",
    "format_close_validation_message",
    "is_contradictory_valid",
]
