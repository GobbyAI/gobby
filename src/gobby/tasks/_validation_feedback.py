"""Classify task-validation feedback text."""

from __future__ import annotations

import re

_FAILURE_FEEDBACK_FLAGS = re.IGNORECASE | re.DOTALL
_VALIDATION_GATE_WORDS = (
    r"(?:"
    r"(?:required\s+)?(?:validation|verification|quality)\s+(?:gate|check|step)s?|"
    r"(?:required\s+)?checks?|"
    r"(?:test|build|compil(?:e|ation|er)|lint|format|coverage|static\s+analysis)"
    r"(?:\s+(?:gate|check|step))?s?|"
    r"ci(?:\s+(?:gate|check|step))?"
    r")"
)
_VALIDATION_FAILURE_WORDS = r"(?:failed|failing|not\s+clean|did\s+not\s+pass|not\s+pass(?:ed|ing)?)"
_SAME_SENTENCE_PROXIMITY = r"[^.!?]{0,100}"
_ZERO_FAILURE_TOKEN_RE = re.compile(
    r"\b(?:0\s+fail(?:ed|ures?)|zero\s+failures?|fail(?:ed|ures?)\s*[=:]\s*0)\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_NEGATED_FAILURE_FRAGMENT_RE = re.compile(
    r"\b(?:no|without)\s+(?:[\w-]+\s+){0,6}"
    r"(?:criteri(?:on|a)|gates?|checks?|errors?|failures?)\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_RESOLVED_REGRESSION_FRAGMENT_RE = re.compile(
    r"\b(?:previous|previously|formerly|earlier|prior)\s+"
    r"(?:(?!\b(?:still|not)\b)[\w-]+\s+){0,8}fail(?:ed|ing|ures?)\b"
    r"(?:(?![.!?]|\b(?:still|not|remain|remaining|unresolved)\b).){0,120}"
    r"\b(?:"
    r"(?:now|successfully)\s+(?:pass(?:es|ed)?|passing|fixed|resolved|green)|"
    r"(?:have|has)\s+been\s+(?:fixed|resolved)|"
    r"(?:are|is|were|was)\s+(?:fixed|resolved|green)|"
    r"(?:fixed|resolved)"
    r")\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_TDD_RED_PARENTHETICAL_RE = re.compile(
    # Example: "TDD evidence is documented: red (3 failing tests, 404s and
    # missing content-encoding header), green (23 passed)" — the parenthetical
    # after "red" describes the intentional pre-implementation failure, not a
    # current gate failure.
    r"\bred\s*\([^()]{0,200}\)",
    _FAILURE_FEEDBACK_FLAGS,
)
_TDD_RED_EVIDENCE_FRAGMENT_RE = re.compile(
    # Example: "Red evidence: failing test output captured before
    # implementation" — a description of the TDD red phase. The window must
    # not cross a contrastive clause, so "red evidence was captured, but the
    # final test run failed" keeps its failure evidence.
    r"\b(?:tdd|red)(?:\s+(?:phase|step|run|stage))?\s+evidence\b"
    r"(?:(?!\b(?:but|however|yet|although|though)\b)[^.!?]){0,140}",
    _FAILURE_FEEDBACK_FLAGS,
)
_PRE_IMPLEMENTATION_FAILURE_FRAGMENT_RE = re.compile(
    # Example: "2 tests failing prior to implementation" — the expected TDD
    # red failure, not a current one. The gap must not cross a contrastive
    # clause so trailing genuine admissions survive.
    r"\bfail(?:ed|ing|ures?)\b"
    r"(?:(?!\b(?:but|however|yet)\b)[^.!?]){0,80}"
    r"\b(?:before|prior\s+to|pre)[\s-]*(?:the\s+)?implementation\b"
    r"|\bpre-?implementation\b[^.!?]{0,60}\bfail(?:ed|ing|ures?)\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_FAILED_AS_EXPECTED_FRAGMENT_RE = re.compile(
    # Example: "the red test run failed as expected" — an intentional TDD
    # failure description, not an admission.
    r"\bfail(?:ed|ing|s)?\s+as\s+(?:expected|designed|intended)\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_QUOTED_FEEDBACK_FRAGMENT_RE = re.compile(
    r"(?:\"[^\"]{1,240}\"|`[^`]{1,240}`|(?<!\w)'[^']{1,240}'(?!\w))",
    _FAILURE_FEEDBACK_FLAGS,
)
_ASSERTION_VERBS = (
    r"(?:assert(?:s|ed|ing)?|expect(?:s|ed|ing)?|verif(?:y|ies|ied|ying)|"
    r"prov(?:e|es|ed|ing)|ensur(?:e|es|ed|ing)|mark(?:s|ed|ing)?|"
    r"record(?:s|ed|ing)?|coerc(?:e|es|ed|ing)|convert(?:s|ed|ing)?|"
    r"treat(?:s|ed|ing)?|classif(?:y|ies|ied|ying))"
)
_ASSERTION_DESCRIBED_FAILURE_FRAGMENT_RE = re.compile(
    # Example: "an end-to-end test asserting a real CronRun records
    # status=failed with error populated" — a description of an assertion or
    # designed behavior about failure states, not an admission that a gate
    # failed. The gap must not cross a contrastive clause, a gate word, or a
    # success word, so genuine admissions like "verify the retry path, but the
    # lint check failed" keep their failure evidence.
    rf"\b{_ASSERTION_VERBS}\b"
    rf"(?:(?!\b(?:but|however|yet|although|though|satisfied|met|pass(?:es|ed|ing)?)\b|"
    rf"\b{_VALIDATION_GATE_WORDS}\b)[^.!?]){{0,80}}?"
    r"\bfail(?:ed|ing|ures?)\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_STATUS_VALUE_FAILURE_TOKEN_RE = re.compile(
    # Example: "records status=failed" or "the run status is 'failed'" — a
    # status *value* named failed, not a failure event.
    r"\bstatus\s*(?:[=:]{1,2}\s*|\s+(?:is|as|of)\s+)(?:recorded\s+as\s+)?"
    r"[\"'`]?fail(?:ed|ure)[\"'`]?\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_FAILURE_BUCKET_DESTINATION_RE = re.compile(
    # Example: "pushes these into failed" or "the source appears under failed"
    # — `failed` names a result bucket records are routed into, not a failure
    # event. Reversed order ("failed under load") does not match.
    r"\b(?:into|under|onto)\s+(?:the\s+)?[\"'`]?failed[\"'`]?\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_FAILURE_COLLECTION_NOUN_RE = re.compile(
    # Example: "an empty failed array" or "the failed list" — `failed`
    # adjectivally naming a data structure, not an event. Gate nouns such as
    # "failed tests" or "failed checks" are deliberately excluded so genuine
    # admissions keep their failure evidence.
    r"\bfail(?:ed|ures?)\s+"
    r"(?:arrays?|lists?|entr(?:y|ies)|buckets?|fields?|keys?|columns?|collections?)\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_FAILURE_BUCKET_EMPTY_RE = re.compile(
    # Example: "failed is empty" or "failed remains empty" — asserting a result
    # bucket named `failed` holds nothing, which describes success.
    r"\bfail(?:ed|ures?)\s+(?:is|are|was|were|remains?|stays?)\s+empty\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_FAILURE_COMPOUND_MODIFIER_RE = re.compile(
    # Example: "the explicit-id failed-path assertion" — hyphenated compounds
    # use failed/failing as a naming modifier, not as an event.
    r"\bfail(?:ed|ing)-[\w-]+\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_NONZERO_FAILURE_COUNT_RE = re.compile(
    # Example: "1 failed" or "2 failures".
    r"\b(?:[1-9]\d*|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"fail(?:ed|ures?)\b|\bfail(?:ed|ures?)\s*[=:]\s*[1-9]\d*\b",
    _FAILURE_FEEDBACK_FLAGS,
)

