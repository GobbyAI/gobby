"""Relational receipt-effects storage authority. DML only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal
from uuid import uuid4

from psycopg.types.json import Jsonb

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now

HOOK_RECEIPT_IDEMPOTENCY_WINDOW = timedelta(hours=24)

ReceiptState = Literal["prepared", "acknowledged", "released", "terminal-undelivered"]

_RECEIPT_COLUMNS = (
    "receipt_id, original_envelope_id, current_envelope_id, session_id, "
    "delivery_generation, state, staged_payload, transition_at, created_at"
)


@dataclass(frozen=True)
class HookReceipt:
    """One delivery obligation across however many envelopes carry it."""

    receipt_id: str
    original_envelope_id: str
    current_envelope_id: str
    session_id: str
    delivery_generation: int
    state: ReceiptState
    staged_payload: dict[str, Any]


def _row_to_receipt(row: Any) -> HookReceipt:
    payload = row["staged_payload"]
    if not isinstance(payload, dict):
        payload = {}
    state = row["state"]
    return HookReceipt(
        receipt_id=str(row["receipt_id"]),
        original_envelope_id=str(row["original_envelope_id"]),
        current_envelope_id=str(row["current_envelope_id"]),
        session_id=str(row["session_id"]),
        delivery_generation=int(row["delivery_generation"]),
        state=state,
        staged_payload=payload,
    )


def prepare_receipt(
    db: HubDatabase,
    *,
    session_id: str,
    envelope_id: str,
    staged_payload: dict[str, Any] | None = None,
) -> HookReceipt:
    """Insert a prepared receipt for the first durable envelope."""

    receipt_id = str(uuid4())
    payload = staged_payload or {}
    now = utc_now()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO hook_receipt_effects ("
            "receipt_id, original_envelope_id, current_envelope_id, session_id, "
            "delivery_generation, state, staged_payload, transition_at, created_at"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                receipt_id,
                envelope_id,
                envelope_id,
                session_id,
                1,
                "prepared",
                Jsonb(payload),
                now,
                now,
            ),
        )
        row = conn.execute(
            f"SELECT {_RECEIPT_COLUMNS} FROM hook_receipt_effects WHERE receipt_id = %s",
            (receipt_id,),
        ).fetchone()
    assert row is not None
    return _row_to_receipt(row)


def acknowledge_receipt(
    db: HubDatabase,
    *,
    receipt_id: str,
    delivery_generation: int,
) -> HookReceipt | None:
    """CAS prepared (or in-window terminal-undelivered) to acknowledged."""

    now = utc_now()
    cutoff = now - HOOK_RECEIPT_IDEMPOTENCY_WINDOW
    with db.transaction() as conn:
        row = conn.execute(
            "UPDATE hook_receipt_effects SET state = 'acknowledged', transition_at = %s "
            "WHERE receipt_id = %s AND delivery_generation = %s AND ("
            "state = 'prepared' OR ("
            "state = 'terminal-undelivered' AND transition_at > %s"
            ")) RETURNING " + _RECEIPT_COLUMNS,
            (now, receipt_id, delivery_generation, cutoff),
        ).fetchone()
    if row is None:
        existing = _get_receipt(db, receipt_id)
        if (
            existing is not None
            and existing.state == "acknowledged"
            and existing.delivery_generation == delivery_generation
        ):
            return existing
        return None
    return _row_to_receipt(row)


def release_receipt(db: HubDatabase, *, receipt_id: str) -> HookReceipt | None:
    """CAS prepared to released for transport loss."""

    now = utc_now()
    with db.transaction() as conn:
        row = conn.execute(
            "UPDATE hook_receipt_effects SET state = 'released', transition_at = %s "
            "WHERE receipt_id = %s AND state = 'prepared' RETURNING " + _RECEIPT_COLUMNS,
            (now, receipt_id),
        ).fetchone()
    return _row_to_receipt(row) if row is not None else None


def reprepare_receipt(
    db: HubDatabase,
    *,
    receipt_id: str,
    envelope_id: str,
) -> HookReceipt | None:
    """CAS released to prepared onto the next carrying envelope."""

    now = utc_now()
    with db.transaction() as conn:
        row = conn.execute(
            "UPDATE hook_receipt_effects SET state = 'prepared', "
            "current_envelope_id = %s, delivery_generation = delivery_generation + 1, "
            "transition_at = %s WHERE receipt_id = %s AND state = 'released' "
            "RETURNING " + _RECEIPT_COLUMNS,
            (envelope_id, now, receipt_id),
        ).fetchone()
    return _row_to_receipt(row) if row is not None else None


def terminalize_receipts_for_envelope(db: HubDatabase, *, envelope_id: str) -> int:
    """CAS prepared or released rows for this carrying envelope to undelivered."""

    now = utc_now()
    with db.transaction() as conn:
        result = conn.execute(
            "UPDATE hook_receipt_effects SET state = 'terminal-undelivered', "
            "transition_at = %s WHERE current_envelope_id = %s "
            "AND state IN ('prepared', 'released')",
            (now, envelope_id),
        )
        return int(getattr(result, "rowcount", 0) or 0)


def increment_force_continue(
    db: HubDatabase,
    session_id: str,
    execution_num: int,
) -> int:
    """Atomically increment the per-execution force_continue budget."""

    with db.transaction() as conn:
        row = conn.execute(
            "INSERT INTO hook_force_continue_budgets (session_id, execution_num, count) "
            "VALUES (%s, %s, 1) "
            "ON CONFLICT (session_id, execution_num) "
            "DO UPDATE SET count = hook_force_continue_budgets.count + 1 "
            "RETURNING count",
            (session_id, execution_num),
        ).fetchone()
    assert row is not None
    return int(row["count"])


def _get_receipt(db: HubDatabase, receipt_id: str) -> HookReceipt | None:
    with db.transaction() as conn:
        row = conn.execute(
            f"SELECT {_RECEIPT_COLUMNS} FROM hook_receipt_effects WHERE receipt_id = %s",
            (receipt_id,),
        ).fetchone()
    return _row_to_receipt(row) if row is not None else None
