"""Context-pressure observer for compact guidance.

Known model windows use percentage pressure bands with an absolute soft cap.
Sessions without a usable window retain the absolute-token fallback. Each band
is announced once per context epoch, and a successful ``set_handoff`` silences
the remaining epoch.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Protocol

from gobby.hooks.events import HookEvent
from gobby.hooks.tool_outcomes import tool_outcome_from_data
from gobby.sessions.handoff import HANDOFF_DISPATCH_GATE_VARIABLE

logger = logging.getLogger(__name__)

FALLBACK_SOFT_CONTEXT_TOKENS = 128_000
FALLBACK_STRONG_CONTEXT_TOKENS = 256_000
ABSOLUTE_SOFT_CONTEXT_TOKENS = 200_000
DEFAULT_SOFT_CONTEXT_RATIO = 0.40
DEFAULT_STRONG_CONTEXT_RATIO = 0.70
LARGE_CONTEXT_SOFT_RATIO = 0.30
LARGE_CONTEXT_STRONG_RATIO = 0.40
LARGE_CONTEXT_WINDOW = 1_000_000
UNKNOWN_USAGE_TURN_FALLBACK = 10

HANDOFF_RESULT_VARIABLE = HANDOFF_DISPATCH_GATE_VARIABLE
HIGHEST_ANNOUNCED_THRESHOLD_VARIABLE = "context_compact_highest_announced_threshold"
PRESSURE_BAND_VARIABLE = "context_compact_mid_turn_pressure_band"

UNKNOWN_USAGE_MESSAGE = (
    "Context usage has been unknown for 10 non-plan turns. "
    "Call `gobby-sessions:set_handoff` with `clear_session=false` and a concise structured "
    "handoff at the next clean boundary."
)

_HandoffGate = Literal["pending", "failed"] | None


class _SessionValue(Protocol):
    @property
    def context_used_tokens(self) -> object: ...

    @property
    def context_window(self) -> object: ...


class _SessionManager(Protocol):
    def get(self, session_id: str) -> _SessionValue | None: ...


def detect_context_compact_guidance(
    variables: dict[str, Any],
    session_id: str,
    session_manager: _SessionManager | None,
) -> None:
    """Populate compact guidance variables for turn_start evaluation."""
    variables["context_compact_guidance_kind"] = ""
    variables["context_compact_guidance_message"] = ""

    gate = _handoff_gate(variables)
    retry_guidance: str | None = None
    if gate == "pending" or variables.get("pending_context_reset") is True:
        _reset_epoch_state(variables)
    elif gate == "failed":
        result = variables.get(HANDOFF_RESULT_VARIABLE)
        if isinstance(result, dict) and isinstance(result.get("retry_guidance"), str):
            retry_guidance = result["retry_guidance"]
        variables[HANDOFF_RESULT_VARIABLE] = None

    if _is_plan_mode(variables):
        return
    if retry_guidance:
        variables["context_compact_guidance_kind"] = "failed"
        variables["context_compact_guidance_message"] = retry_guidance
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
    used = _used_tokens_from_session(session)
    if used is None:
        if turns_since_compact >= UNKNOWN_USAGE_TURN_FALLBACK:
            _set_guidance(variables, "unknown", UNKNOWN_USAGE_MESSAGE)
        return

    soft_threshold, strong_threshold = _thresholds_from_session(session)
    band = _pressure_band(used, soft_threshold, strong_threshold)
    variables[PRESSURE_BAND_VARIABLE] = band
    if band == "none":
        return

    if band == "strong":
        _set_guidance(variables, "strong", _strong_message(used))
        return

    _set_guidance(variables, "soft", _soft_message(used))


def detect_mid_turn_context_compact_guidance(
    event: HookEvent,
    variables: dict[str, Any],
    session_id: str,
    session_manager: _SessionManager | None,
) -> None:
    """Populate compact guidance for an after_tool event under context pressure."""
    variables["context_compact_guidance_kind"] = ""
    variables["context_compact_guidance_message"] = ""

    _record_handoff_result(event, variables)

    if variables.get("pending_context_reset") is True or _is_plan_mode(variables):
        variables[PRESSURE_BAND_VARIABLE] = "none"
        return

    gate = _handoff_gate(variables)
    if gate == "pending":
        return

    session = _load_session(session_manager, session_id)
    used = _used_tokens_from_session(session)
    if used is None:
        return

    soft_threshold, strong_threshold = _thresholds_from_session(session)
    band = _pressure_band(used, soft_threshold, strong_threshold)
    variables[PRESSURE_BAND_VARIABLE] = band
    if band == "none":
        return

    if gate == "failed":
        _set_guidance(variables, band, _failed_handoff_message(used, variables))
        return

    message = _strong_message(used) if band == "strong" else _soft_message(used)
    _set_guidance(variables, band, message)


def _record_handoff_result(event: HookEvent, variables: dict[str, Any]) -> None:
    """Update epoch state from a terminal handoff tool outcome."""
    data = event.data or {}
    if data.get("mcp_server") != "gobby-sessions":
        return
    payload: Any = data.get("tool_output")
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        if payload.get("success") is not True:
            return
        payload = payload["result"]
    if not isinstance(payload, dict):
        return

    # The proxy strips the nested ``success`` key from sub-tool results; only the
    # envelope's ``success`` (checked above) is meaningful here (#21713).
    if data.get("mcp_tool") == "get_handoff":
        if payload.get("found") is True:
            _reset_epoch_state(variables)
        return
    if data.get("mcp_tool") != "set_handoff":
        return
    if tool_outcome_from_data(data).succeeded is not True:
        return

    if (
        payload.get("handoff_staged") is True
        and payload.get("delivery_pending") is True
        and isinstance(payload.get("attempt_id"), str)
        and bool(payload["attempt_id"])
        and isinstance(payload.get("session_id"), str)
        and payload["session_id"] == event.metadata.get("_platform_session_id")
        and isinstance(payload.get("clear_session"), bool)
    ):
        previous = variables.get(HANDOFF_RESULT_VARIABLE)
        if (
            isinstance(previous, dict)
            and previous.get("delivery_failed") is True
            and previous.get("attempt_id") == payload["attempt_id"]
        ):
            return
        variables[HANDOFF_RESULT_VARIABLE] = {
            "handoff_staged": True,
            "delivery_pending": True,
            "attempt_id": payload["attempt_id"],
            "clear_session": payload["clear_session"],
        }
        return

    compacted = payload.get("compacted")
    if isinstance(compacted, bool):
        reason = payload.get("reason")
        variables[HANDOFF_RESULT_VARIABLE] = {
            "compacted": compacted,
            "reason": reason if isinstance(reason, str) and reason else None,
        }
        return

    if _clear_session_requested(data) and _clear_attempt_is_pending(payload):
        reason = payload.get("reason") or payload.get("error")
        variables[HANDOFF_RESULT_VARIABLE] = {
            "compacted": None,
            "reason": reason if isinstance(reason, str) and reason else None,
            "attempt_pending": True,
        }


def _handoff_gate(variables: dict[str, Any]) -> _HandoffGate:
    result = variables.get(HANDOFF_RESULT_VARIABLE)
    if not isinstance(result, dict):
        return None
    if (
        result.get("compacted") is True
        or result.get("attempt_pending") is True
        or result.get("delivery_pending") is True
    ):
        return "pending"
    if result.get("compacted") is False or result.get("delivery_failed") is True:
        return "failed"
    return None


def _clear_session_requested(data: dict[str, Any]) -> bool:
    raw_input = data.get("tool_input")
    if not isinstance(raw_input, dict):
        return False
    raw_arguments = raw_input.get("arguments")
    arguments = raw_arguments if isinstance(raw_arguments, dict) else raw_input
    return arguments.get("clear_session") is True


def _clear_attempt_is_pending(payload: dict[str, Any]) -> bool:
    if payload.get("attempt_pending") is True:
        return True
    if payload.get("success") is not True and payload.get("queued") is not True:
        return False
    return any(
        payload.get(field) is True
        for field in ("handoff_staged", "command_sent", "queued", "cleared")
    )


def _reset_epoch_state(variables: dict[str, Any]) -> None:
    variables[PRESSURE_BAND_VARIABLE] = "none"
    variables[HIGHEST_ANNOUNCED_THRESHOLD_VARIABLE] = "none"
    variables[HANDOFF_RESULT_VARIABLE] = None


def _load_session(
    session_manager: _SessionManager | None,
    session_id: str,
) -> _SessionValue | None:
    if session_manager is None or not session_id:
        return None
    try:
        return session_manager.get(session_id)
    except Exception as exc:
        logger.debug("Failed to load session %s for context usage observer: %s", session_id, exc)
        return None


def _used_tokens_from_session(session: _SessionValue | None) -> int | None:
    if session is None:
        return None
    used = _int_or_none(getattr(session, "context_used_tokens", None))
    if used is None or used < 0:
        return None
    return used


def _thresholds_from_session(session: _SessionValue | None) -> tuple[int, int]:
    """Return soft and strong token thresholds for the session's model window."""
    if session is None:
        return FALLBACK_SOFT_CONTEXT_TOKENS, FALLBACK_STRONG_CONTEXT_TOKENS
    window = _int_or_none(getattr(session, "context_window", None))
    if window is None or window <= 0:
        return FALLBACK_SOFT_CONTEXT_TOKENS, FALLBACK_STRONG_CONTEXT_TOKENS
    if window >= LARGE_CONTEXT_WINDOW:
        soft_ratio, strong_ratio = LARGE_CONTEXT_SOFT_RATIO, LARGE_CONTEXT_STRONG_RATIO
    else:
        soft_ratio, strong_ratio = DEFAULT_SOFT_CONTEXT_RATIO, DEFAULT_STRONG_CONTEXT_RATIO
    soft_threshold = min(round(window * soft_ratio), ABSOLUTE_SOFT_CONTEXT_TOKENS)
    return soft_threshold, round(window * strong_ratio)


