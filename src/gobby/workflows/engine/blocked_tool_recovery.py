"""Blocked-tool retry recovery state and messages."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from typing import Any

from gobby.hooks.events import HookEventType

logger = logging.getLogger(__name__)

CONSECUTIVE_TOOL_BLOCK_RULE = "consecutive-tool-block"
_CODE_INDEX_REMEDIATION_RULES = {
    "require-code-index-skill",
    "prefer-gcode-for-code-search",
    "prefer-gcode-for-source-read",
}
_RULE_REASON_RE = re.compile(r"^Rule enforced by Gobby: \[([^\]]+)\]")
_ACTION_WORD_RE = re.compile(
    r"\b(?:ask|call|claim|clear|continue|create|load|retrieve|retry|run|use)\b",
    re.IGNORECASE,
)
_CALL_FORM_RE = re.compile(
    r"\b(?:call_tool|get_tool_schema|set_variable|[a-z_]+_(?:memory|review|stage|task))\s*\("
)
_BACKTICK_FORM_RE = re.compile(r"`[^`\n]+`")


def extract_rule_name(reason: str | None) -> str | None:
    """Extract rule name from a standard Gobby block reason prefix."""
    if not reason:
        return None
    match = _RULE_REASON_RE.match(reason)
    if not match:
        return None
    return match.group(1)


def recovery_directive_suffix(reason: str) -> str:
    """Lift complete actionable directives from a verbose block reason."""
    text = _RULE_REASON_RE.sub("", reason, count=1).strip()
    action_candidates: list[str] = []
    for candidate in _directive_sentences(text):
        has_call = _CALL_FORM_RE.search(candidate) is not None
        has_backtick = _BACKTICK_FORM_RE.search(candidate) is not None
        has_action = _ACTION_WORD_RE.search(candidate) is not None
        if not has_action and not has_call and not has_backtick:
            continue
        if has_call and not _calls_are_complete(candidate):
            continue
        action_candidates.append(candidate)

    if not action_candidates:
        return ""
    one_line = " ".join(" ".join(candidate.split()) for candidate in action_candidates)
    return f"\nRecovery directive: {one_line}"


def _directive_sentences(reason: str) -> list[str]:
    sentences: list[str] = []
    start = 0
    parenthesis_depth = 0
    in_backticks = False
    for index, char in enumerate(reason):
        if char == "`":
            in_backticks = not in_backticks
        elif not in_backticks:
            if char == "(":
                parenthesis_depth += 1
            elif char == ")" and parenthesis_depth:
                parenthesis_depth -= 1
            elif char in ".?!" and parenthesis_depth == 0:
                sentences.append(reason[start : index + 1].strip())
                start = index + 1
    tail = reason[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def _calls_are_complete(directive: str) -> bool:
    return all(
        _balanced_call_end(directive, match.start()) is not None
        for match in _CALL_FORM_RE.finditer(directive)
    )


def _balanced_call_end(text: str, start: int) -> int | None:
    open_at = text.find("(", start)
    if open_at < 0:
        return None
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(open_at, len(text)):
        char = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def block_source_for_rule(rule_name: str) -> str:
    """Map block rule names onto observability source labels."""
    if rule_name in {"agent-tool-enforcement", "step-tool-enforcement"}:
        return "step-enforcement"
    return "rule"


def block_reason_signature(rule_name: str, reason: str) -> str:
    digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:16]
    return f"{rule_name}:{digest}"


def format_aggregated_block_reason(
    gates: Sequence[tuple[str, str]],
    *,
    tool_name: str | None = None,
) -> str:
    """Build a compact multi-gate block reason."""
    lines = [f"Rule enforced by Gobby: [aggregated:{len(gates)}-gates]"]
    retry_target = (tool_name or "").strip()
    if retry_target and retry_target != "-":
        lines.append(f"Multiple gates blocked while retrying {retry_target}.")
    for index, (rule_name, reason) in enumerate(gates, start=1):
        compact_reason = _compact_gate_reason(reason)
        lines.append(f"{index}. [{rule_name}] {compact_reason}")
    return "\n".join(lines)


def ensure_block_reason(
    *,
    session_id: str,
    event_type: HookEventType | str,
    tool_name: str,
    source: str,
    rule_name: str,
    reason: str | None,
    fallback_reason: str,
    warn_detail: str,
) -> str:
    """Return a non-empty block reason, warning when fallback text is required."""
    cleaned = (reason or "").strip()
    if cleaned:
        return cleaned
    _warn_block_fallback(
        session_id=session_id,
        event_type=event_type,
        tool_name=tool_name,
        source=source,
        rule_name=rule_name,
        detail=warn_detail,
    )
    return fallback_reason


def log_block(
    *,
    session_id: str,
    event_type: HookEventType | str,
    tool_name: str,
    source: str,
    rule_name: str,
    reason: str,
) -> None:
    """Emit structured block log for observability and downstream debugging."""
    logger.debug(
        "BLOCK session=%s event=%s tool=%s source=%s rule=%s reason=%s",
        session_id,
        _event_value(event_type),
        tool_name or "-",
        source,
        rule_name,
        reason,
    )


def remember_blocked_tool_recovery_state(
    variables: dict[str, Any],
    *,
    tool_name: str,
    rule_name: str,
    reason: str,
) -> None:
    """Persist the latest original BEFORE_TOOL block for terminal retry guidance."""
    variables["_last_blocked_tool"] = tool_name
    variables["_last_blocked_rule_name"] = rule_name
    variables["_last_blocked_reason"] = reason


def clear_blocked_tool_recovery_state(variables: dict[str, Any]) -> None:
    """Clear stored BEFORE_TOOL block recovery state."""
    variables["_last_blocked_tool"] = ""
    variables["_last_blocked_rule_name"] = ""
    variables["_last_blocked_reason"] = ""


def is_blocked_tool_recovery_remediation(
    variables: dict[str, Any],
    event_data: dict[str, Any],
) -> bool:
    """Return true when the current tool directly satisfies the last block's remediation."""
    return (
        variables.get("_last_blocked_rule_name") in _CODE_INDEX_REMEDIATION_RULES
        and event_data.get("canonical_code_index_navigation") is True
    )


