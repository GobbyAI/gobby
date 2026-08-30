"""Cross-provider pending-message delivery contract tests."""

from __future__ import annotations

import importlib
from pathlib import Path
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
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

STAGED_EFFECTS_KEY = "_gobby_staged_effects"
RECIPIENT_SESSION_ID = "11111111-1111-4111-8111-111111111111"
_RECEIPT_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "crates/gcore/assets/schema/migrations/414_hook_receipt_effects.sql"
)

CONTEXT_PROVIDER_CASES: tuple[tuple[type[Any], str], ...] = (
    (ClaudeCodeAdapter, "user-prompt-submit"),
    (CodexHooksAdapter, "UserPromptSubmit"),
    (QwenAdapter, "UserPromptSubmit"),
    (DroidAdapter, "UserPromptSubmit"),
    (AgyAdapter, "PreInvocation"),
)

ALL_PROVIDER_CASES: tuple[tuple[type[Any], str], ...] = (
    *CONTEXT_PROVIDER_CASES,
    (GrokAdapter, "user_prompt_submit"),
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
            "hookEventName": hook_type,
            "prompt": "continue",
        },
    }


def _staged_pending(response: HookResponse) -> dict[str, Any]:
    staged = response.metadata[STAGED_EFFECTS_KEY]
    assert isinstance(staged, dict)
    return staged


def _enrich(
    adapter_type: type[Any],
    hook_type: str,
    *,
    content: str = "small lossless notification",
    session_manager: Any = None,
) -> tuple[HookResponse, MagicMock]:
    adapter = adapter_type()
    event = adapter.translate_to_hook_event(_native_event(hook_type))
    event.metadata["_platform_session_id"] = RECIPIENT_SESSION_ID
    message_manager = MagicMock()
    message_manager.get_undelivered_messages.return_value = [_message(content)]
    response = HookResponse(context="existing workflow context")
    EventEnricher(
        session_manager=session_manager,
        injected_sessions=set(),
        inter_session_msg_manager=message_manager,
    ).enrich(event, response)
    return response, message_manager


def _receipts() -> Any:
    return importlib.import_module("gobby.storage.hook_receipts")


def _apply_acknowledged_receipt() -> Any:
    inbox = importlib.import_module("gobby.hooks.inbox")
    apply = getattr(inbox, "apply_acknowledged_receipt", None)
    assert apply is not None
    return apply


@pytest.fixture
def receipts_db(temp_db: HubDatabase) -> HubDatabase:
    sql = "\n".join(
        line
        for line in _RECEIPT_MIGRATION.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    )
    statements = [statement.strip() for statement in sql.split(";") if statement.strip()]
    with temp_db.transaction() as conn:
        for statement in statements:
            conn.execute(statement)
    return temp_db


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
    event.metadata["_platform_session_id"] = RECIPIENT_SESSION_ID
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
    message_manager.mark_delivered_batch.assert_not_called()
    staged = _staged_pending(response)
    assert staged["pending_message_ids"] == ["msg-lossless"]
    assert staged["pending_message_session_id"] == RECIPIENT_SESSION_ID

    native_response = adapter.translate_from_hook_response(response, hook_type)
    assert expected_fragment in str(native_response)
    assert STAGED_EFFECTS_KEY not in native_response


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
    event.metadata["_platform_session_id"] = RECIPIENT_SESSION_ID
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


def test_agy_pre_tool_use_leaves_messages_pending() -> None:
    response, message_manager = _enrich(AgyAdapter, "PreToolUse", content="pending")

    assert response.context == "existing workflow context"
    message_manager.get_undelivered_messages.assert_not_called()
    message_manager.mark_delivered_batch.assert_not_called()
    assert STAGED_EFFECTS_KEY not in response.metadata


def test_grok_queues_pending_messages_without_marking_them() -> None:
    session_manager = MagicMock()
    with patch(
        "gobby.hooks.event_enrichment.grok_pending_context.enqueue_pending_messages",
        create=True,
    ) as enqueue:
        response, message_manager = _enrich(
            GrokAdapter,
            "user_prompt_submit",
            content="queued",
            session_manager=session_manager,
        )

    assert response.context == "existing workflow context"
    enqueue.assert_called_once()
    message_manager.mark_delivered_batch.assert_not_called()
    staged = _staged_pending(response)
    assert staged["pending_message_ids"] == ["msg-lossless"]
    assert staged["pending_message_session_id"] == RECIPIENT_SESSION_ID


