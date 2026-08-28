"""Context-pressure observer for compact guidance."""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DEFAULT_SOFT_CONTEXT_RATIO = 0.40
DEFAULT_STRONG_CONTEXT_RATIO = 0.70
LARGE_CONTEXT_SOFT_RATIO = 0.30
LARGE_CONTEXT_STRONG_RATIO = 0.40
LARGE_CONTEXT_WINDOW = 1_000_000
UNKNOWN_USAGE_TURN_FALLBACK = 10
GUIDANCE_KINDS = frozenset({"soft", "strong", "unknown"})


class _SessionValue(Protocol):
    @property
    def context_usage_ratio(self) -> object: ...

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
        if turns_since_compact >= UNKNOWN_USAGE_TURN_FALLBACK:
            _set_guidance(
                variables,
                "unknown",
                (
                    "Context usage has been unknown for 10 non-plan turns. "
                    "Call `gobby-sessions:set_handoff` with `clear_session=false` and a concise structured handoff at the next clean boundary."
                ),
            )
        return

    soft_ratio, strong_ratio = _thresholds_from_session(session)
    variables["context_compact_mid_turn_pressure_band"] = _pressure_band(
        ratio,
        soft_ratio,
        strong_ratio,
    )

    if ratio >= strong_ratio:
        _set_guidance(
            variables,
            "strong",
            (
                f"Context pressure is {_percent(ratio)}. "
                "Call `gobby-sessions:set_handoff` with `clear_session=false` and a concise structured handoff at the next clean boundary."
            ),
        )
        return

    if ratio >= soft_ratio:
        _set_guidance(
            variables,
            "soft",
            (
                f"Context pressure is {_percent(ratio)}. "
                "Consider calling `gobby-sessions:set_handoff` with `clear_session=false` and a concise structured handoff at the next natural pause "
                "in your work."
            ),
        )


def detect_mid_turn_context_compact_guidance(
    variables: dict[str, Any],
    session_id: str,
    session_manager: _SessionManager | None,
) -> None:
    """Populate compact guidance when context crosses a pressure band within a turn."""
    variables["context_compact_guidance_kind"] = ""
    variables["context_compact_guidance_message"] = ""

    if variables.get("pending_context_reset") is True:
        variables["context_compact_mid_turn_pressure_band"] = "none"
        variables["context_compact_guidance_shown_kinds"] = []
        return

    if _is_plan_mode(variables):
        variables["context_compact_mid_turn_pressure_band"] = "none"
        variables["context_compact_guidance_shown_kinds"] = []
        return

    session = _load_session(session_manager, session_id)
    ratio = _ratio_from_session(session)
    if ratio is None:
        return

    soft_ratio, strong_ratio = _thresholds_from_session(session)
    previous_band = str(variables.get("context_compact_mid_turn_pressure_band") or "none")
    current_band = _pressure_band(ratio, soft_ratio, strong_ratio)
    variables["context_compact_mid_turn_pressure_band"] = current_band
    if _pressure_band_rank(current_band) <= _pressure_band_rank(previous_band):
        return

    if current_band == "strong":
        _set_guidance(
            variables,
            "strong",
            (
                f"Context pressure is {_percent(ratio)}. "
                "Call `gobby-sessions:set_handoff` with `clear_session=false` and a concise structured handoff at the next clean boundary."
            ),
        )
        return

    _set_guidance(
        variables,
        "soft",
        (
            f"Context pressure is {_percent(ratio)}. "
            "Consider calling `gobby-sessions:set_handoff` with `clear_session=false` and a concise structured handoff at the next natural pause "
            "in your work."
        ),
    )


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


def _ratio_from_session(session: _SessionValue | None) -> float | None:
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


def _thresholds_from_session(session: _SessionValue | None) -> tuple[float, float]:
    window = _int_or_none(getattr(session, "context_window", None))
    if window is not None and window >= LARGE_CONTEXT_WINDOW:
        return LARGE_CONTEXT_SOFT_RATIO, LARGE_CONTEXT_STRONG_RATIO
    return DEFAULT_SOFT_CONTEXT_RATIO, DEFAULT_STRONG_CONTEXT_RATIO


def _set_guidance(
    variables: dict[str, Any],
    kind: str,
    message: str,
) -> None:
    shown_kinds = _shown_guidance_kinds(variables)
    if kind in shown_kinds or (kind == "soft" and "strong" in shown_kinds):
        return
    variables["context_compact_guidance_kind"] = kind
    variables["context_compact_guidance_message"] = message
    shown_kinds.append(kind)
    variables["context_compact_guidance_shown_kinds"] = shown_kinds


def _shown_guidance_kinds(variables: dict[str, Any]) -> list[str]:
    raw_kinds = variables.get("context_compact_guidance_shown_kinds")
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


def _pressure_band(ratio: float, soft_ratio: float, strong_ratio: float) -> str:
    if ratio >= strong_ratio:
        return "strong"
    if ratio >= soft_ratio:
        return "soft"
    return "none"


def _pressure_band_rank(band: str) -> int:
    return {"none": 0, "soft": 1, "strong": 2}.get(band, 0)


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