def format_consecutive_tool_block_reason(
    *,
    tool_name: str,
    total_attempts: int,
    variables: dict[str, Any],
) -> str:
    """Build the terminal same-tool retry guard reason."""
    prior_reason = variables.get("_last_blocked_reason")
    if isinstance(prior_reason, str) and prior_reason.strip():
        prior_section = f"Most recent original block reason:\n{prior_reason.strip()}"
    else:
        prior_section = (
            "The prior block reason was unavailable. Scroll up and read the immediately "
            "preceding tool error, then follow its requested remediation before retrying."
        )

    return (
        f"Rule enforced by Gobby: [{CONSECUTIVE_TOOL_BLOCK_RULE}]\n"
        f"You have attempted {tool_name} {total_attempts} times consecutively after "
        "repeated BEFORE_TOOL blocks.\n\n"
        f"{prior_section}\n\n"
        "Recovery required: read the most recent original block reason, follow that rule's "
        "requested remediation before retrying, use a different tool only when it directly "
        "performs that remediation, and do not stop/end the turn until remediation has "
        "been attempted."
    )


def _warn_block_fallback(
    *,
    session_id: str,
    event_type: HookEventType | str,
    tool_name: str,
    source: str,
    rule_name: str,
    detail: str,
) -> None:
    """Emit a warning when block handling has to synthesize a reason."""
    logger.warning(
        "BLOCK fallback session=%s event=%s tool=%s source=%s rule=%s detail=%s",
        session_id,
        _event_value(event_type),
        tool_name or "-",
        source,
        rule_name,
        detail,
    )


def _compact_gate_reason(reason: str) -> str:
    compact = " ".join(line.strip() for line in reason.splitlines() if line.strip())
    return re.sub(r"\s+", " ", compact).strip() or "No reason supplied."


def _event_value(event_type: HookEventType | str) -> str:
    if isinstance(event_type, HookEventType):
        return event_type.value
    return str(event_type)
