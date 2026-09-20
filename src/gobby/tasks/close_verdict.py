"""Lenient parser for the bounded task-close criteria verdict."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Literal, cast

from gobby.tasks.criteria_contract import is_external_criterion

VerdictStatus = Literal["valid", "invalid"]
CriterionVerdictState = Literal["satisfied", "gap", "pending_external"]
FindingSeverity = Literal["critical", "high", "medium", "low"]
FindingCategory = Literal[
    "bug",
    "security",
    "performance",
    "maintainability",
    "test",
    "style",
    "documentation",
    "other",
]

FINDING_SEVERITY_ORDER: dict[FindingSeverity, int] = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


class CloseVerdictParseError(ValueError):
    """The reviewer response did not contain a usable close verdict."""


@dataclass(frozen=True)
class CloseCriterionVerdict:
    index: int
    criterion: str
    satisfied: bool
    gap: str | None
    required_evidence: str | None = None
    state: CriterionVerdictState | None = None

    @property
    def verdict_state(self) -> CriterionVerdictState:
        if self.state is not None:
            return self.state
        return "satisfied" if self.satisfied else "gap"

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "criterion": self.criterion,
            "state": self.verdict_state,
            "satisfied": self.satisfied,
            "gap": self.gap,
            "required_evidence": self.required_evidence,
        }


@dataclass(frozen=True)
class CloseFinding:
    path: str
    start_line: int
    end_line: int
    severity: FindingSeverity
    category: FindingCategory
    description: str
    suggested_fix: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "severity": self.severity,
            "category": self.category,
            "description": self.description,
            "suggested_fix": self.suggested_fix,
        }


@dataclass(frozen=True)
class CloseVerdict:
    status: VerdictStatus
    criteria: tuple[CloseCriterionVerdict, ...]
    feedback: str
    findings: tuple[CloseFinding, ...] = ()

    @property
    def valid(self) -> bool:
        return self.status == "valid"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "criteria": [criterion.to_dict() for criterion in self.criteria],
            "feedback": self.feedback,
            "findings": [finding.to_dict() for finding in self.findings],
        }


def parse_close_verdict(
    payload: object,
    expected_criteria: Sequence[str],
    *,
    defer_external_criteria: bool = False,
) -> CloseVerdict:
    """Parse a bounded verdict without citation or contradiction policing."""
    data = _coerce_payload(payload)
    status = _coerce_status(data.get("status"))
    feedback = _coerce_feedback(data.get("feedback"), status)
    findings = _coerce_findings(data.get("findings"))
    raw_entries = data.get("criteria")
    entries = (
        [entry for entry in raw_entries if isinstance(entry, Mapping)]
        if isinstance(raw_entries, list)
        else []
    )

    matched, claimed = _match_entries(entries, expected_criteria)
    _require_exact_index_set(claimed, len(entries), len(expected_criteria))
    criteria: list[CloseCriterionVerdict] = []
    default_satisfied = status == "valid"
    for index, criterion in enumerate(expected_criteria, start=1):
        entry = matched[index]
        satisfied = _coerce_satisfied(entry.get("satisfied"), default_satisfied)
        state: CriterionVerdictState = _coerce_criterion_state(
            entry.get("state"),
            "satisfied" if satisfied else "gap",
        )
        deferred = defer_external_criteria and is_external_criterion(criterion)
        if state == "pending_external" and not deferred:
            # Only a spawned-agent close defers Live: criteria; any other close is
            # the one that judges them, so a deferral there leaves the criterion
            # unjudged forever (#21760).
            raise CloseVerdictParseError(
                f"criterion {index}: pending_external is reserved for Live: criteria "
                "when the close caller is a spawned agent; report satisfied or gap"
            )
        if deferred:
            state = "pending_external"
        if state == "pending_external":
            satisfied = False
            gap = None
        elif state == "satisfied":
            satisfied = True
            gap = None
        else:
            satisfied = False
            gap = _coerce_gap(entry.get("gap")) or feedback
        required_evidence = _coerce_gap(entry.get("required_evidence")) if state == "gap" else None
        criteria.append(
            CloseCriterionVerdict(
                index=index,
                criterion=criterion,
                satisfied=satisfied,
                gap=gap,
                required_evidence=required_evidence,
                state=state,
            )
        )
    return CloseVerdict(
        status=status,
        criteria=tuple(criteria),
        feedback=feedback,
        findings=findings,
    )


def _coerce_findings(value: object) -> tuple[CloseFinding, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise CloseVerdictParseError("Reviewer findings must be an array.")
    findings: list[CloseFinding] = []
    severities = set(FINDING_SEVERITY_ORDER)
    categories = {
        "bug",
        "security",
        "performance",
        "maintainability",
        "test",
        "style",
        "documentation",
        "other",
    }
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, Mapping):
            raise CloseVerdictParseError(f"finding {index} must be an object")
        path = raw.get("path")
        start_line = raw.get("start_line")
        end_line = raw.get("end_line")
        severity = raw.get("severity")
        category = raw.get("category")
        description = raw.get("description")
        suggested_fix = raw.get("suggested_fix")
        if not isinstance(path, str) or not path.strip():
            raise CloseVerdictParseError(f"finding {index} requires a non-empty path")
        if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
            raise CloseVerdictParseError(f"finding {index} requires start_line >= 1")
        if isinstance(end_line, bool) or not isinstance(end_line, int) or end_line < start_line:
            raise CloseVerdictParseError(f"finding {index} requires end_line >= start_line")
        if not isinstance(severity, str) or severity not in severities:
            raise CloseVerdictParseError(
                f"finding {index} severity must be critical, high, medium, or low"
            )
        if not isinstance(category, str) or category not in categories:
            raise CloseVerdictParseError(f"finding {index} has an unsupported category")
        if not isinstance(description, str) or not description.strip():
            raise CloseVerdictParseError(f"finding {index} requires a description")
        if suggested_fix is not None and (
            not isinstance(suggested_fix, str) or not suggested_fix.strip()
        ):
            raise CloseVerdictParseError(
                f"finding {index} suggested_fix must be null or a non-empty string"
            )
        findings.append(
            CloseFinding(
                path=path.strip(),
                start_line=start_line,
                end_line=end_line,
                severity=cast(FindingSeverity, severity),
                category=cast(FindingCategory, category),
                description=description.strip(),
                suggested_fix=suggested_fix.strip() if isinstance(suggested_fix, str) else None,
            )
        )
    return tuple(findings)


def _coerce_payload(payload: object) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    if not isinstance(payload, str):
        raise CloseVerdictParseError("Reviewer response was not a JSON object.")
    text = payload.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise CloseVerdictParseError(
                "Reviewer response did not contain a JSON object."
            ) from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise CloseVerdictParseError("Reviewer response contained malformed JSON.") from exc
    if not isinstance(parsed, Mapping):
        raise CloseVerdictParseError("Reviewer JSON response was not an object.")
    return parsed


def _coerce_status(value: object) -> VerdictStatus:
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"valid", "invalid"}:
            return cast(VerdictStatus, normalized)
    raise CloseVerdictParseError("Reviewer response must contain status 'valid' or 'invalid'.")


def _coerce_feedback(value: object, status: VerdictStatus) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "Criteria review passed." if status == "valid" else "Criteria review found a gap."


def _require_exact_index_set(
    claimed: Sequence[int],
    entry_count: int,
    expected_count: int,
) -> None:
    """Reject a submission that does not cover each criterion index exactly once."""
    expected = list(range(1, expected_count + 1))
    received = list(claimed)
    unresolved = entry_count - len(received)
    if received == expected and unresolved == 0:
        return
    detail = f"expected {expected}, received {received}"
    if unresolved:
        detail += f", plus entries matching no criterion (count {unresolved})"
    raise CloseVerdictParseError(
        "Close verdict must report every criterion index exactly once: "
        f"{detail}. Resubmit one entry per criterion index."
    )


def _match_entries(
    entries: Sequence[Mapping[str, Any]],
    expected_criteria: Sequence[str],
) -> tuple[dict[int, Mapping[str, Any]], tuple[int, ...]]:
    """Map criterion indexes to entries and report every index the submission claimed."""
    matched: dict[int, Mapping[str, Any]] = {}
    claimed: list[int] = []
    unmatched: list[tuple[Mapping[str, Any], int | None]] = []
    for entry in entries:
        index = _coerce_index(entry.get("index"))
        if index is not None:
            claimed.append(index)
        if index is not None and 1 <= index <= len(expected_criteria) and index not in matched:
            matched[index] = entry
        else:
            unmatched.append((entry, index))

    available = {index for index in range(1, len(expected_criteria) + 1) if index not in matched}
    for entry, claimed_index in unmatched:
        text = _entry_text(entry)
        if not text or not available:
            continue
        best_index, score = max(
            ((index, _similarity(text, expected_criteria[index - 1])) for index in available),
            key=lambda pair: pair[1],
        )
        if score >= 0.58:
            matched[best_index] = entry
            available.remove(best_index)
            # A mis-indexed entry keeps claiming its own bad index, so only an
            # entry that named no index at all is credited with the match.
            if claimed_index is None:
                claimed.append(best_index)
    return matched, tuple(sorted(claimed))


def _coerce_index(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _entry_text(entry: Mapping[str, Any]) -> str | None:
    for key in ("criterion", "text", "name"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _coerce_satisfied(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "satisfied", "pass", "passed"}:
            return True
        if normalized in {"false", "no", "gap", "fail", "failed", "unsatisfied"}:
            return False
    return default


def _coerce_criterion_state(
    value: object,
    default: CriterionVerdictState,
) -> CriterionVerdictState:
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"satisfied", "gap", "pending_external"}:
            return cast(CriterionVerdictState, normalized)
    return default


def _coerce_gap(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _normalize_text(left), _normalize_text(right)).ratio()


def _normalize_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


__all__ = [
    "CloseCriterionVerdict",
    "CloseFinding",
    "CloseVerdict",
    "CloseVerdictParseError",
    "CriterionVerdictState",
    "FINDING_SEVERITY_ORDER",
    "FindingCategory",
    "FindingSeverity",
    "VerdictStatus",
    "parse_close_verdict",
]