_ACCEPTANCE_CRITERIA_THEN_FAILURE_RE = re.compile(
    # Example: "Acceptance criteria failed for the delivered implementation."
    r"\b(?:acceptance\s+)?criteri(?:on|a)\b.{0,80}"
    r"\b(?:failed|failing|unmet|unsatisfied|not\s+(?:satisfied|met))\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_FAILURE_THEN_ACCEPTANCE_CRITERIA_RE = re.compile(
    # Example: "Failed acceptance criteria remain unresolved."
    r"\b(?:failed|failing|unmet|unsatisfied|not\s+(?:satisfied|met))\b.{0,80}"
    r"\b(?:acceptance\s+)?criteri(?:on|a)\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_VALIDATION_GATE_THEN_FAILURE_RE = re.compile(
    # Example: "Required validation gate did not pass."
    rf"\b{_VALIDATION_GATE_WORDS}\b{_SAME_SENTENCE_PROXIMITY}\b{_VALIDATION_FAILURE_WORDS}\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_FAILURE_THEN_VALIDATION_GATE_RE = re.compile(
    # Example: "Tests are failing in the required validation check."
    rf"\b{_VALIDATION_FAILURE_WORDS}\b{_SAME_SENTENCE_PROXIMITY}\b{_VALIDATION_GATE_WORDS}\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_ERRORS_REMAIN_PREDICATE = (
    # "errors remain", "errors are unresolved", "errors still remaining" —
    # remain/unresolved must predicate the errors through at most a short
    # copula/adverb gap. A bare adjective later in the sentence ("resolved/"
    # "unresolved citation behavior") is feature vocabulary, not an admission
    # (#17766 close verdict misfire).
    r"\berrors?\s+"
    r"(?:(?:are|is|was|were|still|currently)\s+){0,2}"
    r"(?:remain(?:s|ed|ing)?|unresolved)\b"
)
_VALIDATION_GATE_THEN_ERRORS_REMAIN_RE = re.compile(
    # Example: "Validation gate errors remain unresolved."
    rf"\b{_VALIDATION_GATE_WORDS}\b.{{0,100}}{_ERRORS_REMAIN_PREDICATE}",
    _FAILURE_FEEDBACK_FLAGS,
)
_VALIDATION_ERRORS_REMAIN_RE = re.compile(
    # Example: "Validation errors remain in the package."
    r"\b(?:validation|verification)\s+errors?\s+"
    r"(?:remain|remaining|(?:are|is)\s+unresolved|unresolved)\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_ERRORS_REMAIN_THEN_VALIDATION_GATE_RE = re.compile(
    # Example: "Errors remain in the validation step."
    rf"{_ERRORS_REMAIN_PREDICATE}.{{0,100}}\b{_VALIDATION_GATE_WORDS}\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_ERRORS_PREVENTED_CLEAN_PASS_RE = re.compile(
    # Example: "Errors prevented a clean pass."
    r"\berrors?\b.{0,80}\bprevented\b.{0,80}\b(?:clean|pass(?:ing)?|valid)\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_REMAINING_GAP_IS_VALIDATION_RE = re.compile(
    # Example: "The only gap is the coverage gate."
    r"\b(?:only|remaining)\s+gap\s+(?:is|remains)\b.{0,120}"
    rf"\b(?:{_VALIDATION_GATE_WORDS}|criteri(?:on|a))\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_MYPY_THEN_INCOMPLETE_RE = re.compile(
    # Example: "mypy is incomplete at the service boundary."
    r"\bmypy\b.{0,80}\b(?:incomplete|unresolved)\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_INCOMPLETE_THEN_MYPY_RE = re.compile(
    # Example: "Incomplete mypy work remains."
    r"\b(?:incomplete|unresolved)\b.{0,80}\bmypy\b",
    _FAILURE_FEEDBACK_FLAGS,
)
_REQUIRED_FAILURE_FEEDBACK_PATTERNS: tuple[re.Pattern[str], ...] = (
    _NONZERO_FAILURE_COUNT_RE,
    _ACCEPTANCE_CRITERIA_THEN_FAILURE_RE,
    _FAILURE_THEN_ACCEPTANCE_CRITERIA_RE,
    _VALIDATION_GATE_THEN_FAILURE_RE,
    _FAILURE_THEN_VALIDATION_GATE_RE,
    _VALIDATION_GATE_THEN_ERRORS_REMAIN_RE,
    _VALIDATION_ERRORS_REMAIN_RE,
    _ERRORS_REMAIN_THEN_VALIDATION_GATE_RE,
    _ERRORS_PREVENTED_CLEAN_PASS_RE,
    _REMAINING_GAP_IS_VALIDATION_RE,
    _MYPY_THEN_INCOMPLETE_RE,
    _INCOMPLETE_THEN_MYPY_RE,
)

