"""Provider capability registry and adapter degradation tests."""

from __future__ import annotations

import pytest

from gobby.adapters.agy import AgyAdapter
from gobby.adapters.agy_contract import AGY_HOOK_NAMES
from gobby.adapters.capabilities import (
    ContextChannel,
    ProviderDecisionStyle,
    ReasonFormat,
    get_provider_capabilities,
)
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.claude_contract import CLAUDE_NATIVE_HOOK_NAMES
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.droid import DroidAdapter
from gobby.adapters.droid_contract import DROID_PASCAL_HOOK_NAMES
from gobby.adapters.grok import GrokAdapter
from gobby.adapters.qwen import QwenAdapter
from gobby.hooks.events import HookEventType, HookResponse, SessionSource
from gobby.servers.routes.mcp.hooks import _graceful_error_response

pytestmark = pytest.mark.unit


def _capture_degradations(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    calls: list[dict[str, str]] = []

    def fake_inc_counter(
        name: str,
        amount: int = 1,
        attributes: dict[str, str] | None = None,
    ) -> None:
        assert name == "adapter_degradations_total"
        assert amount == 1
        calls.append(attributes or {})

    monkeypatch.setattr("gobby.adapters.degradation.inc_counter", fake_inc_counter)
    return calls


def test_capability_registry_covers_current_http_adapters() -> None:
    assert set(get_provider_capabilities(SessionSource.CLAUDE).hook_events) == set(
        CLAUDE_NATIVE_HOOK_NAMES
    )
    assert get_provider_capabilities(SessionSource.CODEX).hook_events.keys() == (
        CodexHooksAdapter.EVENT_MAP.keys()
    )
    assert get_provider_capabilities(SessionSource.QWEN).hook_events.keys() == (
        QwenAdapter.EVENT_MAP.keys()
    )
    assert get_provider_capabilities(SessionSource.GROK).hook_events.keys() == (
        GrokAdapter.EVENT_MAP.keys()
    )
    assert tuple(get_provider_capabilities(SessionSource.DROID).hook_events) == (
        DROID_PASCAL_HOOK_NAMES
    )
    assert tuple(get_provider_capabilities(SessionSource.AGY).hook_events) == AGY_HOOK_NAMES
    assert get_provider_capabilities(SessionSource.AGY).hook_events.keys() == (
        AgyAdapter.EVENT_MAP.keys()
    )


def test_current_context_and_decision_capabilities_are_declared() -> None:
    claude_pre_tool = get_provider_capabilities("claude").get_hook("pre-tool-use")
    codex_pre_tool = get_provider_capabilities("codex").get_hook("PreToolUse")
    codex_subagent_start = get_provider_capabilities("codex").get_hook("SubagentStart")
    codex_subagent_stop = get_provider_capabilities("codex").get_hook("SubagentStop")
    qwen_pre_tool = get_provider_capabilities("qwen").get_hook("PreToolUse")
    grok_pre_tool = get_provider_capabilities("grok").get_hook("pre_tool_use")
    agy_pre_tool = get_provider_capabilities("agy").get_hook("PreToolUse")
    droid_pre_tool = get_provider_capabilities("droid").get_hook("PreToolUse")

    assert claude_pre_tool is not None
    assert claude_pre_tool.context_channel is ContextChannel.ADDITIONAL_CONTEXT
    assert claude_pre_tool.reason_format is ReasonFormat.PASSTHROUGH

    assert codex_pre_tool is not None
    assert codex_pre_tool.context_channel is ContextChannel.SYSTEM_MESSAGE
    assert codex_pre_tool.decision_style is ProviderDecisionStyle.PRE_TOOL_USE

    assert codex_subagent_start is not None
    assert codex_subagent_start.context_channel is ContextChannel.ADDITIONAL_CONTEXT
    assert codex_subagent_start.decision_style is ProviderDecisionStyle.NONE

    assert codex_subagent_stop is not None
    assert codex_subagent_stop.context_channel is ContextChannel.NONE
    assert codex_subagent_stop.decision_style is ProviderDecisionStyle.TOP_LEVEL_BLOCK

    assert qwen_pre_tool is not None
    assert qwen_pre_tool.context_channel is ContextChannel.ADDITIONAL_CONTEXT
    assert qwen_pre_tool.decision_style is ProviderDecisionStyle.PRE_TOOL_USE

    assert grok_pre_tool is not None
    assert grok_pre_tool.context_channel is ContextChannel.NONE
    assert grok_pre_tool.decision_style is ProviderDecisionStyle.PRE_TOOL_USE

    assert agy_pre_tool is not None
    assert agy_pre_tool.context_channel is ContextChannel.NONE
    assert agy_pre_tool.decision_style is ProviderDecisionStyle.PRE_TOOL_USE
    assert agy_pre_tool.supports_response_field("permission_decision")
    assert agy_pre_tool.supports_response_field("auto_approve")
    assert agy_pre_tool.supports_response_field("modified_input")

    assert droid_pre_tool is not None
    assert droid_pre_tool.context_channel is ContextChannel.NONE
    assert droid_pre_tool.decision_style is ProviderDecisionStyle.PRE_TOOL_USE


def test_claude_2_1_226_hook_capabilities_are_declared() -> None:
    capabilities = get_provider_capabilities("claude")

    setup = capabilities.get_hook("Setup")
    expansion = capabilities.get_hook("UserPromptExpansion")
    batch = capabilities.get_hook("PostToolBatch")
    display = capabilities.get_hook("MessageDisplay")
    directory = capabilities.get_hook("DirectoryAdded")

    assert setup is not None
    assert setup.event_type is HookEventType.SETUP
    assert setup.decision_style is ProviderDecisionStyle.IGNORE_BLOCK
    assert setup.context_channel is ContextChannel.ADDITIONAL_CONTEXT
    assert expansion is not None
    assert expansion.decision_style is ProviderDecisionStyle.TOP_LEVEL_BLOCK
    assert expansion.context_channel is ContextChannel.ADDITIONAL_CONTEXT
    assert batch is not None
    assert batch.decision_style is ProviderDecisionStyle.TOP_LEVEL_BLOCK
    assert batch.context_channel is ContextChannel.ADDITIONAL_CONTEXT
    assert display is not None
    assert display.event_type is HookEventType.MESSAGE_DISPLAY
    assert display.decision_style is ProviderDecisionStyle.DISPLAY_CONTENT
    assert display.supports_response_field("display_content")
    assert directory is not None
    assert directory.event_type is HookEventType.DIRECTORY_ADDED
    assert directory.decision_style is ProviderDecisionStyle.IGNORE_BLOCK


def test_agy_hook_capabilities_have_no_live_transport_claims() -> None:
    """AGY supports hook install parity only; live runtime transport stays unavailable."""
    capabilities = get_provider_capabilities("agy")

    assert capabilities.transport_capabilities == {}
    assert capabilities.supports_permissions is True
    assert capabilities.get_hook("pre_tool_use") is capabilities.get_hook("PreToolUse")
    assert capabilities.get_hook("Stop") is not None
    assert capabilities.get_hook("Stop").decision_style is ProviderDecisionStyle.HARD_STOP


def test_grok_1_0_hook_capabilities_are_declared() -> None:
    """Keep the Grok hook registry aligned with current ACP hook names."""
    capabilities = get_provider_capabilities("grok")

    assert capabilities.transport_capabilities == {
        "loadSession": True,
        "x.ai/fs_notify": True,
        "cancelRewind": True,
        "availableCommands": ("compact", "context", "session-info"),
    }

    assert capabilities.get_hook("PreToolUse") is capabilities.get_hook("pre_tool_use")
    assert capabilities.get_hook("PreCompact") is capabilities.get_hook("pre_compact")
    assert capabilities.get_hook("PostCompact") is capabilities.get_hook("post_compact")
    assert capabilities.get_hook("Stop") is capabilities.get_hook("stop")
    assert capabilities.get_hook("PermissionDenied") is capabilities.get_hook("permission_denied")
    assert capabilities.get_hook("StopFailure") is capabilities.get_hook("stop_failure")
    assert capabilities.get_hook("SubagentStart") is capabilities.get_hook("subagent_start")
    assert capabilities.get_hook("SubagentStop") is capabilities.get_hook("subagent_stop")
    assert capabilities.get_hook("SubagentEnd") is capabilities.get_hook("subagent_stop")

    pre_tool = capabilities.get_hook("pre_tool_use")
    pre_compact = capabilities.get_hook("pre_compact")
    stop = capabilities.get_hook("stop")
    post_compact = capabilities.get_hook("post_compact")
    post_tool = capabilities.get_hook("post_tool_use")
    permission_denied = capabilities.get_hook("permission_denied")
    stop_failure = capabilities.get_hook("stop_failure")
    subagent_start = capabilities.get_hook("subagent_start")
    subagent_stop = capabilities.get_hook("subagent_stop")

    assert pre_tool is not None
    assert pre_tool.context_channel is ContextChannel.NONE
    assert pre_tool.decision_style is ProviderDecisionStyle.PRE_TOOL_USE
    assert pre_tool.supports_response_field("permission_decision")
    assert pre_tool.supports_response_field("auto_approve")
    assert pre_tool.supports_response_field("modified_input")

    assert pre_compact is not None
    assert pre_compact.context_channel is ContextChannel.NONE
    assert pre_compact.decision_style is ProviderDecisionStyle.NONE

    assert stop is not None
    assert stop.context_channel is ContextChannel.ADDITIONAL_CONTEXT
    assert stop.decision_style is ProviderDecisionStyle.TOP_LEVEL_BLOCK

    assert post_compact is not None
    assert post_compact.event_type is HookEventType.POST_COMPACT
    assert post_compact.context_channel is ContextChannel.NONE
    assert post_compact.decision_style is ProviderDecisionStyle.NONE

    assert post_tool is not None
    assert post_tool.context_channel is ContextChannel.NONE
    assert post_tool.decision_style is ProviderDecisionStyle.NONE
    assert permission_denied is not None
    assert permission_denied.decision_style is ProviderDecisionStyle.NONE
    assert stop_failure is not None
    assert stop_failure.decision_style is ProviderDecisionStyle.NONE
    assert subagent_start is not None
    assert subagent_start.decision_style is ProviderDecisionStyle.NONE
    assert subagent_stop is not None
    assert subagent_stop.decision_style is ProviderDecisionStyle.TOP_LEVEL_BLOCK
    assert subagent_stop.context_channel is ContextChannel.ADDITIONAL_CONTEXT

    passive_hooks = {
        "session_start",
        "user_prompt_submit",
        "post_tool_use",
        "post_tool_use_failure",
        "pre_compact",
        "post_compact",
        "notification",
        "permission_denied",
        "stop_failure",
        "subagent_start",
        "session_end",
    }
    for hook_name in passive_hooks:
        hook = capabilities.get_hook(hook_name)
        assert hook is not None
        assert hook.context_channel is ContextChannel.NONE
        assert hook.decision_style is ProviderDecisionStyle.NONE


def test_unsupported_elicitation_fields_are_dropped_with_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_degradations(monkeypatch)

    result = QwenAdapter().translate_from_hook_response(
        HookResponse(
            decision="allow",
            elicitation_action="accept",
            elicitation_content={"answer": "yes"},
        ),
        hook_type="PreToolUse",
    )

    assert result == {"continue": True}
    dropped_fields = {call["response_field"] for call in calls if call["kind"] == "dropped_field"}
    assert {"elicitation_action", "elicitation_content"} <= dropped_fields


def test_unknown_hook_response_fields_are_dropped_with_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_degradations(monkeypatch)

    result = CodexHooksAdapter().translate_from_hook_response(
        HookResponse(context="not supported by an unknown hook"),
        hook_type="UnknownHook",
    )

    assert result == {"continue": True}
    assert {
        "provider": "codex",
        "hook_type": "UnknownHook",
        "kind": "dropped_field",
        "response_field": "context",
        "destination_channel": "none",
    } in calls


def test_claude_reason_passthrough_records_no_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_degradations(monkeypatch)
    reason = (
        "Rule enforced by Gobby: [require-code-index-skill]\n"
        'Call get_skill(name="code-index") on gobby-skills directly through mcp__gobby__call_tool'
    )

    result = ClaudeCodeAdapter().translate_from_hook_response(
        HookResponse(decision="block", reason=reason),
        hook_type="pre-tool-use",
    )

    permission_reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert permission_reason == reason
    assert calls == []


def test_qwen_tool_block_preserves_native_recoverable_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_degradations(monkeypatch)
    reason = (
        "Rule enforced by Gobby: [require-code-index-skill]\n"
        'Call get_skill(name="code-index") on gobby-skills directly through '
        "mcp__gobby__call_tool"
    )

    result = QwenAdapter().translate_from_hook_response(
        HookResponse(decision="block", reason=reason),
        hook_type="PreToolUse",
    )

    assert result == {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }
    assert "permissionDecision" not in result
    assert "permissionDecisionReason" not in result
    assert calls == []


def test_empty_block_sentinel_records_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_degradations(monkeypatch)

    CodexHooksAdapter().translate_from_hook_response(
        HookResponse(decision="block", reason=""),
        hook_type="PreToolUse",
    )

    assert any(call["kind"] == "empty_block_sentinel" for call in calls)


def test_codex_context_reroutes_to_system_message_when_additional_context_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_degradations(monkeypatch)

    result = CodexHooksAdapter().translate_from_hook_response(
        HookResponse(decision="allow", context="rich degradation context"),
        hook_type="PreCompact",
    )

    assert result["systemMessage"] == "rich degradation context"
    assert "hookSpecificOutput" not in result
    assert any(
        call["kind"] == "rerouted_field"
        and call["response_field"] == "context"
        and call["destination_channel"] == "systemMessage"
        for call in calls
    )


def test_graceful_error_uses_provider_capability_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_degradations(monkeypatch)

    qwen_result = _graceful_error_response(
        "PreToolUse",
        "database unavailable",
        source="qwen",
    )
    droid_result = _graceful_error_response(
        "PreToolUse",
        "database unavailable",
        source="droid",
    )

    assert "database unavailable" in qwen_result["hookSpecificOutput"]["additionalContext"]
    assert "systemMessage" not in qwen_result
    assert droid_result["continue"] is True
    assert "database unavailable" in droid_result["systemMessage"]
    assert any(call["kind"] == "graceful_error" for call in calls)


@pytest.mark.parametrize(
    ("adapter", "hook_type"),
    [
        (ClaudeCodeAdapter(), "session-start"),
        (CodexHooksAdapter(), "SessionStart"),
        (QwenAdapter(), "SessionStart"),
        (DroidAdapter(), "SessionStart"),
    ],
)
def test_first_hook_session_banner_is_injected_once(adapter, hook_type: str) -> None:
    banner = "Gobby Session ID: #42 (uuid-123)"

    result = adapter.translate_from_hook_response(
        HookResponse(
            decision="allow",
            system_message=banner,
            metadata={
                "session_id": "uuid-123",
                "session_ref": "#42",
                "_first_hook_for_session": True,
            },
        ),
        hook_type=hook_type,
    )

    assert "systemMessage" not in result
    context = result["hookSpecificOutput"]["additionalContext"]
    assert context.count(banner) == 1
