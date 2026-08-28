"""Task validation-criteria invariants and deterministic criterion splitting."""

from __future__ import annotations

import re
from collections.abc import Iterable

_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<text>.+?)\s*$")

_OPERATIONAL_REQUIREMENTS = {
    "install": re.compile(
        r"\b(?:install(?:ed|ing)?|installation)\s+(?:the\s+)?"
        r"(?:release|binary|artifact|package|build|executable|service|plugin|skill)\b|"
        r"\b(?:release|binary|artifact|package|build|executable|service|plugin|skill)\b"
        r"(?:\s+[\w.-]+){0,4}\s+(?:is\s+|was\s+)?installed\b",
        re.IGNORECASE,
    ),
    "restart": re.compile(
        r"\brestart(?:ed|ing)?\s+(?:the\s+)?(?:daemon|service|server|app(?:lication)?)\b|"
        r"\b(?:daemon|service|server|app(?:lication)?)\s+(?:is\s+|was\s+)?restarted\b",
        re.IGNORECASE,
    ),
    "smoke": re.compile(
        r"\b(?:run|perform|execute|complete)\s+(?:a\s+)?(?:live[- ]+)?"
        r"smoke(?:[- ]+(?:tests?|checks?|probes?))?\b|"
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
    normalized = normalized_validation_criteria(value)
    if normalized is None:
        return ()
    return tuple(
        action
        for action, pattern in _OPERATIONAL_REQUIREMENTS.items()
        if pattern.search(normalized)
    )


def operational_actions_from_command(command: str) -> tuple[str, ...]:
    """Return operational actions evidenced by one successful shell command."""
    return tuple(
        action for action, pattern in _OPERATIONAL_COMMANDS.items() if pattern.search(command)
    )


def missing_operational_evidence(
    validation_criteria: str | None,
    changes_summary: str,
    *,
    transcript_actions: Iterable[str] = (),
) -> tuple[str, ...]:
    """Return required operational actions lacking affirmative completion evidence."""
    required = required_operational_actions(validation_criteria)
    if not required:
        return ()
    completed_from_transcript = {str(action).strip().casefold() for action in transcript_actions}
    return tuple(
        action
        for action in required
        if action not in completed_from_transcript
        and not _has_affirmative_completion(action, changes_summary)
    )


def _has_affirmative_completion(action: str, changes_summary: str) -> bool:
    """Return whether one positive, non-negated completion claim is present."""
    for match in _COMPLETED_OPERATIONAL_EVIDENCE[action].finditer(changes_summary):
        prefix = changes_summary[max(0, match.start() - 32) : match.start()]
        if _NEGATED_COMPLETION_PREFIX_RE.search(prefix) is None:
            return True
    return False