_SUCCESSFUL_VALIDATION_FEEDBACK_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "All three criteria are addressed", "all validation criteria were met":
    # bare "criteria" with a bounded qualifier counts, because judges phrase the
    # approving hallucination against the task's own criteria list, not always
    # the literal words "validation criteria" (#17636). Qualifiers that imply an
    # exception elsewhere ("other", "remaining") never count as approval, and an
    # immediate contrastive tail ("except", "but", ...) disqualifies the match.
    re.compile(
        r"(?:(?=.*\b(?:fixed|resolved|verified|re-?tested)\b).*?)?"
        r"(?<!\bnot\s)\ball\s+"
        r"(?:(?!(?:previous|previously|prior|unmet|unsatisfied|other|remaining)\b)\w+\s+){0,3}"
        r"criteria\s+"
        r"(?:(?:are|were|have\s+been)\s+)?(?:satisfied|met|passed|addressed|covered)\b"
        r"(?!\s*[,;:]?\s*(?:except|but|however|aside|save|unless)\b)",
        re.IGNORECASE,
    ),
)


def _searchable_feedback(feedback: str) -> str:
    normalized_feedback = _ZERO_FAILURE_TOKEN_RE.sub("", " ".join(feedback.split()))
    normalized_feedback = _NEGATED_FAILURE_FRAGMENT_RE.sub("", normalized_feedback)
    normalized_feedback = _RESOLVED_REGRESSION_FRAGMENT_RE.sub("", normalized_feedback)
    normalized_feedback = _TDD_RED_PARENTHETICAL_RE.sub("", normalized_feedback)
    normalized_feedback = _TDD_RED_EVIDENCE_FRAGMENT_RE.sub("", normalized_feedback)
    normalized_feedback = _PRE_IMPLEMENTATION_FAILURE_FRAGMENT_RE.sub("", normalized_feedback)
    normalized_feedback = _FAILED_AS_EXPECTED_FRAGMENT_RE.sub("", normalized_feedback)
    normalized_feedback = _STATUS_VALUE_FAILURE_TOKEN_RE.sub("", normalized_feedback)
    normalized_feedback = _FAILURE_BUCKET_DESTINATION_RE.sub("", normalized_feedback)
    normalized_feedback = _FAILURE_COLLECTION_NOUN_RE.sub("", normalized_feedback)
    normalized_feedback = _FAILURE_BUCKET_EMPTY_RE.sub("", normalized_feedback)
    normalized_feedback = _FAILURE_COMPOUND_MODIFIER_RE.sub("", normalized_feedback)
    normalized_feedback = _ASSERTION_DESCRIBED_FAILURE_FRAGMENT_RE.sub("", normalized_feedback)
    return _QUOTED_FEEDBACK_FRAGMENT_RE.sub("", normalized_feedback)


