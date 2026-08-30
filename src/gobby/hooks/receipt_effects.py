"""Apply one-shot hook effects after a delivery receipt is acknowledged."""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

STAGED_EFFECTS_FIELD = "_gobby_staged_effects"

_WORKER_STAGING = threading.local()


def record_worker_staging(payload: dict[str, Any]) -> None:
    """Merge staged effects onto the current adapter worker."""

    current = getattr(_WORKER_STAGING, "payload", None)
    merged = dict(current) if isinstance(current, dict) else {}
    merged.update(payload)
    _WORKER_STAGING.payload = merged


def take_worker_staging() -> dict[str, Any]:
    """Return and clear staged effects recorded on this worker thread."""

    payload = getattr(_WORKER_STAGING, "payload", None)
    _WORKER_STAGING.payload = {}
    return dict(payload) if isinstance(payload, dict) else {}


def apply_acknowledged_receipt(
    receipt: Any,
    *,
    message_manager: Any = None,
) -> None:
    """Commit staged pending-message marks after a successful receipt CAS."""

    payload = getattr(receipt, "staged_payload", None)
    if not isinstance(payload, dict) or message_manager is None:
        return
    raw_ids = payload.get("pending_message_ids")
    session_id = payload.get("pending_message_session_id")
    if not isinstance(session_id, str) or not session_id or not isinstance(raw_ids, list):
        return
    message_ids = [str(message_id) for message_id in raw_ids if str(message_id)]
    if not message_ids:
        return
    try:
        message_manager.mark_delivered_batch(message_ids, session_id)
    except Exception:
        logger.warning(
            "Failed to mark piggyback messages delivered after receipt %s",
            getattr(receipt, "receipt_id", None),
            exc_info=True,
        )
