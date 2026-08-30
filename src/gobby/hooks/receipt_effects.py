"""Apply one-shot hook effects after a delivery receipt is acknowledged."""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

STAGED_EFFECTS_FIELD = "_gobby_staged_effects"

_WORKER_STAGING = threading.local()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for raw in value:
        text = str(raw)
        if text and text not in seen:
            items.append(text)
            seen.add(text)
    return items


def merge_staged_payloads(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge staged effect payloads without clobbering sibling keys."""

    merged = dict(current)
    for key, value in incoming.items():
        if key in {"session_variables", "append_set_variables"} and isinstance(value, dict):
            existing = merged.get(key)
            combined: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
            if key == "append_set_variables":
                for name, raw_values in value.items():
                    if not isinstance(name, str) or not name:
                        continue
                    prev = _string_list(combined.get(name))
                    seen = set(prev)
                    for item in _string_list(raw_values):
                        if item not in seen:
                            prev.append(item)
                            seen.add(item)
                    combined[name] = prev
            else:
                combined.update(value)
            merged[key] = combined
        elif key == "pending_message_ids" and isinstance(value, list):
            prev = list(merged[key]) if isinstance(merged.get(key), list) else []
            seen = set(prev)
            for item in value:
                if item not in seen:
                    prev.append(item)
                    seen.add(item)
            merged[key] = prev
        else:
            merged[key] = value
    return merged


def record_worker_staging(payload: dict[str, Any]) -> None:
    """Merge staged effects onto the current adapter worker."""

    current = getattr(_WORKER_STAGING, "payload", None)
    base = dict(current) if isinstance(current, dict) else {}
    _WORKER_STAGING.payload = merge_staged_payloads(base, payload)


def peek_worker_staging() -> dict[str, Any]:
    """Return staged effects recorded on this worker thread without clearing."""

    payload = getattr(_WORKER_STAGING, "payload", None)
    return dict(payload) if isinstance(payload, dict) else {}


def take_worker_staging() -> dict[str, Any]:
    """Return and clear staged effects recorded on this worker thread."""

    payload = getattr(_WORKER_STAGING, "payload", None)
    _WORKER_STAGING.payload = {}
    return dict(payload) if isinstance(payload, dict) else {}


def staged_append_set_values(name: str) -> set[str]:
    """Return already-staged set-union values for this worker turn."""

    staged = peek_worker_staging().get("append_set_variables")
    if not isinstance(staged, dict):
        return set()
    return set(_string_list(staged.get(name)))


def stage_append_set_variables(session_id: str, name: str, values: list[str]) -> None:
    """Stage set-union session variable values until the delivery receipt acks."""

    if not session_id or not name or not values:
        return
    record_worker_staging(
        {
            "session_id": session_id,
            "append_set_variables": {name: list(values)},
        }
    )


def apply_acknowledged_receipt(
    receipt: Any,
    *,
    message_manager: Any = None,
    variable_manager: Any = None,
) -> None:
    """Commit staged one-shot effects after a successful receipt CAS."""

    payload = getattr(receipt, "staged_payload", None)
    if not isinstance(payload, dict):
        return
    if message_manager is not None:
        raw_ids = payload.get("pending_message_ids")
        session_id = payload.get("pending_message_session_id")
        if isinstance(session_id, str) and session_id and isinstance(raw_ids, list):
            message_ids = [str(message_id) for message_id in raw_ids if str(message_id)]
            if message_ids:
                try:
                    message_manager.mark_delivered_batch(message_ids, session_id)
                except Exception:
                    logger.warning(
                        "Failed to mark piggyback messages delivered after receipt %s",
                        getattr(receipt, "receipt_id", None),
                        exc_info=True,
                    )
    session_id = payload.get("session_id") or getattr(receipt, "session_id", None)
    if variable_manager is None or not isinstance(session_id, str) or not session_id:
        return
    appends = payload.get("append_set_variables")
    if isinstance(appends, dict) and appends:
        for name, raw_values in appends.items():
            if not isinstance(name, str) or not name:
                continue
            values = _string_list(raw_values)
            if not values:
                continue
            try:
                variable_manager.append_to_set_variable(session_id, name, values)
            except Exception:
                logger.warning(
                    "Failed to append staged set variable %s after receipt %s",
                    name,
                    getattr(receipt, "receipt_id", None),
                    exc_info=True,
                )
    updates = payload.get("session_variables")
    if not isinstance(updates, dict) or not updates:
        return
    try:
        variable_manager.merge_variables(session_id, updates)
    except Exception:
        logger.warning(
            "Failed to persist staged session variables after receipt %s",
            getattr(receipt, "receipt_id", None),
            exc_info=True,
        )