def feedback_admits_required_validation_failure(feedback: str | None) -> bool:
    """Return True when validator feedback explicitly admits a required gate failed."""
    return matched_required_validation_failure_pattern(feedback) is not None


def matched_successful_validation_pattern(feedback: str | None) -> re.Pattern[str] | None:
    """Return the validation-success pattern matched by feedback, if any."""
    if not feedback or matched_required_validation_failure_pattern(feedback) is not None:
        return None

    return matched_successful_validation_pattern_unchecked(feedback)


def matched_successful_validation_pattern_unchecked(
    feedback: str | None,
) -> re.Pattern[str] | None:
    """Return a success match without applying failure-precedence filtering."""
    if not feedback:
        return None

    searchable_feedback = _searchable_feedback(feedback)
    for pattern in _SUCCESSFUL_VALIDATION_FEEDBACK_PATTERNS:
        if pattern.search(searchable_feedback) is not None:
            return pattern
    return None


def matched_required_validation_failure_pattern(feedback: str | None) -> re.Pattern[str] | None:
    """Return the validation-failure pattern matched by feedback, if any."""
    if not feedback:
        return None

    searchable_feedback = _searchable_feedback(feedback)
    for pattern in _REQUIRED_FAILURE_FEEDBACK_PATTERNS:
        if pattern.search(searchable_feedback) is not None:
            return pattern
    return None
