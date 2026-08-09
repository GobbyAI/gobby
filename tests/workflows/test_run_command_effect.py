"""Tests for the run_command rule effect."""

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.workflow_definitions import WorkflowDefinitionRow
from gobby.workflows.definitions import RuleEffect
from gobby.workflows.engine.effects import EffectsMixin
from gobby.workflows.engine.run_command import (
    STDERR_LIMIT_BYTES,
    STDOUT_LIMIT_BYTES,
    RunCommandResult,
    _parse_command_output,
    build_run_command_payload,
    execute_run_command,
)

_ROW = cast(WorkflowDefinitionRow, SimpleNamespace(id="rule-id", name="test-rule"))

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
        messages: list[dict[str, str]] = []

        def record_message(**kwargs: str) -> None:
            messages.append(kwargs)

        manager.create_message.side_effect = record_message
        with patch(
            "gobby.storage.inter_session_messages.InterSessionMessageManager",
            return_value=manager,
        ):
            await mixin._run_command_then_deliver(
                [sys.executable, "-c", _ECHO_SCRIPT],
                os.getcwd(),
                json.dumps({"tool_name": "Write"}).encode(),
                5.0,
                rule_name="test-rule",
                rule_id="rule-id",
                platform_session_id="platform-session-1",
            )
        assert messages == [
            {
                "from_session": "platform-session-1",
                "to_session": "platform-session-1",
                "content": "saw Write",
                "message_type": "command_result",
            }
        ]

    async def test_deliver_skips_without_platform_session(self) -> None:
        mixin = EffectsMixin()
        mixin.db = MagicMock()
        constructions: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def record_construction(*args: object, **kwargs: object) -> MagicMock:
            constructions.append((args, kwargs))
            return MagicMock()

        with patch(
            "gobby.storage.inter_session_messages.InterSessionMessageManager",
            side_effect=record_construction,
        ):
            await mixin._run_command_then_deliver(
                [sys.executable, "-c", _ECHO_SCRIPT],
                os.getcwd(),
                json.dumps({"tool_name": "Write"}).encode(),
                5.0,
                rule_name="test-rule",
                rule_id="rule-id",
                platform_session_id=None,
            )
        assert constructions == []


class TestRunCommandDeadlines:
    async def test_inline_timeout_uses_remaining_aggregate_deadline(self) -> None:
        result = RunCommandResult(
            status="success",
            context=None,
            duration_ms=1.0,
            exit_code=0,
            stdout_bytes=0,
            stderr_bytes=0,
            timeout_seconds=0.1,
            overflow_stream=None,
            background=False,
        )
        execute = AsyncMock(return_value=result)
        context_parts: list[str] = []
        with patch("gobby.workflows.engine.effects.execute_run_command", execute):
            await EffectsMixin()._apply_effect(
                _effect(timeout_seconds=5.0),
                _ROW,
                {},
                {"event": _event(), "_blocking_deadline": time.monotonic() + 0.2},
                {},
                context_parts,
                [],
            )

        await_args = execute.await_args
        assert await_args is not None
        timeout = await_args.kwargs["timeout_seconds"]
        assert 0 < timeout <= 0.2

    async def test_exhausted_deadline_skips_spawn_and_is_audited(self) -> None:
        mixin = EffectsMixin()
        context_parts: list[str] = []
        with (
            patch(
                "gobby.workflows.engine.effects.execute_run_command", new_callable=AsyncMock
            ) as run,
            patch.object(mixin, "_audit_run_command") as audit,
        ):
            await mixin._apply_effect(
                _effect(),
                _ROW,
                {},
                {"event": _event(), "_blocking_deadline": time.monotonic() - 1},
                {},
                context_parts,
                [],
            )

        run.assert_not_awaited()
        assert audit.call_args.args[0].status == "deadline_exhausted"


