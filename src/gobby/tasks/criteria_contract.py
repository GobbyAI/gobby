"""Task validation-criteria invariants and deterministic criterion splitting."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<text>.+?)\s*$")

_OPERATIONAL_REQUIREMENTS = {
    "install": re.compile(
        r"\b(?:install(?:ed|ing)?|installation)\s+(?:the\s+)?"
        r"(?:release|binary|artifact|package|build|executable|service|plugin|skill)\b|"
        r"\b(?:release|binary|artifact|package|build|executable|service|plugin|skill)\b"
        r"(?:\s+[\w.-]+){0,4}\s+(?:(?:is\s+|was\s+)?installed|installation)\b",
        re.IGNORECASE,
    ),
    "restart": re.compile(
        r"\brestart(?:ed|ing)?\s+(?:the\s+)?(?:daemon|service|server|app(?:lication)?)\b|"
        r"\b(?:daemon|service|server|app(?:lication)?)\s+"
        r"(?:(?:is\s+|was\s+)?restarted|restart)\b",
        re.IGNORECASE,
    ),
    "smoke": re.compile(
        r"\b(?:run|perform|execute|complete)\s+(?:a\s+)?(?:live[- ]+)?"
        r"smoke(?:[- ]+(?:tests?|checks?|probes?))?\b|"
        r"\blive[- ]+smoke[- ]+(?:tests?|checks?|probes?)\b|"
        r"\b(?:live[- ]+)?smoke(?:[- ]+(?:tests?|checks?|probes?))?\s+"
        r"(?:passes|passed|succeeds|succeeded|completes|completed|shows|show|verifies|verified)\b",
        re.IGNORECASE,
    ),
    "deploy": re.compile(
        r"\bdeploy(?:ed|ing)?\s+(?:the\s+)?(?:service|release|app(?:lication)?|site)\b|"
        r"\b(?:service|release|app(?:lication)?|site)\s+(?:is\s+|was\s+)?deployed\b",
        re.IGNORECASE,
    ),
    "publish": re.compile(
        r"\bpublish(?:ed|ing)?\s+(?:the\s+)?(?:release|package|artifact|site)\b|"
        r"\b(?:release|package|artifact|site)\s+(?:is\s+|was\s+)?published\b",
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
        r"\b(?:installed|install(?:ation)?\s+(?:completed|passed|succeeded|verified))\b",
        re.IGNORECASE,
    ),
    "restart": re.compile(
        r"\b(?:restarted|restart\s+(?:completed|passed|succeeded|healthy|verified))\b",
        re.IGNORECASE,
    ),
    "smoke": re.compile(
        r"\b(?:smoke-tested|smoke(?:[- ]+(?:tests?|checks?|probes?))?\s+"
        r"(?:completed|passed|succeeded|clean|verified))\b",
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

_OPERATIONAL_SUBJECT_RE = re.compile(
    r"\b(?:release|binary|artifact|package|build|executable|service|plugin|skill|"
    r"ghook|gcode|gwiki|gobby|daemon|server|app|application|site)\b",
    re.IGNORECASE,
)


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


def require_validation_criteria(task_type: str, value: str | None) -> str | None:
    """Enforce the criteria invariant and return the normalized value."""
    normalized = normalized_validation_criteria(value)
    if task_type != "epic" and normalized is None:
        raise TaskCriteriaError(
            "Every non-epic task requires nonempty validation_criteria. "
            "State observable completion evidence before creating or updating the task."
        )
    return normalized


def split_validation_criteria(value: str | None) -> tuple[str, ...]:
    """Split free-text criteria into stable, distinct criterion strings."""
    normalized = normalized_validation_criteria(value)
    if normalized is None:
        return ()

    lines = normalized.splitlines()
    items: list[str] = []
    current: list[str] = []
    saw_list_marker = False

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if current:
                items.append(" ".join(current))
                current = []
            continue
        match = _LIST_ITEM_RE.match(raw_line)
        if match is not None:
            if not saw_list_marker:
                items = []
                current = []
            saw_list_marker = True
            if current:
                items.append(" ".join(current))
            current = [match.group("text").strip()]
            continue
        current.append(line)

    if current:
        items.append(" ".join(current))

    if saw_list_marker:
        return tuple(item for item in items if item)

    paragraphs = tuple(item for item in items if item)
    return paragraphs or (normalized,)


def required_operational_actions(value: str | None) -> tuple[str, ...]:
    """Return operational actions explicitly named by acceptance criteria."""
    return tuple(dict.fromkeys(requirement.action for requirement in _requirements(value)))


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
            subjects.update(("gobby", "ghook", "gcode", "gwiki"))
        markers.append(_evidence_marker(action, subjects))
    return tuple(markers)


def missing_operational_evidence(
    validation_criteria: str | None,
    changes_summary: str,
    *,
    transcript_actions: Iterable[str] = (),
) -> tuple[str, ...]:
    """Return required operational actions lacking affirmative completion evidence."""
    requirements = _requirements(validation_criteria)
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


def _requirements(value: str | None) -> tuple[_OperationalRequirement, ...]:
    normalized = normalized_validation_criteria(value)
    if normalized is None:
        return ()
    requirements: list[_OperationalRequirement] = []
    for action, pattern in _OPERATIONAL_REQUIREMENTS.items():
        for match in pattern.finditer(normalized):
            requirements.append(
                _OperationalRequirement(
                    action=action, subjects=frozenset(_subjects(match.group(0)))
                )
            )
    return tuple(requirements)


def _subjects(value: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            match.group(0).casefold() for match in _OPERATIONAL_SUBJECT_RE.finditer(value)
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
        prefix = changes_summary[max(0, match.start() - 32) : match.start()]
        if _NEGATED_COMPLETION_PREFIX_RE.search(prefix) is not None:
            continue
        evidence_window = changes_summary[
            max(0, match.start() - 48) : min(len(changes_summary), match.end() + 48)
        ]
        evidence_subjects = frozenset(_subjects(evidence_window))
        if not requirement.subjects or requirement.subjects & evidence_subjects:
            return True
    return False
