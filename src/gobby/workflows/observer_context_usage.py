"""Configurable context-pressure guidance and handoff enforcement state."""

from __future__ import annotations

import logging
from typing import Any, Literal, Protocol

from psycopg.errors import QueryCanceled

from gobby.hooks.events import HookEvent
from gobby.hooks.tool_outcomes import tool_outcome_from_data
from gobby.sessions.handoff import HANDOFF_DISPATCH_GATE_VARIABLE
from gobby.storage.hub.operation_deadline import DatabaseOperationDeadlineExceeded

logger = logging.getLogger(__name__)

DEFAULT_WARN_TOKENS = 200_000
DEFAULT_BLOCK_TOKENS = 256_000
DEFAULT_SMALL_WINDOW_TOKENS = 256_000
DEFAULT_SMALL_WINDOW_WARN_RATIO = 0.50
DEFAULT_SMALL_WINDOW_BLOCK_RATIO = 0.75
DEFAULT_EXTENDED_WINDOW_TOKENS = 500_000
DEFAULT_EXTENDED_WARN_TOKENS = 250_000
DEFAULT_EXTENDED_BLOCK_TOKENS = 300_000
DEFAULT_WARN_EVERY_TOOL_CALLS = 5
UNKNOWN_USAGE_TURN_FALLBACK = 10

HANDOFF_RESULT_VARIABLE = HANDOFF_DISPATCH_GATE_VARIABLE
PRESSURE_BAND_VARIABLE = "context_compact_mid_turn_pressure_band"
TOOL_CALLS_SINCE_NUDGE_VARIABLE = "context_compact_tool_calls_since_nudge"
BLOCK_MESSAGE_VARIABLE = "context_compact_block_message"
HANDOFF_UNAVAILABLE_VARIABLE = "context_compact_handoff_unavailable"
UNKNOWN_ANNOUNCED_VARIABLE = "context_compact_unknown_announced"

_NON_RETRYABLE_HANDOFF_ERROR_CODES = frozenset(
    {
        "terminal_target_unavailable",
        "terminal_target_not_live",
        "unsupported_session_type",
        "web_chat_registry_unavailable",
        "no_compaction_command",
        "interrupt_observation_unavailable",
    }
)

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


class _ContextHandoffConfig(Protocol):
    warn_tokens: int
    block_tokens: int
    small_window_tokens: int
    small_window_warn_ratio: float
    small_window_block_ratio: float
    extended_window_tokens: int
    extended_warn_tokens: int
    extended_block_tokens: int
    warn_every_tool_calls: int


def detect_context_compact_guidance(
    variables: dict[str, Any],
    session_id: str,
    session_manager: _SessionManager | None,
    config: _ContextHandoffConfig | None = None,
) -> None:
    """Populate compact guidance variables for turn_start evaluation."""
    gate = _handoff_gate(variables)
    skip_session_lookup = (
        gate in {"pending", "failed"}
        or variables.get("pending_context_reset") is True
        or _is_plan_mode(variables)
    )
    # A timed-out read must leave the previous guidance and turn counters intact.
    session = None if skip_session_lookup else _load_session(session_manager, session_id)
    variables["context_compact_guidance_kind"] = ""
    variables["context_compact_guidance_message"] = ""

    if gate == "pending" or variables.get("pending_context_reset") is True:
        _reset_epoch_state(variables)
        return
    if gate == "failed":
        result = variables.get(HANDOFF_RESULT_VARIABLE)
        if isinstance(result, dict) and isinstance(result.get("retry_guidance"), str):
            variables["context_compact_guidance_kind"] = "failed"
            variables["context_compact_guidance_message"] = result["retry_guidance"]
        if isinstance(result, dict) and result.get("delivery_failed") is True:
            return
        variables[HANDOFF_RESULT_VARIABLE] = None
        return

    if _is_plan_mode(variables):
        _write_band(variables, "none", None, None)
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

    used = _used_tokens_from_session(session)
    if used is None:
        _write_band(variables, "none", None, None)
        if (
            turns_since_compact >= UNKNOWN_USAGE_TURN_FALLBACK
            and variables.get(UNKNOWN_ANNOUNCED_VARIABLE) is not True
        ):
            variables["context_compact_guidance_kind"] = "unknown"
            variables["context_compact_guidance_message"] = UNKNOWN_USAGE_MESSAGE
            variables[UNKNOWN_ANNOUNCED_VARIABLE] = True
        return

    window = _int_or_none(getattr(session, "context_window", None)) if session else None
    warn_threshold, block_threshold, _every = _thresholds(config, window)
    band = _pressure_band(used, warn_threshold, block_threshold)
    if variables.get(HANDOFF_UNAVAILABLE_VARIABLE) is True and band == "block":
        band = "warn"
    _write_band(variables, band, used, (warn_threshold, block_threshold))
    if band == "none":
        return
    variables["context_compact_guidance_kind"] = band
    variables["context_compact_guidance_message"] = _guidance_message(
        band, used, warn_threshold, block_threshold
    )


