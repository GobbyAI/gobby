"""Tests for bounded data-controlled regular expressions."""

from __future__ import annotations

import time

import pytest

from gobby.agents.detection.safe_regex import (
    MAX_PATTERN_SIZE,
    InvalidPatternError,
    RegexOutcome,
    compile_safe_regex,
)

pytestmark = pytest.mark.unit


def test_pathological_pattern_bounded() -> None:
    pattern = compile_safe_regex(r"(a+)+$")
    started = time.monotonic()

    result = pattern.search("a" * 100_000 + "!")

    assert result.outcome is RegexOutcome.PATTERN_TIMEOUT
    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize("pattern", ["(", "x" * (MAX_PATTERN_SIZE + 1)])
def test_invalid_or_oversized_pattern_is_controlled(pattern: str) -> None:
    with pytest.raises(InvalidPatternError) as exc_info:
        compile_safe_regex(pattern)

    assert exc_info.value.code == "invalid_pattern"
