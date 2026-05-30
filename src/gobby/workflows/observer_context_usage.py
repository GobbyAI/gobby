"""Context-pressure observer for compact guidance."""

from __future__ import annotations

from typing import Any

SOFT_CONTEXT_RATIO = 0.65
STRONG_CONTEXT_RATIO = 0.80
STRONG_NUDGE_COOLDOWN_TURNS = 2
UNKNOWN_USAGE_TURN_FALLBACK = 10


def detect_context_compact_guidance(
    variables: dict[str, Any],
    session_id: str,
    session_manager: Any | None,
) -> None:
    """Populate compact guidance variables for non-plan turn_start evaluation."""
    variables["context_compact_guidance_kind"] = ""
    variables["context_compact_guidance_message"] = ""

    if _is_plan_mode(variables):
        return

    turn_seq = _next_turn_seq(variables)
    last_compacted = _int_or_none(variables.get("last_compacted_turn_seq"))
    if last_compacted is not None:
        turns_since_compact = max(0, turn_seq - last_compacted)
    else:
        previous_turns_since_compact = (
            _int_or_none(variables.get("turns_since_compact"), default=0) or 0
        )
        turns_since_compact = previous_turns_since_compact + 1
    variables["turns_since_compact"] = turns_since_compact

    session = _load_session(session_manager, session_id)
    ratio = _ratio_from_session(session)
    if ratio is None:
        if turns_since_compact >= UNKNOWN_USAGE_TURN_FALLBACK and _cooldown_elapsed(
            variables,
            turn_seq,
            UNKNOWN_USAGE_TURN_FALLBACK,
        ):
            _set_guidance(
                variables,
                turn_seq,
                "unknown",
                (
                    "Context usage has been unknown for 10 non-plan turns. "
                    "Call `gobby-sessions:compact_self` at the next clean boundary."
                ),
            )
        return

    if ratio >= STRONG_CONTEXT_RATIO:
        if _cooldown_elapsed(variables, turn_seq, STRONG_NUDGE_COOLDOWN_TURNS):
            _set_guidance(
                variables,
                turn_seq,
                "strong",
                (
                    f"Context pressure is {_percent(ratio)}. "
                    "Call `gobby-sessions:compact_self` at the next clean boundary."
                ),
            )
        return

    if ratio >= SOFT_CONTEXT_RATIO and not variables.get("last_compact_nudge_turn_seq"):
        _set_guidance(
            variables,
            turn_seq,
            "soft",
            (
                f"Context pressure is {_percent(ratio)}. "
                "Plan a `gobby-sessions:compact_self` call for the next clean boundary."
            ),
        )


def _load_session(session_manager: Any | None, session_id: str) -> Any | None:
    if session_manager is None or not session_id:
        return None
    try:
        return session_manager.get(session_id)
    except Exception:
        return None


def _ratio_from_session(session: Any | None) -> float | None:
    if session is None:
        return None
    ratio = getattr(session, "context_usage_ratio", None)
    if isinstance(ratio, int | float) and not isinstance(ratio, bool):
        return _clamp(float(ratio))

    used = _int_or_none(getattr(session, "context_used_tokens", None))
    window = _int_or_none(getattr(session, "context_window", None))
    if used is None or window is None or window <= 0:
        return None
    return _clamp(used / window)


def _set_guidance(
    variables: dict[str, Any],
    turn_seq: int,
    kind: str,
    message: str,
) -> None:
    variables["context_compact_guidance_kind"] = kind
    variables["context_compact_guidance_message"] = message
    variables["last_compact_nudge_turn_seq"] = turn_seq


def _is_plan_mode(variables: dict[str, Any]) -> bool:
    if variables.get("plan_mode"):
        return True
    if variables.get("mode_level") == 0:
        return True
    return variables.get("chat_mode") == "plan"


def _next_turn_seq(variables: dict[str, Any]) -> int:
    parent_turn_seq = _int_or_none(variables.get("parent_turn_seq"))
    if parent_turn_seq is not None:
        return parent_turn_seq + 1
    previous = _int_or_none(variables.get("_context_usage_turn_seq"), default=0) or 0
    current = previous + 1
    variables["_context_usage_turn_seq"] = current
    return current


def _cooldown_elapsed(variables: dict[str, Any], turn_seq: int, cooldown: int) -> bool:
    last = _int_or_none(variables.get("last_compact_nudge_turn_seq"))
    return last is None or turn_seq - last >= cooldown


def _int_or_none(value: Any, default: int | None = None) -> int | None:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _percent(value: float) -> str:
    return f"{round(_clamp(value) * 100)}%"