def detect_mid_turn_context_compact_guidance(
    event: HookEvent,
    variables: dict[str, Any],
    session_id: str,
    session_manager: _SessionManager | None,
    config: _ContextHandoffConfig | None = None,
) -> None:
    """Populate compact guidance for an after_tool event under context pressure."""
    variables["context_compact_guidance_kind"] = ""
    variables["context_compact_guidance_message"] = ""

    _record_handoff_result(event, variables)

    gate = _handoff_gate(variables)
    if (
        variables.get("pending_context_reset") is True
        or _is_plan_mode(variables)
        or gate == "pending"
    ):
        _write_band(variables, "none", None, None)
        variables[TOOL_CALLS_SINCE_NUDGE_VARIABLE] = 0
        return

    session = _load_session(session_manager, session_id)
    used = _used_tokens_from_session(session)
    if used is None:
        _write_band(variables, "none", None, None)
        return

    window = _int_or_none(getattr(session, "context_window", None)) if session else None
    warn_threshold, block_threshold, every = _thresholds(config, window)
    previous_band = str(variables.get(PRESSURE_BAND_VARIABLE) or "none")
    band = _pressure_band(used, warn_threshold, block_threshold)
    message: str | None = None

    result = variables.get(HANDOFF_RESULT_VARIABLE)
    delivery_failed = isinstance(result, dict) and result.get("delivery_failed") is True
    if gate == "failed" and isinstance(result, dict) and result.get("compacted") is False:
        if not delivery_failed:
            error_code = result.get("error_code")
            if isinstance(error_code, str) and error_code in _NON_RETRYABLE_HANDOFF_ERROR_CODES:
                variables[HANDOFF_UNAVAILABLE_VARIABLE] = True
                band = "warn" if band == "block" else band
            message = _failed_handoff_message(used, variables)
            variables[HANDOFF_RESULT_VARIABLE] = None
    if variables.get(HANDOFF_UNAVAILABLE_VARIABLE) is True and band == "block":
        band = "warn"

    _write_band(variables, band, used, (warn_threshold, block_threshold))
    if band == "none":
        variables[TOOL_CALLS_SINCE_NUDGE_VARIABLE] = 0
        return

    counter = (_int_or_none(variables.get(TOOL_CALLS_SINCE_NUDGE_VARIABLE), default=0) or 0) + 1
    variables[TOOL_CALLS_SINCE_NUDGE_VARIABLE] = counter
    band_rose = (previous_band, band) in {("none", "warn"), ("none", "block"), ("warn", "block")}
    announce = band_rose or counter >= every or band == "block" or message is not None
    if not announce:
        return
    variables[TOOL_CALLS_SINCE_NUDGE_VARIABLE] = 0
    variables["context_compact_guidance_kind"] = band
    variables["context_compact_guidance_message"] = message or _guidance_message(
        band, used, warn_threshold, block_threshold
    )


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
        error_code = payload.get("error_code")
        variables[HANDOFF_RESULT_VARIABLE] = {
            "compacted": compacted,
            "reason": reason if isinstance(reason, str) and reason else None,
            "error_code": error_code if isinstance(error_code, str) and error_code else None,
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
    variables[TOOL_CALLS_SINCE_NUDGE_VARIABLE] = 0
    variables[BLOCK_MESSAGE_VARIABLE] = ""
    variables[HANDOFF_UNAVAILABLE_VARIABLE] = False
    variables[UNKNOWN_ANNOUNCED_VARIABLE] = False
    variables[HANDOFF_RESULT_VARIABLE] = None


def _load_session(
    session_manager: _SessionManager | None,
    session_id: str,
) -> _SessionValue | None:
    if session_manager is None or not session_id:
        return None
    try:
        return session_manager.get(session_id)
    except (DatabaseOperationDeadlineExceeded, QueryCanceled):
        raise
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


def _thresholds(
    config: _ContextHandoffConfig | None,
    window: int | None,
) -> tuple[int, int, int]:
    """Return warn, block, and repeat cadence for the current window class."""
    warn_tokens = _positive_int(getattr(config, "warn_tokens", None), DEFAULT_WARN_TOKENS)
    block_tokens = _positive_int(getattr(config, "block_tokens", None), DEFAULT_BLOCK_TOKENS)
    small_window_tokens = _positive_int(
        getattr(config, "small_window_tokens", None), DEFAULT_SMALL_WINDOW_TOKENS
    )
    extended_window_tokens = _positive_int(
        getattr(config, "extended_window_tokens", None), DEFAULT_EXTENDED_WINDOW_TOKENS
    )
    every = _positive_int(
        getattr(config, "warn_every_tool_calls", None), DEFAULT_WARN_EVERY_TOOL_CALLS
    )
    if window is None or window <= 0:
        return warn_tokens, block_tokens, every
    if window < small_window_tokens:
        warn_ratio = _positive_float(
            getattr(config, "small_window_warn_ratio", None), DEFAULT_SMALL_WINDOW_WARN_RATIO
        )
        block_ratio = _positive_float(
            getattr(config, "small_window_block_ratio", None), DEFAULT_SMALL_WINDOW_BLOCK_RATIO
        )
        return round(window * warn_ratio), round(window * block_ratio), every
    if window >= extended_window_tokens:
        extended_warn_tokens = _positive_int(
            getattr(config, "extended_warn_tokens", None), DEFAULT_EXTENDED_WARN_TOKENS
        )
        extended_block_tokens = _positive_int(
            getattr(config, "extended_block_tokens", None), DEFAULT_EXTENDED_BLOCK_TOKENS
        )
        return extended_warn_tokens, extended_block_tokens, every
    return warn_tokens, block_tokens, every


def _write_band(
    variables: dict[str, Any],
    band: str,
    used: int | None,
    thresholds: tuple[int, int] | None,
) -> None:
    variables[PRESSURE_BAND_VARIABLE] = band
    variables[BLOCK_MESSAGE_VARIABLE] = (
        _block_message(used, *thresholds)
        if band == "block" and used is not None and thresholds is not None
        else ""
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


def _pressure_band(used: int, warn_threshold: int, block_threshold: int) -> str:
    if used >= block_threshold:
        return "block"
    if used >= warn_threshold:
        return "warn"
    return "none"


def _guidance_message(band: str, used: int, warn: int, block: int) -> str:
    if band == "block":
        return _block_message(used, warn, block)
    return (
        f"Context is {_format_tokens(used)} tokens (warn {_format_tokens(warn)}; "
        f"block {_format_tokens(block)}). Prepare a concise structured handoff. "
        "This warning repeats every turn and at the configured tool-call cadence."
    )


def _block_message(used: int, warn: int, block: int) -> str:
    return (
        f"Context is {_format_tokens(used)} tokens (warn {_format_tokens(warn)}; "
        f"block {_format_tokens(block)}). Tool use is blocked until a handoff compacts "
        'the session. Call get_tool_schema(server_name="gobby-sessions", '
        'tool_name="feedback"), then gobby-sessions:feedback with '
        "observations=[] when there is nothing to report. Then call set_handoff last "
        "with a bounded handoff."
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


def _positive_int(value: Any, default: int) -> int:
    parsed = _int_or_none(value)
    return parsed if parsed is not None and parsed > 0 else default


def _positive_float(value: Any, default: float) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
