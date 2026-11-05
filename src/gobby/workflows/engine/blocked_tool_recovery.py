"""Blocked-tool retry recovery state and messages."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEventType, HookResponse
from gobby.workflows.engine.block_batching import close_response_batch
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

if TYPE_CHECKING:
    from gobby.storage.definitions.rules import RuleDefinitionRow
    from gobby.workflows.definitions import RuleDefinitionBody
    from gobby.workflows.engine.evaluation import EvaluationContext, EvaluationMixin

logger = logging.getLogger(__name__)

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
    """Split at ``.?!`` that end a sentence, never inside a path such as ``~/.gobby``."""
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
            elif (
                char in ".?!"
                and parenthesis_depth == 0
                and reason[index + 1 : index + 2].strip() == ""
            ):
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
    recovery_conditions: Sequence[str] = (),
) -> None:
    """Persist the latest original BEFORE_TOOL block for terminal retry guidance."""
    variables["_last_blocked_tool"] = tool_name
    variables["_last_blocked_rule_name"] = rule_name
    variables["_last_blocked_reason"] = reason
    variables["_last_blocked_recovery_conditions"] = list(recovery_conditions)


def clear_blocked_tool_recovery_state(variables: dict[str, Any]) -> None:
    """Clear stored BEFORE_TOOL block recovery state."""
    variables["_last_blocked_tool"] = ""
    variables["_last_blocked_rule_name"] = ""
    variables["_last_blocked_reason"] = ""
    variables["_last_blocked_recovery_conditions"] = []
    close_response_batch(variables)


def is_blocked_tool_recovery_remediation(
    variables: dict[str, Any],
    event_data: dict[str, Any],
) -> bool:
    """Return true when the current tool directly satisfies the last block's remediation."""
    conditions = variables.get("_last_blocked_recovery_conditions", [])
    if not isinstance(conditions, list):
        return False
    evaluator = SafeExpressionEvaluator({"event": event_data, "variables": variables}, {})
    for condition in conditions:
        if not isinstance(condition, str):
            continue
        try:
            if evaluator.evaluate(condition):
                return True
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            logger.warning("Invalid blocked-tool recovery condition", exc_info=True)
    return False


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


async def escalate_retry_limit(
    engine: EvaluationMixin,
    retry_rules: list[tuple[RuleDefinitionRow, RuleDefinitionBody]],
    evaluation: EvaluationContext,
    span: Any,
    *,
    aggregate_blocks: bool,
) -> HookResponse | None:
    """Return the retry-limit block for a repeated denial, or None to continue.

    Runs before agent, step and rule gates, so a looping enforcement block still
    escalates and no sibling side effect runs. The escalation is not a new original
    block, so the recovery state it quotes survives for the next one.
    """
    if not retry_rules:
        return None
    gates = await engine._run_rule_loop(
        retry_rules, evaluation, aggregate_blocks=aggregate_blocks, block_effects_only=True
    )
    if not gates:
        return None
    response = engine._assemble_response(
        evaluation, override_decision=None, override_reason=None, block_gates=gates
    )
    return await engine._finalize_block_response(
        response, evaluation, span, source="retry-limit", block_gates=gates
    )