def _set_guidance(
    variables: dict[str, Any],
    kind: str,
    message: str,
) -> None:
    highest = str(variables.get(HIGHEST_ANNOUNCED_THRESHOLD_VARIABLE) or "none")
    if _pressure_band_rank(kind) <= _pressure_band_rank(highest):
        return
    variables["context_compact_guidance_kind"] = kind
    variables["context_compact_guidance_message"] = message
    variables[HIGHEST_ANNOUNCED_THRESHOLD_VARIABLE] = kind


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


def _pressure_band(used: int, soft_threshold: int, strong_threshold: int) -> str:
    if used >= strong_threshold:
        return "strong"
    if used >= soft_threshold:
        return "soft"
    return "none"


def _pressure_band_rank(band: str) -> int:
    return {"none": -1, "unknown": 0, "soft": 1, "strong": 2}.get(band, -1)


def _soft_message(used: int) -> str:
    return (
        f"Context is {_format_tokens(used)} tokens. Consider gobby-sessions:set_handoff "
        "with a concise structured handoff at the next pause."
    )


def _strong_message(used: int) -> str:
    return (
        f"Context is {_format_tokens(used)} tokens. Call gobby-sessions:set_handoff now, "
        "before any other tool call."
    )


def _failed_handoff_message(used: int, variables: dict[str, Any]) -> str:
    result = variables.get(HANDOFF_RESULT_VARIABLE)
    reason = result.get("reason") if isinstance(result, dict) else None
    detail = reason if isinstance(reason, str) and reason else "unknown reason"
    return (
        f"Context is {_format_tokens(used)} tokens. set_handoff could not compact ({detail}). "
        "Hand off manually or run the CLI's own compact command."
    )


def _format_tokens(used: int) -> str:
    return f"{round(used / 1000)}k"


def _int_or_none(value: Any, default: int | None = None) -> int | None:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
