"""Concurrent hooks for one session inject a pending batch exactly once.

Parallel native hooks each read undelivered messages before any receipt is
acknowledged; the reservation makes the first reader own the batch while its
receipt is in flight, and lifts it whenever that delivery is not staged. A
carried receipt never acknowledges ids its carrying response did not render.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_enrichment import EventEnricher
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.pending_message_reservations import (
    RESERVATION_TTL_SECONDS,
    release_pending_messages,
    reserve_pending_messages,
)
from gobby.hooks.receipt_effects import STAGED_EFFECTS_FIELD, apply_acknowledged_receipt
from gobby.hooks.receipt_redelivery import attach_delivery_receipt
from gobby.storage.hook_receipts import HookReceipt

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


def test_reservation_lapses_after_its_ttl() -> None:
    now = [100.0]

    def clock() -> float:
        return now[0]

    held = reserve_pending_messages(SESSION, ["msg-1"], clock=clock)
    now[0] += RESERVATION_TTL_SECONDS - 0.001
    while_held = reserve_pending_messages(SESSION, ["msg-1"], clock=clock)
    now[0] += 0.001
    after_lapse = reserve_pending_messages(SESSION, ["msg-1"], clock=clock)

    assert held.message_ids == {"msg-1"}
    assert while_held.message_ids == set()
    assert after_lapse.message_ids == {"msg-1"}


def test_a_late_release_leaves_a_newer_grant_in_place() -> None:
    stale = reserve_pending_messages(SESSION, ["msg-1"], clock=lambda: 100.0)
    lapsed = 100.0 + RESERVATION_TTL_SECONDS + 1
    newer = reserve_pending_messages(SESSION, ["msg-1"], clock=lambda: lapsed)

    release_pending_messages(SESSION, stale, stale.message_ids)
    third = reserve_pending_messages(SESSION, ["msg-1"], clock=lambda: lapsed + 0.1)

    assert (stale.message_ids, newer.message_ids, third.message_ids) == (
        frozenset({"msg-1"}),
        frozenset({"msg-1"}),
        frozenset(),
    )


def _carried_receipt(staged_payload: dict[str, Any]) -> HookReceipt:
    return HookReceipt(
        receipt_id="carried",
        original_envelope_id="env-1",
        current_envelope_id="env-2",
        session_id=SESSION,
        delivery_generation=2,
        state="prepared",
        staged_payload=staged_payload,
    )


def _attach_carrying(
    carried: HookReceipt, response: HookResponse, *, skip_empty_receipt: bool
) -> dict[str, Any]:
    """Attach a receipt that carries `carried` forward; return the payload it would ack."""
    with (
        patch(
            "gobby.storage.hook_receipts.release_and_reprepare_for_session",
            return_value=carried,
        ),
        patch("gobby.storage.hook_receipts.prepare_receipt", return_value=carried) as prepare,
    ):
        attach_delivery_receipt(
            {},
            db=MagicMock(),
            envelope_id="env-2",
            session_id=SESSION,
            staged_payload=response.metadata.get(STAGED_EFFECTS_FIELD),
            skip_empty_receipt=skip_empty_receipt,
        )
    payload: dict[str, Any] = prepare.call_args.kwargs["staged_payload"]
    return payload


def _codex_pretool_event() -> HookEvent:
    event = _event()
    event.source = SessionSource.CODEX
    event.event_type = HookEventType.BEFORE_TOOL
    event.metadata["_native_hook_type"] = "pre-tool-use"
    return event


@pytest.mark.parametrize("carrier", ["boundary_second", "codex_pretool"])
def test_carry_forward_acknowledges_only_what_the_carrying_response_rendered(
    carrier: str,
) -> None:
    now = [100.0]
    manager = MagicMock()
    manager.get_undelivered_messages.return_value = [_message("msg-1")]
    enricher = _enricher(manager)
    first, second, later = HookResponse(), HookResponse(), HookResponse()

    def reserve(session_id: str, message_ids: Any) -> Any:
        return reserve_pending_messages(session_id, message_ids, clock=lambda: now[0])

    with patch("gobby.hooks.event_enrichment.reserve_pending_messages", side_effect=reserve):
        enricher.enrich(_event(), first)
        now[0] += RESERVATION_TTL_SECONDS - 0.001
        if carrier == "codex_pretool":
            enricher.enrich(_codex_pretool_event(), second)
        else:
            enricher.enrich(_event(), second)
        carried = _carried_receipt(dict(first.metadata[STAGED_EFFECTS_FIELD]))
        acked = _attach_carrying(carried, second, skip_empty_receipt=carrier == "codex_pretool")
        apply_acknowledged_receipt(replace(carried, staged_payload=acked), message_manager=manager)
        now[0] += 0.002
        enricher.enrich(_event(), later)

    assert (bool(second.context), _staged_ids(second)) == (False, [])
    assert "pending_message_ids" not in acked
    manager.mark_delivered_batch.assert_not_called()
    assert _staged_ids(later) == ["msg-1"]
