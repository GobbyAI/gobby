"""AGY hook adapter tests."""

from __future__ import annotations

import inspect
import json
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from gobby.adapters.acp_hook_adapter import ACPHookAdapter
from gobby.adapters.agy import AGY_APPROVAL_DENIED_REASON, AgyAdapter
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("hook_type", "expected_type"),
    [
        ("PreInvocation", HookEventType.BEFORE_AGENT),
        ("PreToolUse", HookEventType.BEFORE_TOOL),
        ("PostToolUse", HookEventType.AFTER_TOOL),
        ("PostInvocation", HookEventType.AFTER_AGENT),
        ("Stop", HookEventType.STOP),
    ],
)
def test_translate_to_hook_event_maps_agy_hooks(
    hook_type: str,
    expected_type: HookEventType,
) -> None:
    adapter = AgyAdapter()

    event = adapter.translate_to_hook_event(
        {
            "source": "agy",
            "hook_type": hook_type,
            "input_data": {
                "hook_event_name": hook_type,
                "session_id": "agy-session-123",
                "cwd": "/repo",
            },
        }
    )

    assert event.event_type is expected_type
    assert event.source is SessionSource.AGY
    assert event.session_id == "agy-session-123"
    assert event.cwd == "/repo"


def test_translate_to_hook_event_accepts_direct_agy_payload() -> None:
    event = AgyAdapter().translate_to_hook_event(
        {
            "hook_event_name": "PreInvocation",
            "session_id": "agy-direct-123",
            "cwd": "/workspace",
            "prompt": "implement the task",
        }
    )

    assert event.event_type is HookEventType.BEFORE_AGENT
    assert event.session_id == "agy-direct-123"
    assert event.cwd == "/workspace"
    assert event.data["prompt"] == "implement the task"


def test_pre_tool_use_normalizes_agy_shell_tool_name() -> None:
    event = AgyAdapter().translate_to_hook_event(
        {
            "hook_type": "PreToolUse",
            "input_data": {
                "hook_event_name": "PreToolUse",
                "session_id": "agy-tool-123",
                "tool_name": "run_shell_command",
                "tool_input": {"command": "pwd"},
            },
        }
    )

    assert event.event_type is HookEventType.BEFORE_TOOL
    assert event.data["tool_name"] == "Bash"
    assert event.metadata["original_tool_name"] == "run_shell_command"
    assert event.metadata["normalized_tool_name"] == "Bash"


def test_pre_tool_use_allow_response_is_compact() -> None:
    result = AgyAdapter().translate_from_hook_response(
        HookResponse(decision="allow"),
        hook_type="PreToolUse",
    )

    # A plain allow carries no decision field since #20926 (6cf347dfc0).
    assert result == {}


def test_pre_tool_use_block_response_becomes_agy_deny() -> None:
    result = AgyAdapter().translate_from_hook_response(
        HookResponse(decision="block", reason="policy blocked this command"),
        hook_type="PreToolUse",
    )

    assert result == {"decision": "deny", "reason": "policy blocked this command"}


def test_pre_tool_use_ask_response_fails_closed_as_deny() -> None:
    """AGY cannot prompt (record 1.1.14): an approval request is denied, never ``ask``."""
    result = AgyAdapter().translate_from_hook_response(
        HookResponse(
            decision="ask",
            reason="needs confirmation",
            modified_input={"command": "pwd"},
        ),
        hook_type="PreToolUse",
    )

    assert result["decision"] == "deny"
    assert result["reason"] == f"{AGY_APPROVAL_DENIED_REASON} (needs confirmation)"
    assert "approval" in result["reason"]
    assert result["overwrite"] == {"command": "pwd"}


def test_pre_tool_use_ask_without_reason_still_explains_the_deny() -> None:
    result = AgyAdapter().translate_from_hook_response(
        HookResponse(decision="ask"),
        hook_type="PreToolUse",
    )

    assert result == {"decision": "deny", "reason": AGY_APPROVAL_DENIED_REASON}


