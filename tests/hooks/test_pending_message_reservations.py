"""Concurrent hooks for one session inject a pending batch exactly once.

Parallel native hooks each read undelivered messages before any receipt is
acknowledged; the reservation makes the first reader own the batch while its
receipt is in flight, and lifts it whenever that delivery is not staged.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_enrichment import EventEnricher
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.pending_message_reservations import (
    RESERVATION_TTL_SECONDS,
    reserve_pending_messages,
)
from gobby.hooks.receipt_redelivery import DELIVERY_RECEIPT_FIELD, release_receipt_for_response
from gobby.storage.hook_receipts import HOOK_RECEIPT_REDELIVERY_GRACE, HookReceipt

pytestmark = pytest.mark.unit

SESSION = "sess-reserve"


def _message(msg_id: str, content: str = "hello") -> MagicMock:
    msg = MagicMock()
    msg.id = msg_id
    msg.content = content
    msg.message_type = "message"
    msg.from_session = "from-1111-2222-3333-444444444444"
    msg.priority = "normal"
    msg.metadata_json = None
    return msg


def _event() -> HookEvent:
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="ext-session-1",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
        metadata={"_platform_session_id": SESSION, "_native_hook_type": "post-tool-use"},
    )


def _enricher(manager: MagicMock) -> EventEnricher:
    return EventEnricher(
        session_manager=None,
        injected_sessions=set(),
        inter_session_msg_manager=manager,
    )


def _staged_ids(response: HookResponse) -> list[str]:
    staged = response.metadata.get("_gobby_staged_effects") or {}
    ids = staged.get("pending_message_ids") or []
    return [str(message_id) for message_id in ids]


def test_parallel_hooks_inject_one_batch_once() -> None:
    both_read = threading.Barrier(2)

    def read_after_both_arrive(_session_id: str) -> list[MagicMock]:
        both_read.wait(timeout=5)
        return [_message("msg-1"), _message("msg-2")]

    manager = MagicMock()
    manager.get_undelivered_messages.side_effect = read_after_both_arrive
    enricher = _enricher(manager)

    def run_hook() -> HookResponse:
        response = HookResponse()
        enricher.enrich(_event(), response)
        return response

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: run_hook(), range(2)))

    delivered = [response for response in responses if response.context]
    assert len(delivered) == 1
    assert _staged_ids(delivered[0]) == ["msg-1", "msg-2"]
    skipped = next(response for response in responses if response is not delivered[0])
    assert skipped.context is None
    assert _staged_ids(skipped) == []


def test_a_later_hook_delivers_only_messages_that_arrived_since() -> None:
    manager = MagicMock()
    manager.get_undelivered_messages.return_value = [_message("msg-1")]
    enricher = _enricher(manager)
    first = HookResponse()
    enricher.enrich(_event(), first)

    manager.get_undelivered_messages.return_value = [
        _message("msg-1"),
        _message("msg-2", content="second note"),
    ]
    second = HookResponse()
    enricher.enrich(_event(), second)

    assert _staged_ids(first) == ["msg-1"]
    assert _staged_ids(second) == ["msg-2"]
    assert second.context is not None
    assert "second note" in second.context
    assert "hello" not in second.context


def test_render_failure_leaves_the_batch_for_the_next_hook() -> None:
    manager = MagicMock()
    manager.get_undelivered_messages.return_value = [_message("msg-1")]
    enricher = _enricher(manager)
    failed = HookResponse()
    with patch(
        "gobby.hooks.event_enrichment.render_pending_messages",
        side_effect=RuntimeError("render failed"),
    ):
        enricher.enrich(_event(), failed)

    retried = HookResponse()
    enricher.enrich(_event(), retried)

    assert failed.context is None
    assert _staged_ids(retried) == ["msg-1"]


def test_messages_deferred_past_the_budget_stay_available() -> None:
    batch = [_message(f"msg-{index}", content="x" * 1900) for index in range(4)]
    manager = MagicMock()
    manager.get_undelivered_messages.return_value = batch
    enricher = _enricher(manager)
    first = HookResponse()
    enricher.enrich(_event(), first)

    second = HookResponse()
    enricher.enrich(_event(), second)

    assert _staged_ids(first) == ["msg-0", "msg-1", "msg-2"]
    assert _staged_ids(second) == ["msg-3"]


def test_reservation_lapses_no_later_than_receipt_carry_forward() -> None:
    now = [100.0]

    def clock() -> float:
        return now[0]

    held = reserve_pending_messages(SESSION, ["msg-1"], clock=clock)
    now[0] += RESERVATION_TTL_SECONDS - 0.001
    while_held = reserve_pending_messages(SESSION, ["msg-1"], clock=clock)
    now[0] += 0.001
    after_lapse = reserve_pending_messages(SESSION, ["msg-1"], clock=clock)

    assert RESERVATION_TTL_SECONDS <= HOOK_RECEIPT_REDELIVERY_GRACE.total_seconds()
    assert held == {"msg-1"}
    assert while_held == set()
    assert after_lapse == {"msg-1"}


def test_released_receipt_lifts_its_reservation_at_once() -> None:
    reserve_pending_messages(SESSION, ["msg-1", "msg-2"])
    released = HookReceipt(
        receipt_id="receipt-1",
        original_envelope_id="env-1",
        current_envelope_id="env-1",
        session_id=SESSION,
        delivery_generation=1,
        state="released",
        staged_payload={
            "pending_message_ids": ["msg-1"],
            "pending_message_session_id": SESSION,
        },
    )
    response: dict[str, Any] = {DELIVERY_RECEIPT_FIELD: {"receipt_id": "receipt-1"}}

    with patch("gobby.storage.hook_receipts.release_receipt", return_value=released):
        lifted = release_receipt_for_response(MagicMock(), response)

    assert lifted is True
    assert reserve_pending_messages(SESSION, ["msg-1", "msg-2"]) == {"msg-1"}
