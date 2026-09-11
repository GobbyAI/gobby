"""Configurable context-pressure thresholds, cadence, and handoff state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.workflows.observer_context_usage import (
    BLOCK_MESSAGE_VARIABLE,
    HANDOFF_RESULT_VARIABLE,
    HANDOFF_UNAVAILABLE_VARIABLE,
    PRESSURE_BAND_VARIABLE,
    TOOL_CALLS_SINCE_NUDGE_VARIABLE,
    UNKNOWN_ANNOUNCED_VARIABLE,
    _pressure_band,
    _thresholds,
    detect_context_compact_guidance,
    detect_mid_turn_context_compact_guidance,
)

pytestmark = pytest.mark.unit

SESSION_ID = "session-1"


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
        metadata={"_platform_session_id": SESSION_ID},
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
            "tool_outcome": {"status": "succeeded"},
        }
    )


def _get_handoff_event() -> HookEvent:
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
            "tool_output": {"success": True, "result": {"found": True}},
            "tool_outcome": {"status": "succeeded"},
        }
    )


def _variables(**overrides: Any) -> dict[str, Any]:
    variables: dict[str, Any] = {"parent_turn_seq": 0, "chat_mode": "normal"}
    variables.update(overrides)
    return variables


def _turn_start(
    variables: dict[str, Any],
    manager: _SessionManager,
    config: Any | None = None,
) -> str:
    detect_context_compact_guidance(variables, SESSION_ID, manager, config=config)
    return str(variables["context_compact_guidance_message"])


def _after_tool(
    variables: dict[str, Any],
    manager: _SessionManager,
    event: HookEvent | None = None,
    config: Any | None = None,
) -> str:
    detect_mid_turn_context_compact_guidance(
        event or _tool_event(), variables, SESSION_ID, manager, config=config
    )
    return str(variables["context_compact_guidance_message"])


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (None, (200_000, 256_000, 5)),
        (0, (200_000, 256_000, 5)),
        (255_999, (128_000, 191_999, 5)),
        (256_000, (200_000, 256_000, 5)),
        (258_400, (200_000, 256_000, 5)),
        (499_999, (200_000, 256_000, 5)),
        (500_000, (250_000, 300_000, 5)),
        (1_000_000, (250_000, 300_000, 5)),
    ],
)
def test_threshold_matrix_uses_window_classes(
    window: int | None,
    expected: tuple[int, int, int],
) -> None:
    assert _thresholds(None, window) == expected


def test_threshold_overrides_reconfigure_every_field_and_boundary() -> None:
    config = SimpleNamespace(
        warn_tokens=90_000,
        block_tokens=180_000,
        small_window_tokens=300_000,
        small_window_warn_ratio=0.25,
        small_window_block_ratio=0.75,
        extended_window_tokens=600_000,
        extended_warn_tokens=350_000,
        extended_block_tokens=400_000,
        warn_every_tool_calls=3,
    )
    assert _thresholds(config, None) == (90_000, 180_000, 3)
    assert _thresholds(config, 256_000) == (64_000, 192_000, 3)
    assert _thresholds(config, 300_000) == (90_000, 180_000, 3)
    assert _thresholds(config, 599_999) == (90_000, 180_000, 3)
    assert _thresholds(config, 600_000) == (350_000, 400_000, 3)


@pytest.mark.parametrize(
    ("window", "warn_threshold", "block_threshold"),
    [
        (128_000, 64_000, 96_000),
        (None, 200_000, 256_000),
        (256_000, 200_000, 256_000),
        (500_000, 250_000, 300_000),
    ],
)
def test_pressure_bands_change_exactly_at_selected_thresholds(
    window: int | None,
    warn_threshold: int,
    block_threshold: int,
) -> None:
    warn, block, _every = _thresholds(None, window)
    assert (warn, block) == (warn_threshold, block_threshold)
    assert _pressure_band(warn - 1, warn, block) == "none"
    assert _pressure_band(warn, warn, block) == "warn"
    assert _pressure_band(block - 1, warn, block) == "warn"
    assert _pressure_band(block, warn, block) == "block"


@pytest.mark.parametrize(
    ("used", "expected_band"),
    [(250_000, "warn"), (300_000, "block")],
)
def test_turn_start_repeats_guidance_and_writes_block_message_only_in_block(
    used: int,
    expected_band: str,
) -> None:
    variables = _variables()
    manager = _SessionManager(used, 1_000_000)

    first = _turn_start(variables, manager)
    variables["parent_turn_seq"] = 1
    second = _turn_start(variables, manager)

    assert first
    assert second
    assert variables[PRESSURE_BAND_VARIABLE] == expected_band
    assert bool(variables[BLOCK_MESSAGE_VARIABLE]) is (expected_band == "block")
    if expected_band == "block":
        assert 'get_tool_schema(server_name="gobby-sessions"' in second
        assert "observations=[]" in second


def test_after_tool_warns_on_crossing_then_every_configured_calls() -> None:
    variables = _variables()
    manager = _SessionManager(250_000, 1_000_000)

    assert _after_tool(variables, manager)
    assert variables[TOOL_CALLS_SINCE_NUDGE_VARIABLE] == 0
    for expected_counter in range(1, 5):
        assert _after_tool(variables, manager) == ""
        assert variables[TOOL_CALLS_SINCE_NUDGE_VARIABLE] == expected_counter
    assert _after_tool(variables, manager)
    assert variables[TOOL_CALLS_SINCE_NUDGE_VARIABLE] == 0


def test_after_tool_block_band_announces_every_call() -> None:
    variables = _variables()
    manager = _SessionManager(300_000, 1_000_000)

    assert _after_tool(variables, manager)
    assert _after_tool(variables, manager)
    assert variables[PRESSURE_BAND_VARIABLE] == "block"
    assert variables[TOOL_CALLS_SINCE_NUDGE_VARIABLE] == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {HANDOFF_RESULT_VARIABLE: {"delivery_pending": True}},
        {"pending_context_reset": True},
        {"plan_mode": True},
    ],
)
def test_mid_turn_suppression_clears_band_message_and_counter(overrides: dict[str, Any]) -> None:
    variables = _variables(
        **{
            PRESSURE_BAND_VARIABLE: "block",
            BLOCK_MESSAGE_VARIABLE: "blocked",
            TOOL_CALLS_SINCE_NUDGE_VARIABLE: 4,
            **overrides,
        }
    )

    assert _after_tool(variables, _SessionManager(300_000, 1_000_000)) == ""
    assert variables[PRESSURE_BAND_VARIABLE] == "none"
    assert variables[BLOCK_MESSAGE_VARIABLE] == ""
    assert variables[TOOL_CALLS_SINCE_NUDGE_VARIABLE] == 0


def test_boundary_turn_start_never_recomputes_stale_usage() -> None:
    variables = _variables(
        **{
            HANDOFF_RESULT_VARIABLE: {"delivery_pending": True},
            PRESSURE_BAND_VARIABLE: "block",
            BLOCK_MESSAGE_VARIABLE: "blocked",
        }
    )

    assert _turn_start(variables, _SessionManager(260_000, 1_000_000)) == ""
    assert variables[PRESSURE_BAND_VARIABLE] == "none"
    assert variables[BLOCK_MESSAGE_VARIABLE] == ""
    assert variables[HANDOFF_RESULT_VARIABLE] is None


def test_missing_usage_writes_none_band() -> None:
    variables = _variables(**{PRESSURE_BAND_VARIABLE: "block", BLOCK_MESSAGE_VARIABLE: "blocked"})

    assert _after_tool(variables, _SessionManager(None, 1_000_000)) == ""
    assert variables[PRESSURE_BAND_VARIABLE] == "none"
    assert variables[BLOCK_MESSAGE_VARIABLE] == ""


def test_non_retryable_handoff_failure_caps_epoch_at_warn() -> None:
    variables = _variables()
    manager = _SessionManager(300_000, 1_000_000)
    failed = _set_handoff_event(
        {
            "success": True,
            "result": {
                "compacted": False,
                "reason": "no terminal target",
                "error_code": "terminal_target_unavailable",
            },
        }
    )

    assert "could not compact" in _after_tool(variables, manager, failed)
    assert variables[PRESSURE_BAND_VARIABLE] == "warn"
    assert variables[HANDOFF_UNAVAILABLE_VARIABLE] is True
    assert variables[BLOCK_MESSAGE_VARIABLE] == ""
    assert _after_tool(variables, manager) == ""
    assert variables[PRESSURE_BAND_VARIABLE] == "warn"


def test_background_delivery_failure_keeps_block_band() -> None:
    variables = _variables(
        **{
            HANDOFF_RESULT_VARIABLE: {
                "delivery_failed": True,
                "retry_guidance": "Retry set_handoff",
            }
        }
    )

    assert _after_tool(variables, _SessionManager(300_000, 1_000_000))
    assert variables[PRESSURE_BAND_VARIABLE] == "block"
    assert variables[BLOCK_MESSAGE_VARIABLE]
    assert variables.get(HANDOFF_UNAVAILABLE_VARIABLE) is not True

    assert _turn_start(variables, _SessionManager(300_000, 1_000_000))
    assert variables["context_compact_guidance_kind"] == "failed"
    assert variables[HANDOFF_RESULT_VARIABLE]["delivery_failed"] is True
    assert variables[PRESSURE_BAND_VARIABLE] == "block"


def test_retryable_handoff_failure_keeps_block_band() -> None:
    variables = _variables()
    result = {
        "compacted": False,
        "reason": "Terminal dispatch failed.",
        "error_code": "dispatch_failed",
    }

    assert _after_tool(
        variables,
        _SessionManager(300_000, 1_000_000),
        _set_handoff_event(result),
    )
    assert variables[PRESSURE_BAND_VARIABLE] == "block"
    assert variables[BLOCK_MESSAGE_VARIABLE]
    assert variables.get(HANDOFF_UNAVAILABLE_VARIABLE) is not True


def test_unknown_guidance_is_once_per_epoch() -> None:
    variables = _variables(turns_since_compact=9)
    manager = _SessionManager(None)

    assert "unknown for 10" in _turn_start(variables, manager)
    variables["parent_turn_seq"] = 1
    assert _turn_start(variables, manager) == ""
    assert variables[UNKNOWN_ANNOUNCED_VARIABLE] is True

    _after_tool(variables, manager, _get_handoff_event())
    variables["turns_since_compact"] = 9
    variables["parent_turn_seq"] = 0
    assert "unknown for 10" in _turn_start(variables, manager)


def test_get_handoff_resets_band_counter_and_epoch_flags() -> None:
    variables = _variables(
        **{
            PRESSURE_BAND_VARIABLE: "block",
            BLOCK_MESSAGE_VARIABLE: "blocked",
            TOOL_CALLS_SINCE_NUDGE_VARIABLE: 3,
            HANDOFF_UNAVAILABLE_VARIABLE: True,
            UNKNOWN_ANNOUNCED_VARIABLE: True,
        }
    )

    _after_tool(variables, _SessionManager(None), _get_handoff_event())

    assert variables[PRESSURE_BAND_VARIABLE] == "none"
    assert variables[TOOL_CALLS_SINCE_NUDGE_VARIABLE] == 0
    assert variables[BLOCK_MESSAGE_VARIABLE] == ""
    assert variables[HANDOFF_UNAVAILABLE_VARIABLE] is False
    assert variables[UNKNOWN_ANNOUNCED_VARIABLE] is False


def test_plan_mode_returns_before_turn_accounting() -> None:
    variables = _variables(plan_mode=True)

    assert _turn_start(variables, _SessionManager(300_000, 1_000_000)) == ""
    assert "turns_since_compact" not in variables
    assert variables[PRESSURE_BAND_VARIABLE] == "none"
    assert variables[BLOCK_MESSAGE_VARIABLE] == ""
