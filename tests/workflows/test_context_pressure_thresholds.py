"""Absolute-token context-pressure bands, cadence, and the set_handoff exit gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.workflows.observer_context_usage import (
    HANDOFF_RESULT_VARIABLE,
    PRESSURE_BAND_VARIABLE,
    SHOWN_KINDS_VARIABLE,
    SOFT_NUDGE_COUNTER_VARIABLE,
    detect_context_compact_guidance,
    detect_mid_turn_context_compact_guidance,
)

pytestmark = pytest.mark.unit

SESSION_ID = "session-1"
SOFT_150K = "Context is 150k tokens. Consider gobby-sessions:set_handoff with a concise structured handoff at the next pause."
STRONG_400K = (
    "Context is 400k tokens. Call gobby-sessions:set_handoff now, before any other tool call."
)
FAILED_400K = (
    "Context is 400k tokens. set_handoff could not compact (tmux target x is not live). "
    "Hand off manually or run the CLI's own compact command."
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


def _set_handoff_event(tool_output: Any) -> HookEvent:
    return _tool_event(
        {
            "tool_name": "mcp__gobby__call_tool",
            "mcp_server": "gobby-sessions",
            "mcp_tool": "set_handoff",
            "tool_input": {
                "server_name": "gobby-sessions",
                "tool_name": "set_handoff",
                "arguments": {"current_state": "x", "next_steps": ["y"]},
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


def _messages_over_tools(
    variables: dict[str, Any], manager: _SessionManager, count: int
) -> list[int]:
    return [index for index in range(1, count + 1) if _after_tool(variables, manager)]


@pytest.mark.parametrize("window", [None, 258_400, 200_000], ids=["no-window", "codex", "200k"])
@pytest.mark.parametrize(
    ("used", "expected_kind"),
    [
        (127_999, ""),
        (128_000, "soft"),
        (255_999, "soft"),
        (256_000, "strong"),
    ],
)
def test_turn_start_bands_are_absolute_and_ignore_the_window(
    window: int | None, used: int, expected_kind: str
) -> None:
    variables = _variables()

    _turn_start(variables, _SessionManager(used, window))

    assert variables["context_compact_guidance_kind"] == expected_kind


def test_actual_one_million_opus_occupancy_keeps_compact_guidance_inactive() -> None:
    variables = _variables()

    _turn_start(variables, _SessionManager(125_071, 1_000_000))

    assert variables["context_compact_guidance_kind"] == ""


def test_soft_guidance_is_emitted_once_per_epoch_at_turn_start() -> None:
    variables = _variables()
    manager = _SessionManager(150_000)

    assert _turn_start(variables, manager) == SOFT_150K
    assert variables["context_compact_guidance_kind"] == "soft"

    assert _next_turn(variables, manager) == ""
    assert variables[SHOWN_KINDS_VARIABLE] == ["soft"]


def test_strong_guidance_refires_on_every_turn_start() -> None:
    variables = _variables()
    manager = _SessionManager(400_000)

    messages = [_turn_start(variables, manager)] + [
        _next_turn(variables, manager) for _ in range(2)
    ]

    assert messages == [STRONG_400K] * 3
    assert variables["context_compact_guidance_kind"] == "strong"
    assert variables[SHOWN_KINDS_VARIABLE] == ["strong"]


def test_strong_guidance_refires_on_every_after_tool() -> None:
    variables = _variables()
    manager = _SessionManager(400_000)
    _turn_start(variables, manager)

    messages = [_after_tool(variables, manager) for _ in range(3)]

    assert messages == [STRONG_400K] * 3
    assert variables.get(SOFT_NUDGE_COUNTER_VARIABLE, 0) == 0


def test_strong_after_soft_suppresses_a_later_turn_start_soft() -> None:
    variables = _variables()
    manager = _SessionManager(150_000)

    assert _turn_start(variables, manager) == SOFT_150K

    manager.session.context_used_tokens = 300_000
    assert _next_turn(variables, manager).startswith("Context is 300k tokens. Call ")
    assert variables[SHOWN_KINDS_VARIABLE] == ["soft", "strong"]

    manager.session.context_used_tokens = 200_000
    assert _next_turn(variables, manager) == ""


def test_soft_cadence_emits_on_crossing_and_every_fifth_tool() -> None:
    variables = _variables()
    manager = _SessionManager(150_000)

    assert _messages_over_tools(variables, manager, 12) == [1, 5, 10]
    assert variables[SOFT_NUDGE_COUNTER_VARIABLE] == 12
    assert variables[PRESSURE_BAND_VARIABLE] == "soft"

    _after_tool(variables, manager)
    _after_tool(variables, manager)
    assert _after_tool(variables, manager) == SOFT_150K
    assert variables["context_compact_guidance_kind"] == "soft"


def test_soft_cadence_after_turn_start_soft_skips_the_crossing() -> None:
    variables = _variables()
    manager = _SessionManager(150_000)
    assert _turn_start(variables, manager) == SOFT_150K

    assert _messages_over_tools(variables, manager, 10) == [5, 10]
    assert variables[SOFT_NUDGE_COUNTER_VARIABLE] == 10


@pytest.mark.parametrize(
    "tool_output",
    [
        {"success": True, "result": {"compacted": True, "session_id": SESSION_ID}},
        {"compacted": True, "session_id": SESSION_ID},
    ],
    ids=["proxy-envelope", "flat-result"],
)
def test_compacted_handoff_suppresses_guidance_until_the_next_turn(tool_output: Any) -> None:
    variables = _variables()
    manager = _SessionManager(400_000)
    assert _turn_start(variables, manager) == STRONG_400K
    assert _after_tool(variables, manager) == STRONG_400K

    assert _after_tool(variables, manager, _set_handoff_event(tool_output)) == ""
    assert variables[HANDOFF_RESULT_VARIABLE] == {"compacted": True, "reason": None}
    assert [_after_tool(variables, manager) for _ in range(3)] == ["", "", ""]

    assert _next_turn(variables, manager) == STRONG_400K
    assert variables[HANDOFF_RESULT_VARIABLE] is None
    assert _after_tool(variables, manager) == STRONG_400K


def test_failed_handoff_demotes_strong_to_the_five_tool_cadence() -> None:
    variables = _variables()
    manager = _SessionManager(400_000)
    assert _turn_start(variables, manager) == STRONG_400K

    failure = {
        "success": True,
        "result": {"compacted": False, "reason": "tmux target x is not live"},
    }
    assert _after_tool(variables, manager, _set_handoff_event(failure)) == FAILED_400K
    assert variables["context_compact_guidance_kind"] == "strong"
    assert variables[HANDOFF_RESULT_VARIABLE] == {
        "compacted": False,
        "reason": "tmux target x is not live",
    }

    assert _messages_over_tools(variables, manager, 10) == [5, 10]
    assert variables["context_compact_guidance_message"] == FAILED_400K
    assert variables[SOFT_NUDGE_COUNTER_VARIABLE] == 10

    assert _next_turn(variables, manager) == STRONG_400K
    assert _after_tool(variables, manager) == STRONG_400K


def test_failed_handoff_in_the_soft_band_uses_the_failure_copy() -> None:
    variables = _variables()
    manager = _SessionManager(150_000)
    assert _turn_start(variables, manager) == SOFT_150K

    failure = {"success": True, "result": {"compacted": False, "reason": "no pane"}}
    message = _after_tool(variables, manager, _set_handoff_event(failure))

    assert message == (
        "Context is 150k tokens. set_handoff could not compact (no pane). "
        "Hand off manually or run the CLI's own compact command."
    )
    assert variables["context_compact_guidance_kind"] == "soft"
    assert _messages_over_tools(variables, manager, 5) == [5]


@pytest.mark.parametrize(
    "tool_output",
    [
        {"success": False, "error": "validation failed"},
        {"success": True, "result": {"cleared": True}},
        "not a dict",
        None,
    ],
    ids=["proxy-error", "no-compacted-key", "string", "missing"],
)
def test_set_handoff_without_a_compacted_verdict_leaves_strong_firing(tool_output: Any) -> None:
    variables = _variables()
    manager = _SessionManager(400_000)
    _turn_start(variables, manager)

    assert _after_tool(variables, manager, _set_handoff_event(tool_output)) == STRONG_400K
    assert variables[HANDOFF_RESULT_VARIABLE] is None


def test_occupancy_below_soft_resets_pressure_state() -> None:
    variables = _variables()
    manager = _SessionManager(150_000)
    _turn_start(variables, manager)
    _messages_over_tools(variables, manager, 3)
    variables[HANDOFF_RESULT_VARIABLE] = {"compacted": False, "reason": "x"}

    manager.session.context_used_tokens = 100_000
    assert _after_tool(variables, manager) == ""

    assert variables[PRESSURE_BAND_VARIABLE] == "none"
    assert variables[SHOWN_KINDS_VARIABLE] == []
    assert variables[SOFT_NUDGE_COUNTER_VARIABLE] == 0
    assert variables[HANDOFF_RESULT_VARIABLE] is None

    manager.session.context_used_tokens = 150_000
    assert _after_tool(variables, manager) == SOFT_150K


def test_turn_start_below_soft_resets_pressure_state() -> None:
    variables = _variables(
        **{
            PRESSURE_BAND_VARIABLE: "strong",
            SHOWN_KINDS_VARIABLE: ["soft", "strong"],
            SOFT_NUDGE_COUNTER_VARIABLE: 7,
        }
    )

    assert _turn_start(variables, _SessionManager(90_000)) == ""

    assert variables[PRESSURE_BAND_VARIABLE] == "none"
    assert variables[SHOWN_KINDS_VARIABLE] == []
    assert variables[SOFT_NUDGE_COUNTER_VARIABLE] == 0


def test_unknown_guidance_is_emitted_once() -> None:
    variables = _variables(parent_turn_seq=9, turns_since_compact=9)
    manager = _SessionManager(None)

    _turn_start(variables, manager)
    assert variables["context_compact_guidance_kind"] == "unknown"
    assert variables[SHOWN_KINDS_VARIABLE] == ["unknown"]

    assert _next_turn(variables, manager) == ""


def test_unknown_occupancy_mid_turn_is_silent() -> None:
    variables = _variables(**{PRESSURE_BAND_VARIABLE: "strong"})

    assert _after_tool(variables, _SessionManager(None)) == ""
    assert variables[PRESSURE_BAND_VARIABLE] == "strong"


def test_plan_mode_returns_before_turn_accounting() -> None:
    variables = _variables(parent_turn_seq=8, chat_mode="plan", turns_since_compact=4)

    assert _turn_start(variables, _SessionManager(900_000, 1_000_000)) == ""
    assert variables["turns_since_compact"] == 4


@pytest.mark.parametrize(
    "overrides",
    [{"pending_context_reset": True}, {"chat_mode": "plan"}, {"plan_mode": True}],
    ids=["pending-reset", "plan-chat-mode", "plan-mode"],
)
def test_mid_turn_reset_conditions_clear_pressure_state(overrides: dict[str, Any]) -> None:
    variables = _variables(
        **overrides,
        **{
            PRESSURE_BAND_VARIABLE: "strong",
            SHOWN_KINDS_VARIABLE: ["strong"],
            SOFT_NUDGE_COUNTER_VARIABLE: 4,
            HANDOFF_RESULT_VARIABLE: {"compacted": False, "reason": "x"},
        },
    )

    assert _after_tool(variables, _SessionManager(400_000)) == ""

    assert variables[PRESSURE_BAND_VARIABLE] == "none"
    assert variables[SHOWN_KINDS_VARIABLE] == []
    assert variables[SOFT_NUDGE_COUNTER_VARIABLE] == 0
    assert variables[HANDOFF_RESULT_VARIABLE] is None
