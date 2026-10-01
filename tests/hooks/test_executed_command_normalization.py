"""Executed commands stay consistent across normalization and rule selectors."""

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.adapters.codex_impl.item_normalization import build_tool_event_data
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.workflows.definitions import RuleEffect
from gobby.workflows.engine.effects import EffectsMixin

pytestmark = pytest.mark.unit


def _after_tool_event(data: dict[str, Any]) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="command-normalization-test",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data,
    )


@pytest.mark.parametrize("tools", [None, ["Bash"]])
def test_codex_completed_item_selectors_match_inner_command(tools: list[str] | None) -> None:
    data = build_tool_event_data(
        {
            "type": "commandExecution",
            "id": "command-1",
            "command": "/bin/zsh -lc 'git status'",
            "aggregatedOutput": "On branch main",
            "status": "completed",
            "exitCode": 0,
        }
    )
    event = _after_tool_event(data)
    matcher = EffectsMixin()
    inner_selector = RuleEffect(
        type="block", reason="inner command", tools=tools, command_pattern=r"^git status$"
    )
    wrapper_selector = RuleEffect(
        type="block", reason="shell wrapper", tools=tools, command_pattern=r"^/bin/zsh -lc"
    )

    assert matcher._effect_matches_event(inner_selector, event)
    assert not matcher._effect_matches_event(wrapper_selector, event)
    assert data["command"] == "git status"


@pytest.mark.parametrize("command_field", ["command", "cmd", "CommandLine"])
@pytest.mark.parametrize("executed_command", ["git status", ""])
def test_tool_input_command_replaces_stale_top_level_command(
    command_field: str, executed_command: str
) -> None:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "command": "git reset --hard",
        "tool_input": {command_field: executed_command},
    }

    normalize_tool_fields(data)

    assert data["command"] == executed_command
    assert data["tool_input"]["command"] == executed_command


def test_command_only_event_keeps_selector_fallback() -> None:
    data = normalize_tool_fields({"tool_name": "Bash", "command": "git status"})
    effect = RuleEffect(type="block", reason="command only", command_pattern=r"^git status$")

    assert data["command"] == "git status"
    assert EffectsMixin()._effect_matches_event(effect, _after_tool_event(data))


def test_executed_command_normalization_is_idempotent() -> None:
    data = normalize_tool_fields(
        {"tool_name": "Bash", "command": "git reset --hard", "tool_input": {"cmd": "git status"}}
    )
    first_pass = deepcopy(data)

    second_pass = normalize_tool_fields(data)

    assert second_pass == first_pass
    assert second_pass["command"] == "git status"
    assert second_pass["_raw_tool_input"] == {"cmd": "git status"}


def test_decoded_tool_input_command_replaces_stale_top_level_command() -> None:
    data = normalize_tool_fields(
        {
            "tool_name": "Bash",
            "command": "git reset --hard",
            "tool_input": '{"command":"git status"}',
        }
    )

    assert data["command"] == "git status"
    assert data["tool_input"]["command"] == "git status"
