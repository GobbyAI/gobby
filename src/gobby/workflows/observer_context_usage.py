"""Context-pressure observer for compact guidance.

Pressure is measured in absolute resident tokens (``session.context_used_tokens``)
against two window-independent cuts. The soft band asks the agent to consider a
handoff on a K-tool cadence; the strong band demands one on every event until a
``gobby-sessions:set_handoff`` result gates the loop or a compaction resets it.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Protocol

from gobby.hooks.events import HookEvent

logger = logging.getLogger(__name__)

SOFT_CONTEXT_TOKENS = 128_000
STRONG_CONTEXT_TOKENS = 256_000
SOFT_NUDGE_EVERY_TOOLS = 5
UNKNOWN_USAGE_TURN_FALLBACK = 10
GUIDANCE_KINDS = frozenset({"soft", "strong", "unknown"})

SOFT_NUDGE_COUNTER_VARIABLE = "context_compact_soft_nudge_tools"
HANDOFF_RESULT_VARIABLE = "context_compact_handoff_result"
SHOWN_KINDS_VARIABLE = "context_compact_guidance_shown_kinds"
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
    variables[HANDOFF_RESULT_VARIABLE] = None

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
    used = _used_tokens_from_session(session)
    if used is None:
        if turns_since_compact >= UNKNOWN_USAGE_TURN_FALLBACK:
            _set_guidance(variables, "unknown", UNKNOWN_USAGE_MESSAGE, once=True)
        return

    band = _pressure_band(used)
    variables[PRESSURE_BAND_VARIABLE] = band
    if band == "none":
        _reset_pressure_state(variables)
        return

    if band == "strong":
        _set_guidance(variables, "strong", _strong_message(used), once=False)
        return

    _set_guidance(variables, "soft", _soft_message(used), once=True)


def detect_mid_turn_context_compact_guidance(
    event: HookEvent,
    variables: dict[str, Any],
    session_id: str,
    session_manager: _SessionManager | None,
) -> None:
    """Populate compact guidance for an after_tool event under context pressure."""
    variables["context_compact_guidance_kind"] = ""
    variables["context_compact_guidance_message"] = ""

    if variables.get("pending_context_reset") is True or _is_plan_mode(variables):
        _reset_pressure_state(variables)
        return

    just_failed = _record_handoff_result(event, variables)

    session = _load_session(session_manager, session_id)
    used = _used_tokens_from_session(session)
    if used is None:
        return

    previous_band = str(variables.get(PRESSURE_BAND_VARIABLE) or "none")
    band = _pressure_band(used)
    variables[PRESSURE_BAND_VARIABLE] = band
    if band == "none":
        _reset_pressure_state(variables)
        return

    gate = _handoff_gate(variables)
    if gate == "pending":
        return

    if band == "strong" and gate is None:
        _set_guidance(variables, "strong", _strong_message(used), once=False)
        return

    if just_failed:
        counter = 0
    else:
        counter = (_int_or_none(variables.get(SOFT_NUDGE_COUNTER_VARIABLE), default=0) or 0) + 1
    variables[SOFT_NUDGE_COUNTER_VARIABLE] = counter

    crossed = previous_band == "none" and band == "soft"
    if not (just_failed or crossed or counter % SOFT_NUDGE_EVERY_TOOLS == 0):
        return

    if gate == "failed":
        _set_guidance(variables, band, _failed_handoff_message(used, variables), once=False)
        return
    _set_guidance(variables, "soft", _soft_message(used), once=False)


def _record_handoff_result(event: HookEvent, variables: dict[str, Any]) -> bool:
    """Store the ``set_handoff`` outcome from *event*; return True on a fresh failure."""
    data = event.data or {}
    if data.get("mcp_server") != "gobby-sessions" or data.get("mcp_tool") != "set_handoff":
        return False
    payload: Any = data.get("tool_output")
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        payload = payload["result"]
    if not isinstance(payload, dict):
        return False
    compacted = payload.get("compacted")
    if not isinstance(compacted, bool):
        return False
    reason = payload.get("reason")
    variables[HANDOFF_RESULT_VARIABLE] = {
        "compacted": compacted,
        "reason": reason if isinstance(reason, str) and reason else None,
    }
    return not compacted


def _handoff_gate(variables: dict[str, Any]) -> _HandoffGate:
    result = variables.get(HANDOFF_RESULT_VARIABLE)
    if not isinstance(result, dict) or "compacted" not in result:
        return None
    return "pending" if result.get("compacted") is True else "failed"


def _reset_pressure_state(variables: dict[str, Any]) -> None:
    variables[PRESSURE_BAND_VARIABLE] = "none"
    variables[SHOWN_KINDS_VARIABLE] = []
    variables[SOFT_NUDGE_COUNTER_VARIABLE] = 0
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


def _set_guidance(
    variables: dict[str, Any],
    kind: str,
    message: str,
    *,
    once: bool,
) -> None:
    shown_kinds = _shown_guidance_kinds(variables)
    if once and (kind in shown_kinds or (kind == "soft" and "strong" in shown_kinds)):
        return
    variables["context_compact_guidance_kind"] = kind
    variables["context_compact_guidance_message"] = message
    if kind not in shown_kinds:
        shown_kinds.append(kind)
    variables[SHOWN_KINDS_VARIABLE] = shown_kinds


def _shown_guidance_kinds(variables: dict[str, Any]) -> list[str]:
    raw_kinds = variables.get(SHOWN_KINDS_VARIABLE)
    if not isinstance(raw_kinds, list):
        return []
    return list(
        dict.fromkeys(
            kind for kind in raw_kinds if isinstance(kind, str) and kind in GUIDANCE_KINDS
        )
    )


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


def _pressure_band(used: int) -> str:
    if used >= STRONG_CONTEXT_TOKENS:
        return "strong"
    if used >= SOFT_CONTEXT_TOKENS:
        return "soft"
    return "none"


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
