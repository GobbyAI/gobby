"""Lenient parser for the bounded task-close criteria verdict."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Literal, cast

VerdictStatus = Literal["valid", "invalid"]


class CloseVerdictParseError(ValueError):
    """The validator response did not contain a usable close verdict."""


@dataclass(frozen=True)
class CloseCriterionVerdict:
    index: int
    criterion: str
    satisfied: bool
    gap: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "criterion": self.criterion,
            "satisfied": self.satisfied,
            "gap": self.gap,
        }


@dataclass(frozen=True)
class CloseVerdict:
    status: VerdictStatus
    criteria: tuple[CloseCriterionVerdict, ...]
    feedback: str

    @property
    def valid(self) -> bool:
        return self.status == "valid"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "criteria": [criterion.to_dict() for criterion in self.criteria],
            "feedback": self.feedback,
        }


def parse_close_verdict(payload: object, expected_criteria: Sequence[str]) -> CloseVerdict:
    """Parse a bounded verdict without citation or contradiction policing."""
    data = _coerce_payload(payload)
    status = _coerce_status(data.get("status"))
    feedback = _coerce_feedback(data.get("feedback"), status)
    raw_entries = data.get("criteria")
    entries = (
        [entry for entry in raw_entries if isinstance(entry, Mapping)]
        if isinstance(raw_entries, list)
        else []
    )

    matched = _match_entries(entries, expected_criteria)
    criteria: list[CloseCriterionVerdict] = []
    inherited_satisfied = status == "valid"
    for index, criterion in enumerate(expected_criteria, start=1):
        entry = matched.get(index)
        if entry is None:
            satisfied = inherited_satisfied
            gap = None if satisfied else feedback
        else:
            satisfied = _coerce_satisfied(entry.get("satisfied"), inherited_satisfied)
            gap = _coerce_gap(entry.get("gap"))
            if not satisfied and gap is None:
                gap = feedback
        criteria.append(
            CloseCriterionVerdict(
                index=index,
                criterion=criterion,
                satisfied=satisfied,
                gap=gap,
            )
        )
    return CloseVerdict(status=status, criteria=tuple(criteria), feedback=feedback)


def _coerce_payload(payload: object) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    if not isinstance(payload, str):
        raise CloseVerdictParseError("Validator response was not a JSON object.")
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
                "Validator response did not contain a JSON object."
            ) from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise CloseVerdictParseError("Validator response contained malformed JSON.") from exc
    if not isinstance(parsed, Mapping):
        raise CloseVerdictParseError("Validator JSON response was not an object.")
    return parsed


def _coerce_status(value: object) -> VerdictStatus:
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"valid", "invalid"}:
            return cast(VerdictStatus, normalized)
    raise CloseVerdictParseError("Validator response must contain status 'valid' or 'invalid'.")


def _coerce_feedback(value: object, status: VerdictStatus) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "Criteria review passed." if status == "valid" else "Criteria review found a gap."


def _match_entries(
    entries: Sequence[Mapping[str, Any]],
    expected_criteria: Sequence[str],
) -> dict[int, Mapping[str, Any]]:
    matched: dict[int, Mapping[str, Any]] = {}
    unmatched: list[Mapping[str, Any]] = []
    for entry in entries:
        index = _coerce_index(entry.get("index"))
        if index is not None and 1 <= index <= len(expected_criteria) and index not in matched:
            matched[index] = entry
        else:
            unmatched.append(entry)

    available = {index for index in range(1, len(expected_criteria) + 1) if index not in matched}
    for entry in unmatched:
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
    return matched


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


def _coerce_gap(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _normalize_text(left), _normalize_text(right)).ratio()


def _normalize_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


__all__ = [
    "CloseCriterionVerdict",
    "CloseVerdict",
    "CloseVerdictParseError",
    "VerdictStatus",
    "parse_close_verdict",
]
