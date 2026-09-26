"""Failure summaries that remain definitive when a shell process stays open."""

from __future__ import annotations

import re

_RUNNER_FAILURE_PATTERNS = (
    re.compile(r"\b[1-9]\d*[^\S\n]+(?:failed|failures?|errors?)\b", re.IGNORECASE),
    re.compile(
        r"(?m)^\s*(?:FAILED\s+\S|ERROR\s+\S+::|---\s+FAIL:|FAIL\s+\S|test result:\s*FAILED\b)"
    ),
    re.compile(r"(?m)^\s*Failing new (?:errors|issues) >= \w+: [1-9]\d*\b"),
)
_RUNNER_SUCCESS_PATTERNS = (
    re.compile(r"(?m)^\s*test result:\s*ok\.\s*[1-9]\d* passed;\s*0 failed\b"),
    re.compile(r"(?m)^\s*Summary\s+\[[^\]\n]+\]\s+\d+ tests? run:\s*[1-9]\d* passed\b"),
    re.compile(r"(?m)^=+\s+[1-9]\d* passed(?:,\s*\d+ skipped)?\s+in\s+\d"),
    re.compile(r"(?m)^Pytest:\s*[1-9]\d* passed,\s*0 failed\b"),
)


def runner_reported_failures(output: str | None) -> bool:
    if not output:
        return False
    return any(pattern.search(output) for pattern in _RUNNER_FAILURE_PATTERNS)


def runner_reported_success(output: str | None) -> bool:
    if not output:
        return False
    return any(pattern.search(output) for pattern in _RUNNER_SUCCESS_PATTERNS)
