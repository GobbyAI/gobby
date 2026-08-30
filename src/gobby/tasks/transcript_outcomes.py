"""Classify bounded shell output and definitive validation outcomes."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any, Literal

from gobby.sessions.transcript_tool_metadata import extract_result_metadata

EvidenceOutcome = Literal["success", "failure", "unknown"]

_EXIT_CODE_KEYS = ("exit_code", "exitCode")
_SUCCESS_STATUSES = {"completed", "ok", "passed", "success", "succeeded"}
_FAILURE_STATUSES = {"error", "failed", "failure"}
_OUTPUT_CHAR_LIMIT = 16_000

_RUNNER_FAILURE_PATTERNS = (
    re.compile(r"\b[1-9]\d*[^\S\n]+(?:failed|failures?|errors?)\b", re.IGNORECASE),
    re.compile(
        r"(?m)^\s*(?:FAILED\s+\S|ERROR\s+\S+::|---\s+FAIL:|FAIL\s+\S|test result:\s*FAILED\b)"
    ),
    re.compile(r"(?m)^\s*Failing new (?:errors|issues) >= \w+: [1-9]\d*\b"),
)


def extract_output(result: Any) -> tuple[str | None, bool]:
    """Extract bounded command output needed to classify validation failures."""
    if isinstance(result, dict) and "outcome_provenance" in result:
        result = {key: value for key, value in result.items() if key != "outcome_provenance"}
    parts: list[str] = []
    seen: set[str] = set()
    for value in _walk_values(result):
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        parts.append(text)
    if not parts:
        return None, False
    output = "\n".join(parts)
    if len(output) <= _OUTPUT_CHAR_LIMIT:
        return output, False
    half = (_OUTPUT_CHAR_LIMIT - len("\n...[output truncated]...\n")) // 2
    return (
        f"{output[:half]}\n...[output truncated]...\n{output[-half:]}",
        True,
    )


def extract_outcome(
    result: Any,
    output: str | None = None,
    *,
    aggregate_status_is_trustworthy: bool = True,
) -> tuple[EvidenceOutcome, int | None, str | None]:
    """Classify one shell result as a validation pass, failure, or unknown."""
    exit_code = _find_exit_code(result)
    if _runner_reported_failures(output) and (
        not aggregate_status_is_trustworthy or exit_code is None
    ):
        return "failure", exit_code, None

    if exit_code is not None:
        return ("success" if exit_code == 0 else "failure"), exit_code, None

    values = list(_walk_values(result))
    for value in values:
        if not isinstance(value, dict):
            continue
        success = value.get("success")
        if isinstance(success, bool):
            return ("success" if success else "failure"), None, None
        status = value.get("status")
        if isinstance(status, str):
            normalized = status.strip().casefold()
            if normalized in _SUCCESS_STATUSES:
                return "success", None, None
            if normalized in _FAILURE_STATUSES:
                return "failure", None, None
        is_error = value.get("is_error")
        if isinstance(is_error, bool):
            return ("failure" if is_error else "success"), None, None
        error = value.get("error")
        if error not in (None, "", False, []):
            return "failure", None, None

    unknown_reason = None
    for value in values:
        if isinstance(value, dict):
            reason = value.get("unknown_reason")
            if isinstance(reason, str) and reason:
                unknown_reason = reason
                break
    return "unknown", None, unknown_reason or "missing definitive provider outcome"


def _runner_reported_failures(output: str | None) -> bool:
    if not output:
        return False
    return any(pattern.search(output) for pattern in _RUNNER_FAILURE_PATTERNS)


def _find_exit_code(result: Any) -> int | None:
    for value in _walk_values(result):
        if not isinstance(value, dict):
            continue
        metadata = extract_result_metadata("bash", value)
        candidates = [metadata.get("exit_code"), *(value.get(key) for key in _EXIT_CODE_KEYS)]
        for candidate in candidates:
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                return candidate
    return None


def _walk_values(value: Any, *, depth: int = 0) -> Iterable[Any]:
    if depth > 8:
        return
    yield value
    if isinstance(value, dict):
        for nested in value.values():
            yield from _walk_values(nested, depth=depth + 1)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_values(nested, depth=depth + 1)
    elif isinstance(value, str) and value[:1] in {"{", "["}:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return
        yield from _walk_values(decoded, depth=depth + 1)
