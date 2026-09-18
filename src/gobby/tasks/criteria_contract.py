"""Task validation-criteria invariants and deterministic criterion splitting."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<text>.+?)\s*$")
_INLINE_NUMBERED_ITEM_RE = re.compile(r"\b(?P<number>\d+)[.)]\s+")
_EXTERNAL_CRITERION_RE = re.compile(r"^live\s*:", re.IGNORECASE)

_OPERATIONAL_REQUIREMENTS = {
    "install": re.compile(
        r"(?P<direct>\binstall(?:ing)?\s+(?:the\s+)?"
        r"(?:release|binary|artifact|package|build|executable|service|plugin|skill)\b|"
        r"\b(?:release|binary|artifact|package|build|executable|service|plugin|skill)\b"
        r"(?:\s+[\w.-]+){0,4}\s+(?:(?:(?:is|was|must|shall|should|will)\s+"
        r"(?:be\s+)?|(?:needs?|has)\s+to\s+be\s+)installed)\b)|"
        r"(?P<nominal>\b(?:release|binary|artifact|package|build|executable|service|"
        r"plugin|skill)\b(?:\s+[\w.-]+){0,4}\s+installation\b)",
        re.IGNORECASE,
    ),
    "restart": re.compile(
        r"(?P<direct>\brestart(?:ing)?\s+(?:the\s+)?"
        r"(?:daemon|service|server|app(?:lication)?)\b|"
        r"\b(?:daemon|service|server|app(?:lication)?)\s+"
        r"(?:(?:(?:is|was|must|shall|should|will)\s+(?:be\s+)?|"
        r"(?:needs?|has)\s+to\s+be\s+)restarted)\b)|"
        r"(?P<nominal>\b(?:daemon|service|server|app(?:lication)?)\s+restart\b)",
        re.IGNORECASE,
    ),
    "smoke": re.compile(
        r"(?P<direct>\b(?:run|perform|execute|complete)\s+(?:a\s+)?(?:live[- ]+)?"
        r"smoke(?:[- ]+(?:tests?|checks?|probes?))?\b|"
        r"\b(?:live[- ]+)?smoke(?:[- ]+(?:tests?|checks?|probes?))?\s+"
        r"(?:passes|passed|succeeds|succeeded|completes|completed|shows|show|verifies|"
        r"verified)\b)|"
        r"(?P<nominal>\blive[- ]+smoke[- ]+(?:tests?|checks?|probes?)\b)",
        re.IGNORECASE,
    ),
    "deploy": re.compile(
        r"\bdeploy(?:ing)?\s+(?:the\s+)?(?:service|release|app(?:lication)?|site)\b|"
        r"\b(?:service|release|app(?:lication)?|site)\s+(?:(?:is|was|must|shall|should|"
        r"will)\s+(?:be\s+)?|(?:needs?|has)\s+to\s+be\s+)deployed\b",
        re.IGNORECASE,
    ),
    "publish": re.compile(
        r"\bpublish(?:ing)?\s+(?:the\s+)?(?:release|package|artifact|site)\b|"
        r"\b(?:release|package|artifact|site)\s+(?:(?:is|was|must|shall|should|will)\s+"
        r"(?:be\s+)?|(?:needs?|has)\s+to\s+be\s+)published\b",
        re.IGNORECASE,
    ),
    "cutover": re.compile(
        r"\b(?:perform|execute|complete)\s+(?:the\s+)?cutover\b|"
        r"\bcutover\s+(?:completes|completed|passes|passed|succeeds|succeeded)\b",
        re.IGNORECASE,
    ),
}

_OPERATIONAL_COMMANDS = {
    "install": re.compile(
        r"(?:^|[\s;&|])(?:(?:uv|python)\s+run\s+)?gobby\s+install\b|"
        r"(?:^|[\s;&|])uv\s+(?:sync|add)\b|"
        r"(?:^|[\s;&|])(?:pip|cargo|npm|pnpm|yarn)\s+install\b",
        re.IGNORECASE,
    ),
    "restart": re.compile(
        r"(?:^|[\s;&|])(?:(?:uv|python)\s+run\s+)?gobby\s+restart\b|"
        r"(?:^|[\s;&|])(?:systemctl|service)\s+\S+\s+restart\b",
        re.IGNORECASE,
    ),
    "smoke": _OPERATIONAL_REQUIREMENTS["smoke"],
    "deploy": _OPERATIONAL_REQUIREMENTS["deploy"],
    "publish": _OPERATIONAL_REQUIREMENTS["publish"],
    "cutover": _OPERATIONAL_REQUIREMENTS["cutover"],
}

_COMPLETED_OPERATIONAL_EVIDENCE = {
    "install": re.compile(
        r"\b(?:(?P<tool_native>successfully\s+installed|"
        r"(?:resolved|audited|installed)\s+\d+\s+packages?)|installed|"
        r"install(?:ation)?\s+(?:completed|passed|succeeded|verified))\b",
        re.IGNORECASE,
    ),
    "restart": re.compile(
        r"\b(?:restarted|restart\s+(?:completed|passed|succeeded|healthy|verified))\b",
        re.IGNORECASE,
    ),
    "smoke": re.compile(
        r"\b(?:smoke-tested|smoke(?:[- ]+(?:tests?|checks?|probes?))?\b"
        r"[^.!?\n]*?\b(?P<smoke_verb>completed|passed|succeeded|clean|verified))\b",
        re.IGNORECASE,
    ),
    "deploy": re.compile(
        r"\b(?:deployed|deployment\s+(?:completed|passed|succeeded|verified))\b",
        re.IGNORECASE,
    ),
    "publish": re.compile(
        r"\b(?:published|publish(?:ing)?\s+(?:completed|passed|succeeded|verified))\b",
        re.IGNORECASE,
    ),
    "cutover": re.compile(
        r"\bcutover\s+(?:complete|completed|passed|succeeded|verified)\b",
        re.IGNORECASE,
    ),
}

_NEGATED_COMPLETION_PREFIX_RE = re.compile(
    r"\b(?:not|never|without|failed\s+to)\s+(?:been\s+)?$",
    re.IGNORECASE,
)

# Requirement prose negates differently from completion prose, so the pattern above
# cannot be reused: a completion claim puts its negator flush against the verb
# ("was not installed"), while a criterion routes through a requirement verb and an
# article ("does not require a daemon restart") or negates after the phrase entirely
# ("a daemon restart is not required"). Both patterns below anchor to the matched
# phrase, so only an unbroken negator-to-phrase run counts; punctuation and unlisted
# words end the run and leave the requirement standing.
_NEGATED_REQUIREMENT_PREFIX_RE = re.compile(
    r"\b(?:no|not|never|without|avoid(?:s|ed|ing)?)\s+"
    r"(?:(?:requir(?:e|es|ed|ing)|need(?:s|ed|ing)?|involv(?:e|es|ed|ing)|"
    r"forc(?:e|es|ed|ing)|trigger(?:s|ed|ing)?|perform(?:s|ed|ing)?|"
    r"depend(?:s|ed|ing)?\s+on)\s+)?"
    r"(?:a|an|any|the|another|its|their)?\s*$",
    re.IGNORECASE,
)

_NEGATED_REQUIREMENT_SUFFIX_RE = re.compile(
    r"^\s*(?:(?:is|are|was|were|be|been|being|will|would|should|must|can|could|"
    r"does|do|did|has|have|had|need|needs)\s+){0,3}"
    r"(?:not|never|no\s+longer)\b",
    re.IGNORECASE,
)

# Widest negator-to-phrase run worth reading, e.g. "should never require another ".
_NEGATION_WINDOW = 48

_NOMINAL_REQUIREMENT_RE = re.compile(
    r"\b(?:is|are|was|were)\s+(?:explicitly\s+)?(?:required|needed)\b|"
    r"\b(?:must|shall|should|will)\b|"
    r"\b(?:requires?|needs?)\b",
    re.IGNORECASE,
)

_NOMINAL_FOLLOWER_RE = re.compile(
    r"^\s*(?:$|[.,;:]|and\b|is\b|are\b|was\b|were\b|must\b|shall\b|should\b|"
    r"will\b|needs?\b|to\b)",
    re.IGNORECASE,
)

_OPERATIONAL_SUBJECT_RE = re.compile(
    r"\b(?:(?P<generic>artifact|package|build)s?|release|binary|executable|service|plugin|skill|"
    r"ghook|gcode|gobby|daemon|server|app|application|site)\b",
    re.IGNORECASE,
)

_SENTENCE_BOUNDARY_RE = re.compile(r"(?:[.!?](?=\s|$)|\n+)")

_OPERATIONAL_HINTS = {
    "install": ("installed", "package"),
    "restart": ("restarted", "daemon"),
    "smoke": ("passed", "smoke test"),
    "deploy": ("deployed", "service"),
    "publish": ("published", "release"),
    "cutover": ("completed", "cutover"),
}


@dataclass(frozen=True, slots=True)
class CriterionSpan:
    """One criterion's text and the range it occupies in the criteria value."""

    text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _OperationalRequirement:
    action: str
    subjects: frozenset[str]


