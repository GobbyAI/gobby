"""Structured task-validation verdict normalization and messaging."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

_NULLISH_FAILURE_EVIDENCE = frozenset({"n/a", "none", "null"})
_VERDICT_STATUSES = frozenset({"valid", "invalid", "pending"})


@dataclass
class ValidationResult:
    """Result of task validation with optional deterministic override provenance."""

    status: Literal["valid", "invalid", "pending", "error"]
    feedback: str | None = None
    blocking_reasons: list[str] = field(default_factory=list)
    mode: Literal["static", "tool_loop"] = "static"
    evidence_refs: tuple[str, ...] = ()
    evidence_complete: bool = True
    trace_summary: tuple[dict[str, object], ...] = ()
    evidence_error: dict[str, object] | None = None
    verdict_override: dict[str, object] | None = None


def _coerce_blocking_reasons(value: object) -> list[str]:
    if isinstance(value, list):
        return [reason for item in value if (reason := str(item).strip())]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


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
    if status == "valid":
        reasons = []
    elif not reasons:
        status = "pending"
        reasons = ["Validation response did not name unmet criteria or failing gates"]
    override = normalized.get("verdict_override")
    if not isinstance(override, dict):
        override = None
    return ValidationResult(
        status=cast(Literal["valid", "invalid", "pending", "error"], status),
        feedback=feedback,
        blocking_reasons=reasons,
        verdict_override=override,
    )


def _is_unsupported_reject(result_data: Mapping[str, object]) -> bool:
    """Return whether an invalid static verdict lacks structurally usable reasons."""
    if str(result_data.get("status", "")).strip().lower() != "invalid":
        return False
    reasons = result_data.get("blocking_reasons")
    if not isinstance(reasons, list | str):
        return True
    return not _coerce_blocking_reasons(reasons)


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
    "_is_unsupported_reject",
    "_validation_result_from_data",
    "contradiction_rejection_message",
    "demote_contradictory_valid",
    "filter_failure_evidence",
    "format_close_validation_message",
    "is_contradictory_valid",
]
