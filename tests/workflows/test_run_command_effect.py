"""Tests for the run_command rule effect."""

import json
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.workflow_definitions import WorkflowDefinitionRow
from gobby.workflows.definitions import RuleEffect
from gobby.workflows.engine.effects import EffectsMixin, _extract_command_context

_ROW = cast(WorkflowDefinitionRow, SimpleNamespace(name="test-rule"))

# Reads the hook event JSON from stdin and echoes its tool name back through
# the Claude hook response shape — proves both stdin fidelity and extraction.
_ECHO_SCRIPT = (
    "import json, sys; "
    "event = json.load(sys.stdin); "
    "print(json.dumps({'hookSpecificOutput': "
    "{'additionalContext': 'saw ' + event['tool_name']}}))"
)


def _event(**data: Any) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="ext-1",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Edit", **data},
        cwd=None,
        metadata={"_platform_session_id": "platform-session-1"},
    )


def _effect(**overrides: Any) -> RuleEffect:
    defaults: dict[str, Any] = {
        "type": "run_command",
        "command": [sys.executable, "-c", _ECHO_SCRIPT],
        "inject_result": True,
    }
    defaults.update(overrides)
    return RuleEffect(**defaults)


async def _apply(effect: RuleEffect, event: HookEvent) -> list[str]:
    context_parts: list[str] = []
    await EffectsMixin()._apply_effect(effect, _ROW, {}, {"event": event}, {}, context_parts, [])
    return context_parts


class TestRunCommandInline:
    async def test_success_injects_context_from_stdin_payload(self) -> None:
        context_parts = await _apply(_effect(), _event())
        assert context_parts == ["saw Edit"]

    async def test_success_without_inject_result_appends_nothing(self) -> None:
        context_parts = await _apply(_effect(inject_result=False), _event())
        assert context_parts == []

    async def test_nonzero_exit_fails_open(self) -> None:
        effect = _effect(command=[sys.executable, "-c", "import sys; sys.exit(3)"])
        assert await _apply(effect, _event()) == []

    async def test_missing_executable_fails_open(self) -> None:
        effect = _effect(command=["/nonexistent/gobby-test-binary"])
        assert await _apply(effect, _event()) == []

    async def test_timeout_kills_process_and_fails_open(self) -> None:
        effect = _effect(
            command=[sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_seconds=0.2,
        )
        assert await _apply(effect, _event()) == []

    async def test_non_json_stdout_fails_open(self) -> None:
        effect = _effect(command=[sys.executable, "-c", "print('plain text')"])
        assert await _apply(effect, _event()) == []

    async def test_missing_event_is_noop(self) -> None:
        context_parts: list[str] = []
        await EffectsMixin()._apply_effect(_effect(), _ROW, {}, {}, {}, context_parts, [])
        assert context_parts == []


class TestRunCommandBackground:
    async def test_background_schedules_task_and_injects_nothing_inline(self) -> None:
        effect = _effect(background=True)
        context_parts: list[str] = []
        with patch("gobby.workflows.engine.effects.create_background_task") as mock_create:
            await EffectsMixin()._apply_effect(
                effect, _ROW, {}, {"event": _event()}, {}, context_parts, []
            )
        assert mock_create.call_count == 1
        assert context_parts == []
        coro = mock_create.call_args.args[0]
        coro.close()

    async def test_deliver_writes_command_result_message(self) -> None:
        mixin = EffectsMixin()
        mixin.db = MagicMock()
        manager = MagicMock()
        with patch(
            "gobby.storage.inter_session_messages.InterSessionMessageManager",
            return_value=manager,
        ):
            await mixin._run_command_then_deliver(
                [sys.executable, "-c", _ECHO_SCRIPT],
                None,
                json.dumps({"tool_name": "Write"}),
                5.0,
                rule_name="test-rule",
                platform_session_id="platform-session-1",
            )
        manager.create_message.assert_called_once_with(
            from_session="platform-session-1",
            to_session="platform-session-1",
            content="saw Write",
            message_type="command_result",
        )

    async def test_deliver_skips_without_platform_session(self) -> None:
        mixin = EffectsMixin()
        mixin.db = MagicMock()
        with patch(
            "gobby.storage.inter_session_messages.InterSessionMessageManager"
        ) as manager_cls:
            await mixin._run_command_then_deliver(
                [sys.executable, "-c", _ECHO_SCRIPT],
                None,
                json.dumps({"tool_name": "Write"}),
                5.0,
                rule_name="test-rule",
                platform_session_id=None,
            )
        manager_cls.assert_not_called()


class TestExtractCommandContext:
    def test_hook_specific_output_shape(self) -> None:
        payload = json.dumps({"hookSpecificOutput": {"additionalContext": "finding"}})
        assert _extract_command_context(payload) == "finding"

    def test_top_level_additional_context(self) -> None:
        assert _extract_command_context(json.dumps({"additionalContext": "note"})) == "note"

    @pytest.mark.parametrize(
        "payload",
        ["", "not json", "[]", "42", json.dumps({"hookSpecificOutput": {}}), "{}"],
    )
    def test_unusable_payloads_return_none(self, payload: str) -> None:
        assert _extract_command_context(payload) is None