def test_pre_tool_use_ask_permission_decision_fails_closed() -> None:
    """A permission effect can hand the dataclass an unhonored ``ask``.

    ``HookResponse.permission_decision`` is annotated ``Literal["allow", "deny"]``
    but ``HookResponse`` is a plain dataclass, and the rule engine assigns
    whatever string a ``_permission_response`` effect carries
    (``workflows/engine/evaluation.py``). AGY honors neither ``ask`` nor
    ``force_ask``, so the adapter denies with the explanation.
    """
    result = AgyAdapter().translate_from_hook_response(
        HookResponse(decision="allow", permission_decision=cast(Any, "ask"), reason="confirm"),
        hook_type="PreToolUse",
    )

    assert result == {
        "decision": "deny",
        "reason": f"{AGY_APPROVAL_DENIED_REASON} (confirm)",
    }


@pytest.mark.parametrize(
    "response",
    [
        HookResponse(decision="ask"),
        HookResponse(decision="ask", reason="confirm"),
        HookResponse(decision="ask", auto_approve=True),
        HookResponse(decision="ask", permission_decision="allow"),
        HookResponse(decision="ask", permission_decision="deny"),
        HookResponse(decision="allow", permission_decision=cast(Any, "ask")),
        HookResponse(decision="allow", permission_decision=cast(Any, "force_ask")),
        HookResponse(decision="allow"),
        HookResponse(decision="deny", reason="no"),
        HookResponse(decision="block", reason="no"),
    ],
)
@pytest.mark.parametrize("hook_type", ["PreToolUse", "PostToolUse", "PreInvocation", "Stop"])
def test_agy_never_emits_ask_or_force_ask(response: HookResponse, hook_type: str) -> None:
    result = AgyAdapter().translate_from_hook_response(response, hook_type=hook_type)

    assert result.get("decision") not in {"ask", "force_ask"}
    assert "force_ask" not in json.dumps(result)


def test_stop_block_response_continues_the_agent_loop() -> None:
    result = AgyAdapter().translate_from_hook_response(
        HookResponse(decision="block", reason="stay active"),
        hook_type="Stop",
    )

    assert result == {"decision": "continue", "reason": "stay active"}


def test_stop_allow_response_is_empty() -> None:
    result = AgyAdapter().translate_from_hook_response(
        HookResponse(decision="allow"),
        hook_type="Stop",
    )

    assert result == {}


def test_pre_invocation_context_becomes_inject_steps() -> None:
    result = AgyAdapter().translate_from_hook_response(
        HookResponse(
            decision="allow",
            context="ephemeral note",
            system_message="user note",
        ),
        hook_type="PreInvocation",
    )

    assert result == {
        "injectSteps": [
            {"ephemeralMessage": "ephemeral note"},
            {"userMessage": "user note"},
        ]
    }


def test_post_tool_use_response_is_empty() -> None:
    result = AgyAdapter().translate_from_hook_response(
        HookResponse(decision="allow", reason="unused"),
        hook_type="PostToolUse",
    )

    assert result == {}


def test_handle_native_uses_agy_source_and_compact_response() -> None:
    hook_manager = MagicMock()
    hook_manager.handle.return_value = HookResponse(decision="allow")
    native_event = {
        "hook_type": "PreToolUse",
        "source": "agy",
        "input_data": {
            "hook_event_name": "PreToolUse",
            "session_id": "agy-handle-123",
            "tool_name": "run_shell_command",
            "tool_input": {"command": "pwd"},
        },
    }

    result = AgyAdapter().handle_native(native_event, hook_manager)

    # A plain allow carries no decision field since #20926 (6cf347dfc0).
    assert result == {}
    hook_event = hook_manager.handle.call_args.args[0]
    assert hook_event.source is SessionSource.AGY
    assert hook_event.event_type is HookEventType.BEFORE_TOOL


