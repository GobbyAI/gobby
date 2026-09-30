"""Receipt storage receives a session UUID, never an envelope or provider label."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.hooks.receipt_redelivery import (
    DELIVERY_RECEIPT_FIELD,
    attach_delivery_receipt,
    receipt_session_id,
)
from gobby.hooks.startup_claim_preflight import StartupClaimLease
from gobby.storage.hook_receipts import acknowledge_receipt, release_receipt
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

SESSION_ID = "a1111111-1111-4111-8111-111111111111"
OTHER_SESSION_ID = "b2222222-2222-4222-8222-222222222222"
ENVELOPE_ID = "n-0000000000001-session-identity"


@pytest.mark.parametrize(
    ("lease_id", "platform_id", "input_data", "envelope_id", "expected"),
    [
        (SESSION_ID, OTHER_SESSION_ID, {}, ENVELOPE_ID, SESSION_ID),
        ("provider-seat", SESSION_ID, {}, ENVELOPE_ID, SESSION_ID),
        (None, "#14680", {"session_id": SESSION_ID}, ENVELOPE_ID, SESSION_ID),
        (
            None,
            "",
            {"session_id": "provider-seat", "conversationId": SESSION_ID},
            ENVELOPE_ID,
            SESSION_ID,
        ),
        (None, "", {"conversation_id": f" {SESSION_ID.upper()} "}, ENVELOPE_ID, SESSION_ID),
        (None, "", {}, ENVELOPE_ID, None),
        (None, "", "malformed-input", OTHER_SESSION_ID, None),
    ],
    ids=[
        "lease-wins",
        "invalid-lease-falls-through",
        "invalid-platform-falls-through",
        "invalid-provider-label-falls-through",
        "uuid-normalized",
        "missing-session",
        "uuid-envelope-is-not-a-session",
    ],
)
def test_receipt_identity_requires_a_session_uuid(
    lease_id: str | None,
    platform_id: str,
    input_data: Any,
    envelope_id: str,
    expected: str | None,
) -> None:
    lease = StartupClaimLease(lease_id, 1, "owner") if lease_id is not None else None

    assert (
        receipt_session_id(
            claim_lease=lease,
            payload={"input_data": input_data},
            platform_session_id=platform_id,
            envelope_id=envelope_id,
        )
        == expected
    )


def test_missing_identity_skips_storage_and_unbudgeted_force_continue(
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = MagicMock()
    response = {"continue": True, "terminationBehavior": "force_continue"}

    attached = attach_delivery_receipt(
        response,
        db=db,
        envelope_id=ENVELOPE_ID,
        session_id=None,
        force_continue_execution_num=1,
    )

    assert attached == {"continue": True}
    db.transaction.assert_not_called()
    assert caplog.records == []


def test_valid_fallback_prepares_and_redelivers_on_the_same_session(
    temp_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session_id = receipt_session_id(
        claim_lease=None,
        payload={"input_data": {"conversation_id": SESSION_ID}},
        platform_session_id="provider-seat",
        envelope_id=ENVELOPE_ID,
    )
    first = attach_delivery_receipt(
        {"continue": True},
        db=temp_db,
        envelope_id=ENVELOPE_ID,
        session_id=session_id,
    )
    receipt = first[DELIVERY_RECEIPT_FIELD]
    assert (
        release_receipt(
            temp_db,
            receipt_id=receipt["receipt_id"],
            delivery_generation=receipt["delivery_generation"],
        )
        is not None
    )

    second = attach_delivery_receipt(
        {"continue": True},
        db=temp_db,
        envelope_id=f"{ENVELOPE_ID}-next",
        session_id=session_id,
    )
    redelivered = second[DELIVERY_RECEIPT_FIELD]
    acknowledged = acknowledge_receipt(
        temp_db,
        receipt_id=redelivered["receipt_id"],
        delivery_generation=redelivered["delivery_generation"],
    )

    assert redelivered["receipt_id"] == receipt["receipt_id"]
    assert redelivered["delivery_generation"] == 2
    assert acknowledged is not None
    assert acknowledged.session_id == SESSION_ID
    assert caplog.records == []


def test_redelivered_receipt_logs_at_debug_only(
    temp_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#22866: routine receipt redelivery stays off INFO."""
    first = attach_delivery_receipt(
        {"continue": True}, db=temp_db, envelope_id=ENVELOPE_ID, session_id=SESSION_ID
    )
    receipt = first[DELIVERY_RECEIPT_FIELD]
    release_receipt(
        temp_db,
        receipt_id=receipt["receipt_id"],
        delivery_generation=receipt["delivery_generation"],
    )

    with caplog.at_level(logging.DEBUG, logger="gobby.hooks.receipt_redelivery"):
        attach_delivery_receipt(
            {"continue": True},
            db=temp_db,
            envelope_id=f"{ENVELOPE_ID}-next",
            session_id=SESSION_ID,
        )

    redelivered = [r for r in caplog.records if "Re-delivering" in r.getMessage()]
    assert [r.levelno for r in redelivered] == [logging.DEBUG]
