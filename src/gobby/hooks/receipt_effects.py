"""Apply one-shot hook effects after a delivery receipt is acknowledged."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

STAGED_EFFECTS_FIELD = "_gobby_staged_effects"

# One hook delivery stages effects from three threads: the adapter worker that
# runs the handler, the single workflow runtime thread that evaluates its rules,
# and the rule-engine executor threads that run offloaded work. A
# ``threading.local`` gave each of them a private buffer that only the adapter
# worker ever drained, so staged effects were invisible across a hop (#21424)
# and piled up on the shared runtime and executor threads until an unrelated
# delivery read them back out (#21427).
#
# A ContextVar holding a mutable buffer scopes staging to one logical delivery
# instead. ``asyncio.run_coroutine_threadsafe`` and ``offload`` both copy the
# calling context, and a copy carries the same buffer object, so every thread
# working on this delivery shares one buffer while concurrent deliveries never
# see each other's — even when they land on the same runtime or executor thread.
_WORKER_STAGING: ContextVar[dict[str, Any] | None] = ContextVar(
    "gobby_worker_staging", default=None
)

# That buffer is shared across threads by design, so its read-modify-write needs
# a guard.
_STAGING_LOCK = threading.Lock()


@contextmanager
def worker_staging_scope() -> Iterator[None]:
    """Bind a fresh staging buffer for one logical hook delivery."""

    token = _WORKER_STAGING.set({})
    try:
        yield
    finally:
        _WORKER_STAGING.reset(token)


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
    """Merge staged effects onto the current hook delivery."""

    buffer = _WORKER_STAGING.get()
    if buffer is None:
        # Callers outside a scope — a test driving a handler directly, say —
        # get a buffer bound into their own context, which behaves like the
        # thread-local this replaced.
        buffer = {}
        _WORKER_STAGING.set(buffer)
    with _STAGING_LOCK:
        merged = merge_staged_payloads(dict(buffer), payload)
        buffer.clear()
        buffer.update(merged)


def peek_worker_staging() -> dict[str, Any]:
    """Return staged effects for this hook delivery without clearing."""

    buffer = _WORKER_STAGING.get()
    if buffer is None:
        return {}
    with _STAGING_LOCK:
        return dict(buffer)


def take_worker_staging() -> dict[str, Any]:
    """Return and clear staged effects for this hook delivery."""

    buffer = _WORKER_STAGING.get()
    if buffer is None:
        return {}
    with _STAGING_LOCK:
        staged = dict(buffer)
        # Cleared in place so every thread sharing this buffer sees the drain.
        buffer.clear()
    return staged


def staged_append_set_values(name: str) -> set[str]:
    """Return already-staged set-union values for this hook delivery."""

    staged = peek_worker_staging().get("append_set_variables")
    if not isinstance(staged, dict):
        return set()
    return set(_string_list(staged.get(name)))


def staged_session_variable(name: str) -> Any:
    """Return this hook delivery's staged value for a session variable, or None."""

    staged = peek_worker_staging().get("session_variables")
    return staged.get(name) if isinstance(staged, dict) else None


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


def apply_acknowledged_marks(receipt: Any, *, message_manager: Any = None) -> None:
    """Mark the piggybacked messages a won receipt delivered. Never raises."""

    payload = getattr(receipt, "staged_payload", None)
    if message_manager is None or not isinstance(payload, dict):
        return
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


def apply_acknowledged_variables(receipt: Any, *, variable_manager: Any) -> None:
    """Persist a won receipt's session effects. A failed write raises.

    The caller holds the transaction that also wrote the receipt's acknowledgment, so a raise
    rolls the acknowledgment back and the receipt stays prepared for redelivery.
    """

    payload = getattr(receipt, "staged_payload", None)
    if not isinstance(payload, dict):
        return
    session_id = payload.get("session_id") or getattr(receipt, "session_id", None)
    if not isinstance(session_id, str) or not session_id:
        return
    startup = payload.get("startup_context")
    commit = getattr(variable_manager, "commit_startup_context", None)
    if callable(commit) and isinstance(startup, dict):
        generation = startup.get("generation")
        owner_token = startup.get("owner_token")
        startup_session = startup.get("session_id") or session_id
        if (
            isinstance(generation, int)
            and isinstance(owner_token, str)
            and owner_token
            and isinstance(startup_session, str)
            and startup_session
        ):
            commit(startup_session, generation, owner_token)
    appends = payload.get("append_set_variables")
    if isinstance(appends, dict) and appends:
        for name, raw_values in appends.items():
            if not isinstance(name, str) or not name:
                continue
            values = _string_list(raw_values)
            if not values:
                continue
            variable_manager.append_to_set_variable(session_id, name, values)
    updates = payload.get("session_variables")
    if isinstance(updates, dict) and updates:
        variable_manager.merge_variables(session_id, updates)


def variable_effect_sessions(owner: Any, payload: Any) -> frozenset[str]:
    """Every session whose variable row apply_acknowledged_variables may write for a receipt.

    The receipt's owner always counts, so the acknowledgment's lock covers its own row too.
    """
    sessions: set[str] = set()
    if isinstance(owner, str) and owner:
        sessions.add(owner)
    if isinstance(payload, str) and payload:
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = None
    if not isinstance(payload, dict):
        return frozenset(sessions)
    target = payload.get("session_id") or owner
    if isinstance(target, str) and target:
        sessions.add(target)
    startup = payload.get("startup_context")
    if isinstance(startup, dict):
        startup_session = startup.get("session_id") or target
        if isinstance(startup_session, str) and startup_session:
            sessions.add(startup_session)
    return frozenset(sessions)


def apply_acknowledged_receipt(
    receipt: Any,
    *,
    message_manager: Any = None,
    variable_manager: Any = None,
) -> None:
    """Apply a won receipt's variable effects, then its message marks."""

    if variable_manager is not None:
        apply_acknowledged_variables(receipt, variable_manager=variable_manager)
    apply_acknowledged_marks(receipt, message_manager=message_manager)
