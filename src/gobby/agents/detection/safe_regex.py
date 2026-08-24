"""Bounded compilation and execution for data-controlled regular expressions."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

import regex

MAX_PATTERN_SIZE = 2_048

# How long one attempt may run before the regex module interrupts it. This is
# wall clock, so on a busy interpreter it fires for a pattern that matched in
# microseconds but was descheduled mid-search -- measured at 98 spurious
# timeouts in 2000 searches of a 15-line capture, p50 22.5 ms per call, against
# a 7.7 us quiet cost. It is an interruption interval, not the bound.
REGEX_TIMEOUT_SECONDS = 0.01

# The actual bound: CPU this thread may burn across a search. Backtracking is
# what the limit exists to stop and backtracking burns CPU, while waiting to be
# rescheduled does not, so charging CPU tells a pathological pattern apart from
# a busy daemon. Each attempt is wall-clock bounded above, so total CPU cannot
# exceed this by more than one attempt's worth.
REGEX_CPU_BUDGET_SECONDS = 0.01

# Backstop so a pathological scheduler cannot spin here forever. Under the
# contention measured above roughly one attempt in twenty is interrupted, so
# eight consecutive interruptions do not occur in practice; the bound that
# matters is the CPU budget.
MAX_SEARCH_ATTEMPTS = 8


class RegexOutcome(StrEnum):
    """Controlled outcomes from bounded regex execution."""

    MATCH = "match"
    NO_MATCH = "no_match"
    PATTERN_TIMEOUT = "pattern_timeout"


@dataclass(frozen=True, slots=True)
class RegexSearchResult:
    """Result of one bounded search."""

    outcome: RegexOutcome

    @property
    def matched(self) -> bool:
        return self.outcome is RegexOutcome.MATCH


class InvalidPatternError(ValueError):
    """A pattern failed the fixed validation or compilation limits."""

    code = "invalid_pattern"


@dataclass(frozen=True, slots=True)
class SafeRegex:
    """A compiled expression whose searches always use a fixed timeout."""

    source: str
    _compiled: regex.Pattern[str] = field(repr=False)

    def search(self, text: str) -> RegexSearchResult:
        """Search under a CPU budget, reporting a timeout only for real work.

        An interrupted attempt is retried while this thread has burned little
        CPU, because that combination means the search was descheduled rather
        than backtracking. The matcher reads PATTERN_TIMEOUT as a no-match, so
        a spurious one silently disables a detection rule (#20852).
        """
        deadline = time.thread_time() + REGEX_CPU_BUDGET_SECONDS
        for _ in range(MAX_SEARCH_ATTEMPTS):
            try:
                match = self._compiled.search(text, timeout=REGEX_TIMEOUT_SECONDS)
            except TimeoutError:
                if time.thread_time() >= deadline:
                    return RegexSearchResult(RegexOutcome.PATTERN_TIMEOUT)
                continue
            return RegexSearchResult(
                RegexOutcome.MATCH if match is not None else RegexOutcome.NO_MATCH
            )
        return RegexSearchResult(RegexOutcome.PATTERN_TIMEOUT)


def compile_safe_regex(pattern: str) -> SafeRegex:
    """Compile a pattern after enforcing the fixed size limit."""

    if not pattern or len(pattern) > MAX_PATTERN_SIZE:
        raise InvalidPatternError(
            f"pattern length must be between 1 and {MAX_PATTERN_SIZE} characters"
        )
    try:
        compiled = regex.compile(pattern, regex.MULTILINE)
    except regex.error as exc:
        raise InvalidPatternError(f"invalid regular expression: {exc}") from exc
    return SafeRegex(source=pattern, _compiled=compiled)