@pytest.mark.parametrize(("adapter_type", "hook_type"), ALL_PROVIDER_CASES)
class TestPendingMessageReceiptCommit:
    def test_prepare_without_mark(
        self,
        adapter_type: type[Any],
        hook_type: str,
    ) -> None:
        with patch(
            "gobby.hooks.event_enrichment.grok_pending_context.enqueue_pending_messages",
            create=True,
        ):
            response, message_manager = _enrich(
                adapter_type,
                hook_type,
                session_manager=MagicMock(),
            )

        message_manager.mark_delivered_batch.assert_not_called()
        staged = _staged_pending(response)
        assert staged["pending_message_ids"] == ["msg-lossless"]
        assert staged["pending_message_session_id"] == RECIPIENT_SESSION_ID

    def test_acknowledged_commit(
        self,
        adapter_type: type[Any],
        hook_type: str,
        receipts_db: HubDatabase,
    ) -> None:
        with patch(
            "gobby.hooks.event_enrichment.grok_pending_context.enqueue_pending_messages",
            create=True,
        ):
            response, message_manager = _enrich(
                adapter_type,
                hook_type,
                session_manager=MagicMock(),
            )
        receipts = _receipts()
        receipt = receipts.prepare_receipt(
            receipts_db,
            session_id=RECIPIENT_SESSION_ID,
            envelope_id=f"env-{adapter_type.__name__}-ack",
            staged_payload=_staged_pending(response),
        )
        committed = receipts.acknowledge_receipt(
            receipts_db,
            receipt_id=receipt.receipt_id,
            delivery_generation=receipt.delivery_generation,
        )
        assert committed is not None
        _apply_acknowledged_receipt()(committed, message_manager=message_manager)
        message_manager.mark_delivered_batch.assert_called_once_with(
            ["msg-lossless"],
            RECIPIENT_SESSION_ID,
        )

    def test_transport_release(
        self,
        adapter_type: type[Any],
        hook_type: str,
        receipts_db: HubDatabase,
    ) -> None:
        with patch(
            "gobby.hooks.event_enrichment.grok_pending_context.enqueue_pending_messages",
            create=True,
        ):
            response, message_manager = _enrich(
                adapter_type,
                hook_type,
                session_manager=MagicMock(),
            )
        receipts = _receipts()
        receipt = receipts.prepare_receipt(
            receipts_db,
            session_id=RECIPIENT_SESSION_ID,
            envelope_id=f"env-{adapter_type.__name__}-release",
            staged_payload=_staged_pending(response),
        )
        released = receipts.release_receipt(receipts_db, receipt_id=receipt.receipt_id)
        assert released is not None
        assert released.state == "released"
        message_manager.mark_delivered_batch.assert_not_called()

    def test_duplicate_ack_is_a_noop(
        self,
        adapter_type: type[Any],
        hook_type: str,
        receipts_db: HubDatabase,
    ) -> None:
        with patch(
            "gobby.hooks.event_enrichment.grok_pending_context.enqueue_pending_messages",
            create=True,
        ):
            response, message_manager = _enrich(
                adapter_type,
                hook_type,
                session_manager=MagicMock(),
            )
        receipts = _receipts()
        receipt = receipts.prepare_receipt(
            receipts_db,
            session_id=RECIPIENT_SESSION_ID,
            envelope_id=f"env-{adapter_type.__name__}-dup",
            staged_payload=_staged_pending(response),
        )
        apply = _apply_acknowledged_receipt()
        first = receipts.acknowledge_receipt(
            receipts_db,
            receipt_id=receipt.receipt_id,
            delivery_generation=receipt.delivery_generation,
        )
        assert first is not None
        apply(first, message_manager=message_manager)
        duplicate = receipts.acknowledge_receipt(
            receipts_db,
            receipt_id=receipt.receipt_id,
            delivery_generation=receipt.delivery_generation,
        )
        assert duplicate is None
        message_manager.mark_delivered_batch.assert_called_once_with(
            ["msg-lossless"],
            RECIPIENT_SESSION_ID,
        )

    def test_daemon_restart_keeps_prepared_row(
        self,
        adapter_type: type[Any],
        hook_type: str,
        receipts_db: HubDatabase,
    ) -> None:
        with patch(
            "gobby.hooks.event_enrichment.grok_pending_context.enqueue_pending_messages",
            create=True,
        ):
            response, message_manager = _enrich(
                adapter_type,
                hook_type,
                session_manager=MagicMock(),
            )
        receipts = _receipts()
        receipt = receipts.prepare_receipt(
            receipts_db,
            session_id=RECIPIENT_SESSION_ID,
            envelope_id=f"env-{adapter_type.__name__}-restart",
            staged_payload=_staged_pending(response),
        )
        committed = receipts.acknowledge_receipt(
            receipts_db,
            receipt_id=receipt.receipt_id,
            delivery_generation=receipt.delivery_generation,
        )
        assert committed is not None
        assert committed.staged_payload["pending_message_ids"] == ["msg-lossless"]
        _apply_acknowledged_receipt()(committed, message_manager=message_manager)
        message_manager.mark_delivered_batch.assert_called_once_with(
            ["msg-lossless"],
            RECIPIENT_SESSION_ID,
        )

    def test_terminal_expiry_does_not_mark(
        self,
        adapter_type: type[Any],
        hook_type: str,
        receipts_db: HubDatabase,
    ) -> None:
        with patch(
            "gobby.hooks.event_enrichment.grok_pending_context.enqueue_pending_messages",
            create=True,
        ):
            response, message_manager = _enrich(
                adapter_type,
                hook_type,
                session_manager=MagicMock(),
            )
        receipts = _receipts()
        receipt = receipts.prepare_receipt(
            receipts_db,
            session_id=RECIPIENT_SESSION_ID,
            envelope_id=f"env-{adapter_type.__name__}-term",
            staged_payload=_staged_pending(response),
        )
        terminalized = receipts.terminalize_receipts_for_envelope(
            receipts_db,
            envelope_id=receipt.current_envelope_id,
        )
        assert terminalized == 1
        message_manager.mark_delivered_batch.assert_not_called()
