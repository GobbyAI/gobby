"""Provider capability registry and adapter degradation tests."""

from __future__ import annotations

import pytest

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
from gobby.adapters.gemini import GeminiAdapter
from gobby.adapters.grok import GrokAdapter
from gobby.adapters.qwen import QwenAdapter
from gobby.hooks.events import HookResponse, SessionSource
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
    assert get_provider_capabilities(SessionSource.GEMINI).hook_events.keys() == (
        GeminiAdapter.EVENT_MAP.keys()
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
    assert get_provider_capabilities(SessionSource.AGY).hook_events == {}


def test_current_context_and_decision_capabilities_are_declared() -> None:
    claude_pre_tool = get_provider_capabilities("claude").get_hook("pre-tool-use")
    codex_pre_tool = get_provider_capabilities("codex").get_hook("PreToolUse")
    gemini_before_model = get_provider_capabilities("gemini").get_hook("BeforeModel")
    grok_pre_tool = get_provider_capabilities("grok").get_hook("pre_tool_use")
    droid_pre_tool = get_provider_capabilities("droid").get_hook("PreToolUse")

    assert claude_pre_tool is not None
    assert claude_pre_tool.context_channel is ContextChannel.ADDITIONAL_CONTEXT
    assert claude_pre_tool.reason_format is ReasonFormat.CLAUDE_PRE_TOOL_COMPACT

    assert codex_pre_tool is not None
    assert codex_pre_tool.context_channel is ContextChannel.SYSTEM_MESSAGE
    assert codex_pre_tool.decision_style is ProviderDecisionStyle.PRE_TOOL_USE

    assert gemini_before_model is not None
    assert gemini_before_model.context_channel is ContextChannel.ADDITIONAL_CONTEXT
    assert gemini_before_model.supports_response_field("modify_args")

    assert grok_pre_tool is not None
    assert grok_pre_tool.context_channel is ContextChannel.SYSTEM_MESSAGE
    assert grok_pre_tool.decision_style is ProviderDecisionStyle.PRE_TOOL_USE

    assert droid_pre_tool is not None
    assert droid_pre_tool.context_channel is ContextChannel.NONE
    assert droid_pre_tool.decision_style is ProviderDecisionStyle.PRE_TOOL_USE


def test_unsupported_elicitation_fields_are_dropped_with_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_degradations(monkeypatch)

    result = GeminiAdapter().translate_from_hook_response(
        HookResponse(
            decision="allow",
            elicitation_action="accept",
            elicitation_content={"answer": "yes"},
        ),
        hook_type="BeforeTool",
    )

    assert result == {"decision": "allow", "continue": True}
    dropped_fields = {call["response_field"] for call in calls if call["kind"] == "dropped_field"}
    assert {"elicitation_action", "elicitation_content"} <= dropped_fields


def test_claude_reason_compaction_records_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_degradations(monkeypatch)
    reason = (
        "Rule enforced by Gobby: [require-code-index-skill]\n"
        'Call get_skill(name="code-index") on gobby-skills, then continue.'
    )

    result = ClaudeCodeAdapter().translate_from_hook_response(
        HookResponse(decision="block", reason=reason),
        hook_type="pre-tool-use",
    )

    permission_reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert permission_reason.startswith("Gobby blocked [require-code-index-skill]:")
    assert permission_reason != reason
    assert any(call["kind"] == "reason_compacted" for call in calls)


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

    gemini_result = _graceful_error_response(
        "BeforeTool",
        "database unavailable",
        source="gemini",
    )
    droid_result = _graceful_error_response(
        "PreToolUse",
        "database unavailable",
        source="droid",
    )

    assert gemini_result["decision"] == "allow"
    assert "database unavailable" in gemini_result["hookSpecificOutput"]["additionalContext"]
    assert droid_result["continue"] is True
    assert "database unavailable" in droid_result["systemMessage"]
    assert any(call["kind"] == "graceful_error" for call in calls)


@pytest.mark.parametrize(
    ("adapter", "hook_type"),
    [
        (ClaudeCodeAdapter(), "session-start"),
        (CodexHooksAdapter(), "SessionStart"),
        (GeminiAdapter(), "SessionStart"),
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
