"""Context-pressure thresholds, epoch deduplication, and the handoff gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.workflows.observer_context_usage import (
    FALLBACK_SOFT_CONTEXT_TOKENS,
    FALLBACK_STRONG_CONTEXT_TOKENS,
    HANDOFF_RESULT_VARIABLE,
    HIGHEST_ANNOUNCED_THRESHOLD_VARIABLE,
    PRESSURE_BAND_VARIABLE,
    _thresholds_from_session,
    detect_context_compact_guidance,
    detect_mid_turn_context_compact_guidance,
)

pytestmark = pytest.mark.unit

SESSION_ID = "session-1"
SOFT_100K = (
    "Context is 100k tokens. Consider gobby-sessions:set_handoff with a concise structured "
    "handoff at the next pause."
)
STRONG_150K = (
    "Context is 150k tokens. Call gobby-sessions:set_handoff now, before any other tool call."
)


@dataclass
class _Session:
    context_used_tokens: int | None
    context_window: int | None = None


class _SessionManager:
    def __init__(self, used: int | None, window: int | None = None) -> None:
        self.session = _Session(used, window)

    def get(self, _session_id: str) -> _Session:
        return self.session


def _tool_event(data: dict[str, Any] | None = None) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {"tool_name": "Read", "tool_input": {"file_path": "/repo/a.py"}},
        metadata={},
    )


def _set_handoff_event(tool_output: Any, *, clear_session: bool = False) -> HookEvent:
    return _tool_event(
        {
            "tool_name": "mcp__gobby__call_tool",
            "mcp_server": "gobby-sessions",
            "mcp_tool": "set_handoff",
            "tool_input": {
                "server_name": "gobby-sessions",
                "tool_name": "set_handoff",
                "arguments": {
                    "current_state": "x",
                    "next_steps": ["y"],
                    "clear_session": clear_session,
                },
            },
            "tool_output": tool_output,
            "tool_outcome": {"status": "succeeded"},
        }
    )


def _get_handoff_event(tool_output: Any) -> HookEvent:
    return _tool_event(
        {
            "tool_name": "mcp__gobby__call_tool",
            "mcp_server": "gobby-sessions",
            "mcp_tool": "get_handoff",
            "tool_input": {
                "server_name": "gobby-sessions",
                "tool_name": "get_handoff",
                "arguments": {},
            },
            "tool_output": tool_output,
        }
    )


def _variables(**overrides: Any) -> dict[str, Any]:
    variables: dict[str, Any] = {"parent_turn_seq": 0, "chat_mode": "normal"}
    variables.update(overrides)
    return variables


def _turn_start(variables: dict[str, Any], manager: _SessionManager) -> str:
    detect_context_compact_guidance(variables, SESSION_ID, manager)
    return str(variables["context_compact_guidance_message"])


def _next_turn(variables: dict[str, Any], manager: _SessionManager) -> str:
    variables["parent_turn_seq"] = int(variables["parent_turn_seq"]) + 1
    return _turn_start(variables, manager)


def _after_tool(
    variables: dict[str, Any],
    manager: _SessionManager,
    event: HookEvent | None = None,
) -> str:
    detect_mid_turn_context_compact_guidance(event or _tool_event(), variables, SESSION_ID, manager)
    return str(variables["context_compact_guidance_message"])


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        pytest.param(
            None,
            (FALLBACK_SOFT_CONTEXT_TOKENS, FALLBACK_STRONG_CONTEXT_TOKENS),
            id="unknown-window",
        ),
        pytest.param(
            0,
            (FALLBACK_SOFT_CONTEXT_TOKENS, FALLBACK_STRONG_CONTEXT_TOKENS),
            id="zero",
        ),
        pytest.param(200_000, (80_000, 140_000), id="standard-model"),
        pytest.param(1_000_000, (200_000, 400_000), id="large-model"),
    ],
)
def test_thresholds_use_model_window_with_absolute_fallback(
    window: int | None,
    expected: tuple[int, int],
) -> None:
    assert _thresholds_from_session(_Session(0, window)) == expected


@pytest.mark.parametrize(
    ("used", "window", "expected_kind"),
    [
        pytest.param(79_999, 200_000, "", id="below-window-soft"),
        pytest.param(80_000, 200_000, "soft", id="window-soft"),
        pytest.param(140_000, 200_000, "strong", id="window-strong"),
        pytest.param(250_000, 1_000_000, "soft", id="large-window-soft-cap"),
        pytest.param(400_000, 1_000_000, "strong", id="large-window-strong"),
        pytest.param(128_000, None, "soft", id="fallback-soft"),
        pytest.param(256_000, None, "strong", id="fallback-strong"),
    ],
)
def test_turn_start_uses_selected_thresholds(
    used: int,
    window: int | None,
    expected_kind: str,
) -> None:
    variables = _variables()

    _turn_start(variables, _SessionManager(used, window))

    assert variables["context_compact_guidance_kind"] == expected_kind


def test_large_window_uses_capped_soft_threshold_mid_turn() -> None:
    variables = _variables()
    manager = _SessionManager(250_000, 1_000_000)

    _after_tool(variables, manager)
    assert variables["context_compact_guidance_kind"] == "soft"

    manager.session.context_used_tokens = 400_000
    _after_tool(variables, manager)
    assert variables["context_compact_guidance_kind"] == "strong"


def test_guidance_fires_once_per_increasing_threshold_in_an_epoch() -> None:
    variables = _variables()
    manager = _SessionManager(100_000, 200_000)

    assert _turn_start(variables, manager) == SOFT_100K
    assert _next_turn(variables, manager) == ""
    assert _after_tool(variables, manager) == ""

    manager.session.context_used_tokens = 150_000
    assert _after_tool(variables, manager) == STRONG_150K
    assert _after_tool(variables, manager) == ""
    assert _next_turn(variables, manager) == ""
    assert variables[HIGHEST_ANNOUNCED_THRESHOLD_VARIABLE] == "strong"


def test_dropping_below_a_threshold_does_not_reannounce_it_in_the_same_epoch() -> None:
    variables = _variables()
    manager = _SessionManager(100_000, 200_000)
    assert _turn_start(variables, manager) == SOFT_100K

    manager.session.context_used_tokens = 50_000
    assert _after_tool(variables, manager) == ""
    assert variables[PRESSURE_BAND_VARIABLE] == "none"

    manager.session.context_used_tokens = 100_000
    assert _after_tool(variables, manager) == ""
    assert variables[HIGHEST_ANNOUNCED_THRESHOLD_VARIABLE] == "soft"


@pytest.mark.parametrize(
    "tool_output",
    [
        {"success": True, "result": {"compacted": True, "session_id": SESSION_ID}},
        {"compacted": True, "session_id": SESSION_ID},
    ],
    ids=["proxy-envelope", "flat-result"],
)
def test_compacted_handoff_suppresses_until_successor_turn_start(tool_output: Any) -> None:
    variables = _variables()
    manager = _SessionManager(150_000, 200_000)

    assert _after_tool(variables, manager, _set_handoff_event(tool_output)) == ""
    assert variables[HANDOFF_RESULT_VARIABLE] == {"compacted": True, "reason": None}
    assert [_after_tool(variables, manager) for _ in range(3)] == ["", "", ""]

    assert _next_turn(variables, manager) == STRONG_150K
    assert variables[HANDOFF_RESULT_VARIABLE] is None
    assert variables[HIGHEST_ANNOUNCED_THRESHOLD_VARIABLE] == "strong"


def test_get_handoff_consumption_starts_a_new_threshold_epoch() -> None:
    variables = _variables(
        **{
            HANDOFF_RESULT_VARIABLE: {"compacted": True, "reason": None},
            HIGHEST_ANNOUNCED_THRESHOLD_VARIABLE: "strong",
        }
    )
    manager = _SessionManager(150_000, 200_000)

    message = _after_tool(
        variables,
        manager,
        _get_handoff_event({"success": True, "result": {"found": True}}),
    )

    assert message == STRONG_150K
    assert variables[HANDOFF_RESULT_VARIABLE] is None
    assert variables[HIGHEST_ANNOUNCED_THRESHOLD_VARIABLE] == "strong"


@pytest.mark.parametrize(
    "tool_output",
    [
        {
            "success": False,
            "error": "timed out waiting for clear-session acknowledgment",
            "attempt_pending": True,
        },
        {"success": True, "handoff_staged": True, "command_sent": True},
        {"queued": True, "handoff_staged": True},
    ],
    ids=["timeout-pending", "acknowledged", "web-chat-queued"],
)
def test_pending_clear_session_attempt_suppresses_guidance(tool_output: Any) -> None:
    variables = _variables()
    manager = _SessionManager(150_000, 200_000)

    assert (
        _after_tool(
            variables,
            manager,
            _set_handoff_event(tool_output, clear_session=True),
        )
        == ""
    )
    assert variables[HANDOFF_RESULT_VARIABLE]["attempt_pending"] is True
    assert _after_tool(variables, manager) == ""


@pytest.mark.parametrize(
    ("tool_output", "expected"),
    [
        pytest.param(
            {"success": True, "result": {"compacted": False, "reason": "no pane"}},
            "set_handoff could not compact (no pane)",
            id="compaction-failed",
        ),
        pytest.param(
            {"success": False, "error": "validation failed"},
            "Call gobby-sessions:set_handoff now",
            id="tool-error",
        ),
    ],
)
def test_failed_handoff_leaves_guidance_enabled(tool_output: Any, expected: str) -> None:
    variables = _variables()
    manager = _SessionManager(150_000, 200_000)

    message = _after_tool(variables, manager, _set_handoff_event(tool_output))

    assert expected in message
    assert variables[HIGHEST_ANNOUNCED_THRESHOLD_VARIABLE] == "strong"
    assert _after_tool(variables, manager) == ""


def test_background_delivery_failure_surfaces_retry_guidance_on_next_turn() -> None:
    variables = _variables(
        **{
            HANDOFF_RESULT_VARIABLE: {
                "compacted": False,
                "delivery_failed": True,
                "retry_guidance": "Retry gobby-sessions:set_handoff now.",
            }
        }
    )

    message = _turn_start(variables, _SessionManager(10_000, 200_000))

    assert message == "Retry gobby-sessions:set_handoff now."
    assert variables[HANDOFF_RESULT_VARIABLE] is None


def test_unknown_guidance_is_emitted_once_per_epoch() -> None:
    variables = _variables(parent_turn_seq=9, turns_since_compact=9)
    manager = _SessionManager(None)

    _turn_start(variables, manager)
    assert variables["context_compact_guidance_kind"] == "unknown"
    assert variables[HIGHEST_ANNOUNCED_THRESHOLD_VARIABLE] == "unknown"
    assert _next_turn(variables, manager) == ""


def test_plan_mode_returns_before_turn_accounting() -> None:
    variables = _variables(parent_turn_seq=8, chat_mode="plan", turns_since_compact=4)

    assert _turn_start(variables, _SessionManager(900_000, 1_000_000)) == ""
    assert variables["turns_since_compact"] == 4


@pytest.mark.parametrize(
    "overrides",
    [{"pending_context_reset": True}, {"chat_mode": "plan"}, {"plan_mode": True}],
    ids=["pending-reset", "plan-chat-mode", "plan-mode"],
)
def test_mid_turn_suppression_preserves_epoch_markers(overrides: dict[str, Any]) -> None:
    handoff_result = {"compacted": True, "reason": None}
    variables = _variables(
        **overrides,
        **{
            PRESSURE_BAND_VARIABLE: "strong",
            HIGHEST_ANNOUNCED_THRESHOLD_VARIABLE: "strong",
            HANDOFF_RESULT_VARIABLE: handoff_result,
        },
    )

    assert _after_tool(variables, _SessionManager(400_000)) == ""
    assert variables[PRESSURE_BAND_VARIABLE] == "none"
    assert variables[HIGHEST_ANNOUNCED_THRESHOLD_VARIABLE] == "strong"
    assert variables[HANDOFF_RESULT_VARIABLE] == handoff_result
