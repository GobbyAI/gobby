"""Bounded compilation and execution for data-controlled regular expressions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import regex

MAX_PATTERN_SIZE = 2_048
REGEX_TIMEOUT_SECONDS = 0.01


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
        try:
            match = self._compiled.search(
                text,
                timeout=REGEX_TIMEOUT_SECONDS,
                concurrent=True,
            )
        except TimeoutError:
            return RegexSearchResult(RegexOutcome.PATTERN_TIMEOUT)
        outcome = RegexOutcome.MATCH if match is not None else RegexOutcome.NO_MATCH
        return RegexSearchResult(outcome)


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
