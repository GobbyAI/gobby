"""Delivery-receipt attachment, carry-forward, and transport release for hooks.

A durable hook envelope's response carries a versioned delivery receipt. Before
a new receipt is prepared, the session's newest undelivered receipt (prepared
but never acknowledged, or released after an emission failure) is re-prepared
onto the new envelope and its staged effects are merged into the new response,
so the provider sees the lost delivery again on its next live hook. When the
daemon fails to emit a receipted response, the receipt is released at once.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from starlette.types import Receive, Scope, Send

from gobby.adapters.agy_contract import AGY_FORCE_CONTINUE_LIMIT, strip_unbudgeted_force_continue
from gobby.hooks.receipt_effects import merge_staged_payloads
from gobby.hooks.startup_claim_preflight import StartupClaimLease
from gobby.servers.responses import JSONResponse

logger = logging.getLogger(__name__)

DELIVERY_RECEIPT_FIELD = "_gobby_delivery_receipt"


def receipt_session_id(
    *,
    claim_lease: StartupClaimLease | None,
    payload: dict[str, Any],
    platform_session_id: str,
    envelope_id: str,
) -> str:
    """Pick the canonical session identity a receipt is recorded against."""
    if claim_lease is not None and claim_lease.session_id:
        return claim_lease.session_id
    if platform_session_id:
        return platform_session_id
    input_data = payload.get("input_data")
    if isinstance(input_data, dict):
        for key in ("session_id", "conversationId", "conversation_id"):
            value = input_data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return envelope_id


def _carry_forward_staged_effects(
    db: Any,
    *,
    session_id: str,
    envelope_id: str,
    staged_payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Re-prepare the session's lost delivery onto this envelope and merge effects."""
    from gobby.storage.hook_receipts import release_and_reprepare_for_session

    try:
        carried = release_and_reprepare_for_session(
            db,
            session_id=session_id,
            envelope_id=envelope_id,
        )
    except Exception:
        logger.warning(
            "Failed to carry undelivered hook receipt forward onto envelope %s",
            envelope_id,
            exc_info=True,
        )
        return staged_payload
    if carried is None:
        return staged_payload
    logger.info(
        "Re-delivering hook receipt %s (generation %s) on envelope %s",
        carried.receipt_id,
        carried.delivery_generation,
        envelope_id,
    )
    return merge_staged_payloads(carried.staged_payload, staged_payload or {})


def attach_delivery_receipt(
    response: dict[str, Any],
    *,
    db: Any,
    envelope_id: str,
    session_id: str,
    staged_payload: dict[str, Any] | None = None,
    force_continue_execution_num: int | None = None,
) -> dict[str, Any]:
    """Prepare (or carry forward) the receipt for this envelope and attach it."""
    if db is None:
        return strip_unbudgeted_force_continue(response)
    try:
        from gobby.storage.hook_receipts import prepare_receipt

        staged_payload = _carry_forward_staged_effects(
            db,
            session_id=session_id,
            envelope_id=envelope_id,
            staged_payload=staged_payload,
        )
        receipt = prepare_receipt(
            db,
            session_id=session_id,
            envelope_id=envelope_id,
            staged_payload=staged_payload,
            force_continue_execution_num=force_continue_execution_num,
        )
    except Exception:
        logger.warning(
            "Failed to prepare hook delivery receipt for envelope %s",
            envelope_id,
            exc_info=True,
        )
        return strip_unbudgeted_force_continue(response)
    attached = dict(response)
    count = getattr(receipt, "force_continue_count", None)
    if isinstance(count, int) and count > AGY_FORCE_CONTINUE_LIMIT:
        attached.pop("terminationBehavior", None)
    attached[DELIVERY_RECEIPT_FIELD] = {
        "receipt_id": receipt.receipt_id,
        "original_envelope_id": receipt.original_envelope_id,
        "delivery_generation": receipt.delivery_generation,
    }
    return attached


def release_receipt_for_response(db: Any, response: Mapping[str, Any]) -> bool:
    """Release the receipt attached to a response whose emission failed."""
    receipt = response.get(DELIVERY_RECEIPT_FIELD)
    if db is None or not isinstance(receipt, dict):
        return False
    receipt_id = receipt.get("receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id:
        return False
    from gobby.storage.hook_receipts import release_receipt

    try:
        released = release_receipt(db, receipt_id=receipt_id)
    except Exception:
        logger.warning("Failed to release hook receipt %s", receipt_id, exc_info=True)
        return False
    if released is not None:
        logger.warning(
            "Released hook receipt %s after the response could not be emitted",
            receipt_id,
        )
    return released is not None


class ReceiptGuardedJSONResponse(JSONResponse):
    """JSON response that releases its delivery receipt when emission fails."""

    def __init__(self, content: dict[str, Any], *, db: Any) -> None:
        super().__init__(content=content)
        self._receipt_db = db
        self._receipt_content = content

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except BaseException:
            release_receipt_for_response(self._receipt_db, self._receipt_content)
            raise


def receipt_guarded_response(response: dict[str, Any], *, db: Any) -> Any:
    """Wrap a receipted response so a failed write releases the receipt."""
    if db is None or not isinstance(response.get(DELIVERY_RECEIPT_FIELD), dict):
        return response
    return ReceiptGuardedJSONResponse(response, db=db)