def _agy_pre_invocation_event(
    *,
    invocation_num: int = 0,
    extra_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    input_data: dict[str, Any] = {
        "hookEventName": "PreInvocation",
        "conversationId": "conv-1",
        "transcriptPath": "/tmp/agy.jsonl",
        "workspacePaths": ["/repo"],
        "artifactDirectoryPath": "/tmp/artifacts",
        "modelName": "gemini-3",
        "invocationNum": invocation_num,
        "initialNumSteps": 0,
        "executionNum": 1,
    }
    if extra_input:
        input_data.update(extra_input)
    return {
        "source": "agy",
        "hook_type": "PreInvocation",
        "input_data": input_data,
    }


def _inject_step_values(result: dict[str, Any]) -> list[str]:
    steps = result.get("injectSteps")
    assert isinstance(steps, list)
    values: list[str] = []
    for step in steps:
        assert isinstance(step, dict)
        for value in step.values():
            if isinstance(value, str):
                values.append(value)
    return values


class TestAgyCamelCasePayload:
    def test_reads_conversation_id_transcript_path_and_workspace_paths(self) -> None:
        event = AgyAdapter().translate_to_hook_event(_agy_pre_invocation_event())

        assert event.event_type is HookEventType.BEFORE_AGENT
        assert event.session_id == "conv-1"
        assert event.cwd == "/repo"
        assert event.data["transcript_path"] == "/tmp/agy.jsonl"
        assert event.data["workspace_paths"] == ["/repo"]
        assert event.data["artifact_directory_path"] == "/tmp/artifacts"
        assert event.data["model"] == "gemini-3"
        assert event.data["invocation_num"] == 0
        assert event.data["execution_num"] == 1

    def test_flattens_tool_call_name_and_args(self) -> None:
        event = AgyAdapter().translate_to_hook_event(
            {
                "source": "agy",
                "hook_type": "PreToolUse",
                "input_data": {
                    "hookEventName": "PreToolUse",
                    "conversationId": "conv-tool",
                    "workspacePaths": ["/repo"],
                    "toolCall": {
                        "name": "list_dir",
                        "args": {"DirectoryPath": "/repo"},
                    },
                },
            }
        )

        assert event.event_type is HookEventType.BEFORE_TOOL
        assert event.session_id == "conv-tool"
        assert event.data["tool_name"] == "Ls"
        assert event.data["tool_input"] == {"DirectoryPath": "/repo"}
        assert event.metadata["original_tool_name"] == "list_dir"

    def test_workspace_paths_zero_fills_cwd_when_absent(self) -> None:
        event = AgyAdapter().translate_to_hook_event(
            {
                "hookEventName": "PreInvocation",
                "conversationId": "conv-direct",
                "transcriptPath": "/tmp/direct.jsonl",
                "workspacePaths": ["/workspace"],
            }
        )

        assert event.session_id == "conv-direct"
        assert event.cwd == "/workspace"
        assert event.data["transcript_path"] == "/tmp/direct.jsonl"

    def test_later_invocation_maps_to_before_model(self) -> None:
        event = AgyAdapter().translate_to_hook_event(_agy_pre_invocation_event(invocation_num=2))

        assert event.event_type is HookEventType.BEFORE_MODEL
        assert event.data["invocation_num"] == 2

    def test_acp_hook_adapter_does_not_read_agy_keys(self) -> None:
        class _BareACP(ACPHookAdapter):
            @property
            def source(self) -> SessionSource:
                return SessionSource.QWEN

        event = _BareACP().translate_to_hook_event(_agy_pre_invocation_event())
        source = inspect.getsource(ACPHookAdapter.translate_to_hook_event)

        assert event.session_id == ""
        assert event.cwd is None
        assert "conversationId" not in source
        assert "workspacePaths" not in source
        assert "transcriptPath" not in source


class TestAgyHandleNativeSynthesis:
    def test_pre_invocation_dispatches_session_start_then_before_agent(self) -> None:
        hook_manager = MagicMock()
        hook_manager.handle.return_value = HookResponse(decision="allow")

        AgyAdapter().handle_native(_agy_pre_invocation_event(), hook_manager)

        events = [call.args[0] for call in hook_manager.handle.call_args_list]
        assert [event.event_type for event in events] == [
            HookEventType.SESSION_START,
            HookEventType.BEFORE_AGENT,
        ]
        start = events[0]
        assert isinstance(start, HookEvent)
        assert start.session_id == "conv-1"
        assert start.cwd == "/repo"
        assert start.data["transcript_path"] == "/tmp/agy.jsonl"
        assert events[1].event_type is HookEventType.BEFORE_AGENT

    def test_repeated_pre_invocation_still_dispatches_both_phases(self) -> None:
        hook_manager = MagicMock()
        hook_manager.handle.return_value = HookResponse(decision="allow")
        adapter = AgyAdapter()

        adapter.handle_native(_agy_pre_invocation_event(), hook_manager)
        adapter.handle_native(_agy_pre_invocation_event(), hook_manager)

        types = [call.args[0].event_type for call in hook_manager.handle.call_args_list]
        assert types == [
            HookEventType.SESSION_START,
            HookEventType.BEFORE_AGENT,
            HookEventType.SESSION_START,
            HookEventType.BEFORE_AGENT,
        ]

    def test_later_invocation_dispatches_before_model(self) -> None:
        hook_manager = MagicMock()
        hook_manager.handle.return_value = HookResponse(decision="allow")

        AgyAdapter().handle_native(
            _agy_pre_invocation_event(invocation_num=3),
            hook_manager,
        )

        types = [call.args[0].event_type for call in hook_manager.handle.call_args_list]
        assert types == [HookEventType.SESSION_START, HookEventType.BEFORE_MODEL]

    def test_merges_startup_context_into_inject_steps(self) -> None:
        hook_manager = MagicMock()

        def _handle(event: HookEvent) -> HookResponse:
            if event.event_type is HookEventType.SESSION_START:
                return HookResponse(
                    decision="allow",
                    context="startup context",
                    system_message="startup banner",
                )
            return HookResponse(decision="allow", context="turn context")

        hook_manager.handle.side_effect = _handle

        result = AgyAdapter().handle_native(_agy_pre_invocation_event(), hook_manager)
        blob = "\n".join(_inject_step_values(result))

        assert "startup context" in blob
        assert "startup banner" in blob
        assert "turn context" in blob
        assert "claim_generation" not in result
        assert "receipt_id" not in result

    def test_inject_context_payload_reaches_response(self) -> None:
        hook_manager = MagicMock()

        def _handle(event: HookEvent) -> HookResponse:
            if event.event_type is HookEventType.BEFORE_AGENT:
                return HookResponse(
                    decision="allow",
                    context="inject_context rule payload",
                )
            return HookResponse(decision="allow")

        hook_manager.handle.side_effect = _handle

        result = AgyAdapter().handle_native(_agy_pre_invocation_event(), hook_manager)

        assert "inject_context rule payload" in _inject_step_values(result)

    def test_private_claim_fields_are_not_emitted(self) -> None:
        hook_manager = MagicMock()
        hook_manager.handle.return_value = HookResponse(
            decision="allow",
            context="visible",
            metadata={
                "startup_claim_generation": 4,
                "owner_token": "owner-1",
                "receipt_id": "receipt-1",
            },
        )

        result = AgyAdapter().handle_native(_agy_pre_invocation_event(), hook_manager)

        assert "startup_claim_generation" not in result
        assert "owner_token" not in result
        assert "receipt_id" not in result
        assert "visible" in "\n".join(_inject_step_values(result))


class TestAgyToolOutcome:
    def test_post_tool_use_uses_live_proven_outcome(self) -> None:
        event = AgyAdapter().translate_to_hook_event(
            {
                "source": "agy",
                "hook_type": "PostToolUse",
                "input_data": {
                    "hookEventName": "PostToolUse",
                    "conversationId": "conv-tool",
                    "workspacePaths": ["/repo"],
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "ls -la", "Cwd": "/repo"},
                    },
                },
            }
        )

        outcome = event.data["tool_outcome"]
        assert outcome["status"] == "succeeded"
        assert outcome["provenance"] != "agy.provider_contract_unproven"
        assert str(outcome["provenance"]).startswith("agy.")