class TestRunCommandBounds:
    async def test_stdout_overflow_kills_and_reaps_child(self, tmp_path: Path) -> None:
        pid_path = tmp_path / "pid"
        script = (
            "import os, pathlib, sys, time; "
            f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
            f"sys.stdout.buffer.write(b'x' * {STDOUT_LIMIT_BYTES + 1}); "
            "sys.stdout.flush(); time.sleep(30)"
        )

        result = await execute_run_command(
            [sys.executable, "-c", script],
            cwd=str(tmp_path),
            stdin_payload=b"{}",
            timeout_seconds=5,
            background=False,
        )

        assert result.status == "output_limit"
        assert result.overflow_stream == "stdout"
        assert result.stdout_bytes > STDOUT_LIMIT_BYTES
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_path.read_text()), 0)

    async def test_stderr_overflow_is_capped_independently(self, tmp_path: Path) -> None:
        result = await execute_run_command(
            [
                sys.executable,
                "-c",
                f"import sys; sys.stderr.buffer.write(b'x' * {STDERR_LIMIT_BYTES + 1})",
            ],
            cwd=str(tmp_path),
            stdin_payload=b"{}",
            timeout_seconds=5,
            background=False,
        )

        assert result.status == "output_limit"
        assert result.overflow_stream == "stderr"
        assert result.stderr_bytes > STDERR_LIMIT_BYTES

    async def test_timeout_kills_and_reaps_child(self, tmp_path: Path) -> None:
        pid_path = tmp_path / "pid"
        script = (
            "import os, pathlib, time; "
            f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
            "time.sleep(30)"
        )

        result = await execute_run_command(
            [sys.executable, "-c", script],
            cwd=str(tmp_path),
            stdin_payload=b"{}",
            timeout_seconds=0.2,
            background=False,
        )

        assert result.status == "timeout"
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_path.read_text()), 0)


def test_run_command_payload_preserves_provider_fields_and_normalizes_edits() -> None:
    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="provider-session",
        source=SessionSource.GROK,
        timestamp=datetime.now(UTC),
        data={
            "function_name": "Write",
            "parameters": {
                "file_path": "one.tsx",
                "file_paths": ["one.tsx", "two.tsx"],
            },
            "provider_field": {"nested": True},
        },
        cwd="/tmp/project",
        metadata={},
    )

    payload = build_run_command_payload(event)

    assert payload["function_name"] == "Write"
    assert payload["parameters"] == {
        "file_path": "one.tsx",
        "file_paths": ["one.tsx", "two.tsx"],
    }
    assert payload["tool_input"] == payload["parameters"]
    assert payload["provider_field"] == {"nested": True}
    assert payload["cwd"] == "/tmp/project"
    assert payload["hook_event_name"] == "PostToolUse"
    assert "tool_name" not in event.data


def test_run_command_payload_synthesizes_stop_only_when_missing() -> None:
    stop_event = _event(hook_event_name="ProviderStop")
    stop_event.event_type = HookEventType.STOP
    preserved = build_run_command_payload(stop_event)
    stop_event.data.pop("hook_event_name")
    synthesized = build_run_command_payload(stop_event)

    assert preserved["hook_event_name"] == "ProviderStop"
    assert synthesized["hook_event_name"] == "Stop"


@pytest.mark.parametrize(
    "status",
    [
        "success",
        "spawn_error",
        "nonzero_exit",
        "timeout",
        "output_limit",
        "invalid_output",
        "deadline_exhausted",
    ],
)
def test_run_command_audit_contains_metadata_only(status: str) -> None:
    mixin = EffectsMixin()
    mixin.db = MagicMock()
    manager = MagicMock()
    result = RunCommandResult(
        status=cast(Any, status),
        context="secret detector output",
        duration_ms=12.5,
        exit_code=3,
        stdout_bytes=44,
        stderr_bytes=55,
        timeout_seconds=5.0,
        overflow_stream="stdout" if status == "output_limit" else None,
        background=False,
    )
    with patch("gobby.storage.workflow_audit.WorkflowAuditManager", return_value=manager):
        mixin._audit_run_command(
            result,
            rule_name="test-rule",
            rule_id="rule-id",
            platform_session_id="platform-session-1",
        )

    kwargs = manager.log.call_args.kwargs
    assert kwargs["event_type"] == "effect"
    assert kwargs["result"] == status
    assert kwargs["rule_id"] == "rule-id"
    assert kwargs["context"] == {
        "duration_ms": 12.5,
        "exit_code": 3,
        "stdout_bytes": 44,
        "stderr_bytes": 55,
        "timeout_seconds": 5.0,
        "overflow_stream": "stdout" if status == "output_limit" else None,
        "background": False,
    }
    assert "secret detector output" not in json.dumps(kwargs)


class TestParseCommandOutput:
    def test_hook_specific_output_shape(self) -> None:
        payload = json.dumps({"hookSpecificOutput": {"additionalContext": "finding"}}).encode()
        assert _parse_command_output(payload) == (True, "finding")

    def test_top_level_additional_context(self) -> None:
        payload = json.dumps({"additionalContext": "note"}).encode()
        assert _parse_command_output(payload) == (True, "note")

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            (b"", (True, None)),
            (b"not json", (False, None)),
            (b"[]", (False, None)),
            (b"42", (False, None)),
            (json.dumps({"hookSpecificOutput": {}}).encode(), (True, None)),
            (b"{}", (True, None)),
        ],
    )
    def test_output_validity_is_distinct_from_missing_context(
        self,
        payload: bytes,
        expected: tuple[bool, str | None],
    ) -> None:
        assert _parse_command_output(payload) == expected
