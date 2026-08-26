"""Cross-provider pending-message delivery contract tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.adapters.agy import AgyAdapter
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.droid import DroidAdapter
from gobby.adapters.grok import GrokAdapter
from gobby.adapters.qwen import QwenAdapter
from gobby.hooks.event_enrichment import EventEnricher
from gobby.hooks.events import HookResponse

pytestmark = pytest.mark.unit


CONTEXT_PROVIDER_CASES: tuple[tuple[type[Any], str], ...] = (
    (ClaudeCodeAdapter, "user-prompt-submit"),
    (CodexHooksAdapter, "UserPromptSubmit"),
    (QwenAdapter, "UserPromptSubmit"),
    (DroidAdapter, "UserPromptSubmit"),
)


def _message(content: str) -> MagicMock:
    message = MagicMock()
    message.id = "msg-lossless"
    message.from_session = "sender-session"
    message.to_session = "recipient-session"
    message.content = content
    message.priority = "normal"
    message.message_type = "message"
    message.metadata_json = None
    return message


def _native_event(hook_type: str) -> dict[str, Any]:
    return {
        "hook_type": hook_type,
        "input_data": {
            "session_id": "external-session",
            "prompt": "continue",
        },
    }


@pytest.mark.parametrize(("adapter_type", "hook_type"), CONTEXT_PROVIDER_CASES)
@pytest.mark.parametrize(
    ("content", "expected_fragment"),
    (
        pytest.param(
            "small lossless notification",
            "small lossless notification",
            id="inline",
        ),
        pytest.param(
            "large-lossless:" + ("x" * 32_597),
            'message_id="msg-lossless"',
            id="reference",
        ),
    ),
)
def test_context_capable_providers_share_inline_reference_and_ack_contract(
    adapter_type: type[Any],
    hook_type: str,
    content: str,
    expected_fragment: str,
) -> None:
    adapter = adapter_type()
    event = adapter.translate_to_hook_event(_native_event(hook_type))
    event.metadata["_platform_session_id"] = "recipient-session"
    message_manager = MagicMock()
    message_manager.get_undelivered_messages.return_value = [_message(content)]
    response = HookResponse(context="existing workflow context")
    enricher = EventEnricher(
        session_manager=None,
        injected_sessions=set(),
        inter_session_msg_manager=message_manager,
    )

    enricher.enrich(event, response)

    assert response.context is not None
    assert expected_fragment in response.context
    assert response.context.index(expected_fragment) < response.context.index(
        "existing workflow context"
    )
    if content.startswith("large-lossless:"):
        assert "large-lossless:" not in response.context
    message_manager.mark_delivered_batch.assert_called_once_with(
        ["msg-lossless"],
        "recipient-session",
    )

    native_response = adapter.translate_from_hook_response(response, hook_type)
    assert expected_fragment in str(native_response)


@pytest.mark.parametrize(
    ("adapter_type", "hook_type"),
    ((DroidAdapter, "PreToolUse"),),
)
def test_hooks_without_context_channels_leave_messages_pending(
    adapter_type: type[Any],
    hook_type: str,
) -> None:
    adapter = adapter_type()
    event = adapter.translate_to_hook_event(_native_event(hook_type))
    event.metadata["_platform_session_id"] = "recipient-session"
    message_manager = MagicMock()
    message_manager.get_undelivered_messages.return_value = [_message("pending")]
    response = HookResponse(context="existing workflow context")
    enricher = EventEnricher(
        session_manager=None,
        injected_sessions=set(),
        inter_session_msg_manager=message_manager,
    )

    enricher.enrich(event, response)

    assert response.context == "existing workflow context"
    message_manager.get_undelivered_messages.assert_not_called()
    message_manager.mark_delivered_batch.assert_not_called()


def test_agy_leaves_messages_pending_without_a_context_capable_hook() -> None:
    adapter = AgyAdapter()
    event = adapter.translate_to_hook_event(
        {
            "hook_event_name": "PreInvocation",
            "session_id": "external-session",
            "prompt": "continue",
        }
    )
    event.metadata["_platform_session_id"] = "recipient-session"
    message_manager = MagicMock()
    message_manager.get_undelivered_messages.return_value = [_message("pending")]
    response = HookResponse(context="existing workflow context")
    enricher = EventEnricher(
        session_manager=None,
        injected_sessions=set(),
        inter_session_msg_manager=message_manager,
    )

    enricher.enrich(event, response)

    assert response.context == "existing workflow context"
    message_manager.get_undelivered_messages.assert_not_called()
    message_manager.mark_delivered_batch.assert_not_called()


def test_grok_queues_pending_messages_for_acknowledged_active_delivery() -> None:
    adapter = GrokAdapter()
    event = adapter.translate_to_hook_event(_native_event("user_prompt_submit"))
    event.metadata["_platform_session_id"] = "recipient-session"
    message_manager = MagicMock()
    message_manager.get_undelivered_messages.return_value = [_message("queued")]
    session_manager = MagicMock()
    response = HookResponse(context="existing workflow context")
    enricher = EventEnricher(
        session_manager=session_manager,
        injected_sessions=set(),
        inter_session_msg_manager=message_manager,
    )

    with patch(
        "gobby.hooks.event_enrichment.grok_pending_context.enqueue_pending_messages",
        create=True,
    ) as enqueue:
        enricher.enrich(event, response)

    assert response.context == "existing workflow context"
    enqueue.assert_called_once()
    message_manager.mark_delivered_batch.assert_not_called()