class TaskCriteriaError(ValueError):
    """Raised when a non-epic task has no observable validation contract."""


def normalized_validation_criteria(value: str | None) -> str | None:
    """Return stripped criteria, treating whitespace-only values as absent."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _inline_numbered_criterion_spans(value: str, offset: int) -> tuple[CriterionSpan, ...] | None:
    matches = list(_INLINE_NUMBERED_ITEM_RE.finditer(value))
    if len(matches) < 2 or matches[0].start() != 0:
        return None
    if [int(match.group("number")) for match in matches] != list(range(1, len(matches) + 1)):
        return None

    spans: list[CriterionSpan] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        text = value[match.end() : end].strip()
        if not text:
            return None
        spans.append(CriterionSpan(text=text, start=match.end() + offset, end=end + offset))
    return tuple(spans)


def require_validation_criteria(task_type: str, value: str | None) -> str | None:
    """Enforce the criteria invariant and return the normalized value."""
    normalized = normalized_validation_criteria(value)
    if task_type != "epic" and normalized is None:
        raise TaskCriteriaError(
            "Every non-epic task requires nonempty validation_criteria. "
            "State observable completion evidence before creating or updating the task."
        )
    return normalized


def criterion_spans(value: str | None) -> tuple[CriterionSpan, ...]:
    """Split free-text criteria into criteria that keep their original ranges.

    A criterion owns its continuation lines, so a wrapped list item or paragraph
    is one span covering every line it was written across.
    """
    normalized = normalized_validation_criteria(value)
    if normalized is None or value is None:
        return ()
    offset = len(value) - len(value.lstrip())

    lines = normalized.split("\n")
    if len(lines) == 1:
        inline_spans = _inline_numbered_criterion_spans(normalized, offset)
        if inline_spans is not None:
            return inline_spans

    spans: list[CriterionSpan] = []
    current: list[str] = []
    start = 0
    end = 0
    saw_list_marker = False
    position = 0

    def span(lines_so_far: list[str], first: int, last: int) -> CriterionSpan:
        return CriterionSpan(text=" ".join(lines_so_far), start=first + offset, end=last + offset)

    for raw_line in lines:
        line_start = position
        position += len(raw_line) + 1
        line = raw_line.strip()
        if not line:
            if current:
                spans.append(span(current, start, end))
                current = []
            continue
        match = _LIST_ITEM_RE.match(raw_line)
        if match is not None:
            if not saw_list_marker:
                spans = []
                current = []
            saw_list_marker = True
            if current:
                spans.append(span(current, start, end))
            current = [match.group("text").strip()]
            start = line_start
        else:
            if not current:
                start = line_start
            current.append(line)
        end = line_start + len(raw_line)

    if current:
        spans.append(span(current, start, end))

    items = tuple(item for item in spans if item.text)
    if saw_list_marker or items:
        return items
    return (CriterionSpan(text=normalized, start=offset, end=offset + len(normalized)),)


def split_validation_criteria(value: str | None) -> tuple[str, ...]:
    """Split free-text criteria into stable, distinct criterion strings."""
    return tuple(span.text for span in criterion_spans(value))


def external_criterion_ranges(value: str | None) -> tuple[tuple[int, int], ...]:
    """Ranges of coordinator-owned ``Live:`` criteria in the original criteria text."""
    return tuple(
        (span.start, span.end)
        for span in criterion_spans(value)
        if is_external_criterion(span.text)
    )


def is_external_criterion(criterion: str) -> bool:
    """Return whether a criterion is coordinator-owned live verification."""
    match = _LIST_ITEM_RE.match(criterion)
    text = match.group("text") if match is not None else criterion.strip()
    return _EXTERNAL_CRITERION_RE.match(text) is not None


def required_operational_actions(value: str | None) -> tuple[str, ...]:
    """Return operational actions explicitly named by acceptance criteria."""
    return tuple(dict.fromkeys(requirement.action for requirement in _requirements(value)))


def operational_evidence_hint(
    validation_criteria: str | None,
    missing_actions: Iterable[str],
) -> str:
    """Describe one canonical completion verb and subject for each missing action."""
    requirements = _requirements(validation_criteria)
    hints: list[str] = []
    for action in dict.fromkeys(missing_actions):
        verb, default_subject = _OPERATIONAL_HINTS[action]
        subjects = sorted(
            {
                subject
                for requirement in requirements
                if requirement.action == action
                for subject in requirement.subjects
            }
        )
        subject_text = " or ".join(subjects) or default_subject
        hints.append(
            f"{action}: use completion verb '{verb}' with subject '{subject_text}' "
            "in the same sentence"
        )
    return "; ".join(hints) + "."


def operational_actions_from_command(command: str) -> tuple[str, ...]:
    """Return operational actions evidenced by one successful shell command."""
    markers: list[str] = []
    for action, pattern in _OPERATIONAL_COMMANDS.items():
        if pattern.search(command) is None:
            continue
        subjects = set(_subjects(command))
        normalized = command.casefold()
        if action == "restart" and "gobby restart" in normalized:
            subjects.update(("daemon", "gobby"))
        elif action == "install" and "gobby install" in normalized:
            subjects.update(("gobby", "ghook", "gcode"))
        markers.append(_evidence_marker(action, subjects))
    return tuple(markers)


def missing_operational_evidence(
    validation_criteria: str | None,
    changes_summary: str,
    *,
    transcript_actions: Iterable[str] = (),
    skip_external: bool = False,
) -> tuple[str, ...]:
    """Return required operational actions lacking affirmative completion evidence."""
    requirements = _requirements(validation_criteria, skip_external=skip_external)
    if not requirements:
        return ()
    transcript_evidence = _parse_evidence_markers(transcript_actions)
    missing: list[str] = []
    for requirement in requirements:
        transcript_subjects = transcript_evidence.get(requirement.action)
        transcript_matches = transcript_subjects is not None and (
            not requirement.subjects or bool(requirement.subjects & transcript_subjects)
        )
        if transcript_matches or _has_affirmative_completion(requirement, changes_summary):
            continue
        missing.append(requirement.action)
    return tuple(dict.fromkeys(missing))


def _requirements(
    value: str | None,
    *,
    skip_external: bool = False,
) -> tuple[_OperationalRequirement, ...]:
    requirements: list[_OperationalRequirement] = []
    for criterion in split_validation_criteria(value):
        if skip_external and is_external_criterion(criterion):
            continue
        for action, pattern in _OPERATIONAL_REQUIREMENTS.items():
            for match in pattern.finditer(criterion):
                if _is_ruled_out(criterion, match) or (
                    match.lastgroup == "nominal" and not _is_nominal_requirement(criterion, match)
                ):
                    continue
                requirements.append(
                    _OperationalRequirement(
                        action=action, subjects=frozenset(_subjects(match.group(0)))
                    )
                )
    return tuple(requirements)


def _is_nominal_requirement(criteria: str, match: re.Match[str]) -> bool:
    """Return whether a nominal action phrase is itself an acceptance demand."""
    suffix = criteria[match.end() :]
    if _NOMINAL_FOLLOWER_RE.match(suffix) is None:
        return False
    stripped = criteria.strip().rstrip(".:;")
    if stripped.casefold() == match.group(0).casefold():
        return True
    return _NOMINAL_REQUIREMENT_RE.search(criteria) is not None


def _is_ruled_out(criteria: str, match: re.Match[str]) -> bool:
    """Return whether a matched operational phrase names something to avoid.

    Criteria promise an operation will not happen as readily as they demand it, and
    both name it. Without reading the polarity around the match, a criterion such as
    "clears without a daemon restart" registers a restart as required and the close
    gate then demands evidence of the very thing the criterion ruled out.

    Matching is per-occurrence, not per-action: criteria that rule one operation out
    while demanding another keep demanding the one they asked for.
    """
    prefix = criteria[max(0, match.start() - _NEGATION_WINDOW) : match.start()]
    if _NEGATED_REQUIREMENT_PREFIX_RE.search(prefix) is not None:
        return True
    suffix = criteria[match.end() : match.end() + _NEGATION_WINDOW]
    return _NEGATED_REQUIREMENT_SUFFIX_RE.match(suffix) is not None


def _subjects(value: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (match.group("generic") or match.group(0)).casefold()
            for match in _OPERATIONAL_SUBJECT_RE.finditer(value)
        )
    )


def _evidence_marker(action: str, subjects: Iterable[str]) -> str:
    normalized_subjects = sorted({subject.strip().casefold() for subject in subjects if subject})
    return f"{action}:{','.join(normalized_subjects)}" if normalized_subjects else action


def _parse_evidence_markers(markers: Iterable[str]) -> dict[str, frozenset[str]]:
    evidence: dict[str, set[str]] = {}
    for raw_marker in markers:
        action, separator, raw_subjects = str(raw_marker).strip().casefold().partition(":")
        if action not in _OPERATIONAL_REQUIREMENTS:
            continue
        subjects = evidence.setdefault(action, set())
        if separator:
            subjects.update(subject for subject in raw_subjects.split(",") if subject)
    return {action: frozenset(subjects) for action, subjects in evidence.items()}


def _has_affirmative_completion(
    requirement: _OperationalRequirement,
    changes_summary: str,
) -> bool:
    """Return whether one positive, non-negated completion claim is present."""
    for match in _COMPLETED_OPERATIONAL_EVIDENCE[requirement.action].finditer(changes_summary):
        completion_start = (
            match.start("smoke_verb") if match.lastgroup == "smoke_verb" else match.start()
        )
        prefix = changes_summary[max(0, completion_start - 32) : completion_start]
        if _NEGATED_COMPLETION_PREFIX_RE.search(prefix) is not None:
            continue
        if match.lastgroup == "tool_native":
            return True
        evidence_window = _sentence_window(changes_summary, match)
        evidence_subjects = frozenset(_subjects(evidence_window))
        if not requirement.subjects or requirement.subjects & evidence_subjects:
            return True
    return False


def _sentence_window(value: str, match: re.Match[str]) -> str:
    """Return the sentence containing a completion match."""
    start = 0
    for prior_boundary in _SENTENCE_BOUNDARY_RE.finditer(value, 0, match.start()):
        start = prior_boundary.end()
    next_boundary = _SENTENCE_BOUNDARY_RE.search(value, match.end())
    end = next_boundary.start() if next_boundary is not None else len(value)
    return value[start:end]
