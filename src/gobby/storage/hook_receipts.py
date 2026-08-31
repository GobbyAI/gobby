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
# A 'prepared' receipt is presumed lost (and re-prepared onto a newer envelope)
# only after this window; pipelined hooks otherwise outrun the client ack and
# every generation goes stale before its receipt file lands. 'released' rows are
# known lost and re-prepare immediately.
HOOK_RECEIPT_REDELIVERY_GRACE = timedelta(seconds=15)

ReceiptState = Literal["prepared", "acknowledged", "released", "terminal-undelivered"]


@dataclass(frozen=True)
class ReceiptPruneResult:
    """One bounded prune pass over terminal receipt rows and stale budgets."""

    deleted: int
    budgets_deleted: int = 0
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
            if receipt.state == "prepared" and staged_payload is not None:
                # A re-prepared receipt already carries this envelope; the
                # caller merged the lost delivery's effects into the payload.
                conn.execute(
                    "UPDATE hook_receipt_effects SET staged_payload = %s "
                    "WHERE receipt_id = %s AND state = 'prepared'",
                    (Jsonb(payload), receipt.receipt_id),
                )
                receipt = replace(receipt, staged_payload=dict(payload))
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


def release_and_reprepare_for_session(
    db: HubDatabase,
    *,
    session_id: str,
    envelope_id: str,
) -> HookReceipt | None:
    """Carry the newest undelivered receipt for a session onto a new envelope.

    One transaction: the newest ``released`` row — or ``prepared`` row older
    than ``HOOK_RECEIPT_REDELIVERY_GRACE`` — whose current envelope is not
    ``envelope_id`` is compare-and-set prepared->released (when still prepared)
    and then released->prepared onto ``envelope_id`` with the delivery
    generation incremented and ``staged_payload`` preserved. Returns ``None``
    when the session has nothing to re-deliver. The grace keeps a fresh
    ``prepared`` delivery (its ack still in flight) from being presumed lost
    by the next pipelined hook.
    """

    now = utc_now()
    with db.transaction() as conn:
        candidate = conn.execute(
            "SELECT receipt_id, state FROM hook_receipt_effects "
            "WHERE session_id = %s "
            "AND (state = 'released' OR (state = 'prepared' AND transition_at < %s)) "
            "AND current_envelope_id <> %s "
            "ORDER BY created_at DESC, receipt_id DESC LIMIT 1 FOR UPDATE SKIP LOCKED",
            (session_id, now - HOOK_RECEIPT_REDELIVERY_GRACE, envelope_id),
        ).fetchone()
        if candidate is None:
            return None
        receipt_id = str(candidate["receipt_id"])
        if candidate["state"] == "prepared":
            released = conn.execute(
                "UPDATE hook_receipt_effects SET state = 'released', transition_at = %s "
                "WHERE receipt_id = %s AND state = 'prepared' RETURNING receipt_id",
                (now, receipt_id),
            ).fetchone()
            if released is None:
                return None
        row = conn.execute(
            "UPDATE hook_receipt_effects SET state = 'prepared', "
            "current_envelope_id = %s, delivery_generation = delivery_generation + 1, "
            "transition_at = %s WHERE receipt_id = %s AND state = 'released' "
            "RETURNING " + _RECEIPT_COLUMNS,
            (envelope_id, now, receipt_id),
        ).fetchone()
    return _row_to_receipt(row) if row is not None else None


def find_undelivered_startup_context(
    db: HubDatabase,
    *,
    session_id: str,
) -> dict[str, Any] | None:
    """Return the staged startup-context claim of the newest undelivered receipt."""

    row = db.fetchone(
        "SELECT staged_payload FROM hook_receipt_effects "
        "WHERE session_id = %s AND state IN ('prepared', 'released') "
        "AND staged_payload ? 'startup_context' "
        "ORDER BY created_at DESC, receipt_id DESC LIMIT 1",
        (session_id,),
    )
    if row is None:
        return None
    startup = _payload_dict(row["staged_payload"]).get("startup_context")
    return dict(startup) if isinstance(startup, dict) else None


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


def _increment_force_continue_on_conn(
    conn: Any,
    session_id: str,
    execution_num: int,
) -> int:
    row = conn.execute(
        "INSERT INTO hook_force_continue_budgets "
        "(session_id, execution_num, count, updated_at) "
        "VALUES (%s, %s, 1, now()) "
        "ON CONFLICT (session_id, execution_num) "
        "DO UPDATE SET count = hook_force_continue_budgets.count + 1, updated_at = now() "
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
    """Delete terminal receipt rows and stale budgets strictly after the window.

    Prepared and released rows are never pruned. Eligibility is
    ``transition_at < now - HOOK_RECEIPT_IDEMPOTENCY_WINDOW`` (``updated_at``
    for force_continue budgets): a row exactly at the boundary is retained.
    Batches are bounded so a backlog drains across successive passes.
    """

    cutoff = (now if now is not None else utc_now()) - HOOK_RECEIPT_IDEMPOTENCY_WINDOW
    limit = HOOK_RECEIPT_PRUNE_MAX_ENTRIES if max_entries is None else max(0, max_entries)
    if limit == 0:
        return ReceiptPruneResult(deleted=0, budgets_deleted=0, truncated=False)
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
        budgets = conn.execute(
            "DELETE FROM hook_force_continue_budgets WHERE (session_id, execution_num) IN ("
            "SELECT session_id, execution_num FROM hook_force_continue_budgets "
            "WHERE updated_at < %s "
            "ORDER BY updated_at ASC "
            "LIMIT %s"
            ") RETURNING session_id",
            (cutoff, limit),
        ).fetchall()
    deleted = len(rows)
    budgets_deleted = len(budgets)
    return ReceiptPruneResult(
        deleted=deleted,
        budgets_deleted=budgets_deleted,
        truncated=deleted == limit or budgets_deleted == limit,
    )
