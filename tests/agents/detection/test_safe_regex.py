"""Tests for bounded data-controlled regular expressions."""

from __future__ import annotations

import threading
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


def test_contention_from_other_threads_never_reads_as_a_pattern_timeout() -> None:
    """A busy process must not make a microsecond pattern look pathological.

    REGEX_TIMEOUT_SECONDS is wall clock, so a search that hands the GIL away
    mid-match is timed on how long it waits to get the GIL back rather than on
    how long it matches. The matcher turns PATTERN_TIMEOUT into a no-match, so
    every prompt-detection and stall-classification rule would silently stop
    firing under exactly the load that makes detection matter (#20852).
    """
    pattern = compile_safe_regex(r"^\s*bypass permissions\b.*$")
    text = "\n".join([f"  line {index} of scrollback" for index in range(14)])
    text += "\n  bypass permissions on"

    stop = threading.Event()

    def _burn_the_gil() -> None:
        counter = 0
        while not stop.is_set():
            counter += 1

    threads = [threading.Thread(target=_burn_the_gil, daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    try:
        outcomes = [pattern.search(text).outcome for _ in range(400)]
    finally:
        stop.set()
        for thread in threads:
            thread.join()

    assert set(outcomes) == {RegexOutcome.MATCH}, (
        "contention from other threads was reported as a pattern timeout; "
        "the search must hold the GIL so the timeout bounds matching, not scheduling"
    )