class TestAgyResponseMapping:
    def test_post_invocation_deny_emits_force_continue(self) -> None:
        result = AgyAdapter().translate_from_hook_response(
            HookResponse(decision="deny", reason="stay in the turn"),
            hook_type="PostInvocation",
        )

        assert result["terminationBehavior"] == "force_continue"
        assert "terminate" not in result.get("terminationBehavior", "")
        assert {"ephemeralMessage": "stay in the turn"} in result["injectSteps"]

    def test_post_invocation_allow_omits_termination_behavior(self) -> None:
        result = AgyAdapter().translate_from_hook_response(
            HookResponse(decision="allow", context="note"),
            hook_type="PostInvocation",
        )

        assert "terminationBehavior" not in result
        assert result["injectSteps"] == [{"ephemeralMessage": "note"}]

    def test_never_emits_terminate_permission_overrides_or_tool_call_steps(self) -> None:
        result = AgyAdapter().translate_from_hook_response(
            HookResponse(
                decision="block",
                reason="blocked",
                updated_permissions=[{"permission": "all"}],
            ),
            hook_type="PostInvocation",
        )

        assert result.get("terminationBehavior") == "force_continue"
        assert "permissionOverrides" not in result
        assert all("toolCall" not in step for step in result["injectSteps"])
        assert "terminate" not in result.values()
