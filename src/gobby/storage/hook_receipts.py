"""Relational receipt-effects storage authority. DML only."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from psycopg.types.json import Jsonb

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now

HOOK_RECEIPT_IDEMPOTENCY_WINDOW = timedelta(hours=24)
HOOK_RECEIPT_PRUNE_MAX_ENTRIES = 100_000

ReceiptState = Literal["prepared", "acknowledged", "released", "terminal-undelivered"]


@dataclass(frozen=True)
class ReceiptPruneResult:
    """One bounded prune pass over terminal receipt-effects rows."""

    examined: int
    deleted: int
    truncated: bool = False


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
    force_continue_count: int | None = None


def _payload_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _row_to_receipt(row: Any) -> HookReceipt:
    payload = _payload_dict(row["staged_payload"])
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
    force_continue_execution_num: int | None = None,
) -> HookReceipt:
    """Insert a prepared receipt for the first durable envelope."""

    payload = staged_payload or {}
    now = utc_now()
    with db.transaction() as conn:
        existing = conn.execute(
            f"SELECT {_RECEIPT_COLUMNS} FROM hook_receipt_effects "
            "WHERE current_envelope_id = %s AND state IN ('prepared', 'acknowledged') "
            "ORDER BY created_at ASC LIMIT 1",
            (envelope_id,),
        ).fetchone()
        if existing is not None:
            receipt = _row_to_receipt(existing)
            if force_continue_execution_num is None:
                return receipt
            existing_count = _force_continue_count_on_conn(
                conn, session_id, force_continue_execution_num
            )
            return replace(receipt, force_continue_count=existing_count)
        budget_count: int | None = None
        if force_continue_execution_num is not None:
            budget_count = _increment_force_continue_on_conn(
                conn, session_id, force_continue_execution_num
            )
        receipt_id = str(uuid4())
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
    receipt = _row_to_receipt(row)
    if budget_count is not None:
        return replace(receipt, force_continue_count=budget_count)
    return receipt


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
        return _increment_force_continue_on_conn(conn, session_id, execution_num)


def _increment_force_continue_on_conn(
    conn: Any,
    session_id: str,
    execution_num: int,
) -> int:
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


def _force_continue_count_on_conn(
    conn: Any,
    session_id: str,
    execution_num: int,
) -> int | None:
    row = conn.execute(
        "SELECT count FROM hook_force_continue_budgets "
        "WHERE session_id = %s AND execution_num = %s",
        (session_id, execution_num),
    ).fetchone()
    if row is None:
        return None
    return int(row["count"])


def terminalize_receipts_for_session(db: HubDatabase, *, session_id: str) -> int:
    """CAS prepared or released rows for this session to undelivered."""

    now = utc_now()
    with db.transaction() as conn:
        result = conn.execute(
            "UPDATE hook_receipt_effects SET state = 'terminal-undelivered', "
            "transition_at = %s WHERE session_id = %s "
            "AND state IN ('prepared', 'released')",
            (now, session_id),
        )
        return int(getattr(result, "rowcount", 0) or 0)


def retire_force_continue_budgets(db: HubDatabase, *, session_id: str) -> int:
    """Drop every force_continue budget row for a finished session."""

    with db.transaction() as conn:
        rows = conn.execute(
            "DELETE FROM hook_force_continue_budgets WHERE session_id = %s RETURNING session_id",
            (session_id,),
        ).fetchall()
    return len(rows)


def retire_session_hook_effects(db: HubDatabase, *, session_id: str) -> int:
    """Terminalize receipts and retire budgets for one completed session."""

    now = utc_now()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE hook_receipt_effects SET state = 'terminal-undelivered', "
            "transition_at = %s WHERE session_id = %s "
            "AND state IN ('prepared', 'released')",
            (now, session_id),
        )
        rows = conn.execute(
            "DELETE FROM hook_force_continue_budgets WHERE session_id = %s RETURNING session_id",
            (session_id,),
        ).fetchall()
    return len(rows)


def retire_expired_session_hook_effects(db: HubDatabase) -> int:
    """Retire budgets and prepared receipts for sessions already marked expired."""

    now = utc_now()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE hook_receipt_effects SET state = 'terminal-undelivered', "
            "transition_at = %s WHERE state IN ('prepared', 'released') "
            "AND session_id IN (SELECT id FROM sessions WHERE status = 'expired')",
            (now,),
        )
        rows = conn.execute(
            "DELETE FROM hook_force_continue_budgets "
            "WHERE session_id IN (SELECT id FROM sessions WHERE status = 'expired') "
            "RETURNING session_id"
        ).fetchall()
    return len(rows)


def prune_hook_receipts(
    db: HubDatabase,
    *,
    now: datetime | None = None,
    max_entries: int | None = None,
) -> ReceiptPruneResult:
    """Delete acknowledged and terminal-undelivered rows strictly after the window.

    Prepared and released rows are never pruned. Eligibility is
    ``transition_at < now - HOOK_RECEIPT_IDEMPOTENCY_WINDOW``: a row exactly at
    the boundary is retained. Batches are bounded so a backlog drains across
    successive passes.
    """

    cutoff = (now if now is not None else utc_now()) - HOOK_RECEIPT_IDEMPOTENCY_WINDOW
    limit = HOOK_RECEIPT_PRUNE_MAX_ENTRIES if max_entries is None else max(0, max_entries)
    if limit == 0:
        return ReceiptPruneResult(examined=0, deleted=0, truncated=False)
    with db.transaction() as conn:
        rows = conn.execute(
            "DELETE FROM hook_receipt_effects WHERE receipt_id IN ("
            "SELECT receipt_id FROM hook_receipt_effects "
            "WHERE state IN ('acknowledged', 'terminal-undelivered') "
            "AND transition_at < %s "
            "ORDER BY transition_at ASC "
            "LIMIT %s"
            ") RETURNING receipt_id",
            (cutoff, limit),
        ).fetchall()
    deleted = len(rows)
    return ReceiptPruneResult(
        examined=deleted,
        deleted=deleted,
        truncated=deleted == limit,
    )
