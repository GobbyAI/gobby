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
    re.compile(
        r"(?m)^[ \t]*test result:[ \t]*ok\.[ \t]*[1-9]\d* passed;[ \t]*0 failed[^\r\n]*\r?\n"
    ),
    re.compile(
        r"(?m)^[ \t]*Summary[ \t]+\[[^\]\r\n]+\][ \t]+\d+ tests? run:"
        r"[ \t]*[1-9]\d* passed(?:,[ \t]*(?:\d+ skipped|0 failed))*[ \t]*\r?\n"
    ),
    re.compile(r"(?m)^=+[ \t]+[1-9]\d* passed(?:,[ \t]*\d+ skipped)?[ \t]+in[ \t]+\d[^\r\n]*\r?\n"),
    re.compile(r"(?m)^Pytest:[ \t]*[1-9]\d* passed(?:,[ \t]*0 failed)?[ \t]*\r?\n"),
)


def runner_reported_failures(output: str | None) -> bool:
    if not output:
        return False
    return any(pattern.search(output) for pattern in _RUNNER_FAILURE_PATTERNS)


def runner_reported_success(output: str | None) -> bool:
    if not output:
        return False
    return any(pattern.search(output) for pattern in _RUNNER_SUCCESS_PATTERNS)
