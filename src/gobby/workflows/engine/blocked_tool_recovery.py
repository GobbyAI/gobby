"""Blocked-tool retry recovery state and messages."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from gobby.hooks.events import HookEventType

logger = logging.getLogger(__name__)

CONSECUTIVE_TOOL_BLOCK_RULE = "consecutive-tool-block"
_RULE_REASON_RE = re.compile(r"^Rule enforced by Gobby: \[([^\]]+)\]")


def extract_rule_name(reason: str | None) -> str | None:
    """Extract rule name from a standard Gobby block reason prefix."""
    if not reason:
        return None
    match = _RULE_REASON_RE.match(reason)
    if not match:
        return None
    return match.group(1)


def block_source_for_rule(rule_name: str) -> str:
    """Map block rule names onto observability source labels."""
    if rule_name in {"agent-tool-enforcement", "step-tool-enforcement"}:
        return "step-enforcement"
    return "rule"


def block_reason_signature(rule_name: str, reason: str) -> str:
    digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:16]
    return f"{rule_name}:{digest}"


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
    logger.info(
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


def _event_value(event_type: HookEventType | str) -> str:
    if isinstance(event_type, HookEventType):
        return event_type.value
    return str(event_type)
