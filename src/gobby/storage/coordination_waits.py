"""Durable, idempotent subscriptions to coordination release or owner status."""

from __future__ import annotations

import json
import math
import uuid
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions._constants import ALLOWED_SESSION_STATUSES


class CoordinationWaitManager:
    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def has_active_wait(self, session_id: str) -> bool:
        """Protect unresolved, unexpired holds with an existing nonterminal owner.

        Delivery acknowledgement is irrelevant: expiry ends protection even
        when the terminal outcome is still awaiting delivery.
        """
        row = self.db.fetchone(
            "SELECT EXISTS (SELECT 1 FROM coordination_waits w "
            "JOIN sessions owner ON owner.id = w.owner_session_id "
            "WHERE w.waiter_session_id = %s AND w.outcome = 'waiting' "
            "AND w.expires_at > clock_timestamp() "
            "AND owner.status NOT IN ('completed', 'cancelled', 'closed', 'expired', 'deleted')"
            ") AS active",
            (session_id,),
        )
        return row is not None and row["active"] is True

    def register(
        self,
        waiter_session_id: str,
        owner_session_id: str,
        *,
        coordination_key: str | None = None,
        statuses: list[str] | None = None,
        timeout: float = 900,
    ) -> dict[str, Any]:
        if (coordination_key is None) == (statuses is None):
            raise ValueError("Provide exactly one of coordination_key or statuses")
        if coordination_key is not None and (
            not coordination_key.strip() or len(coordination_key) > 256
        ):
            raise ValueError("coordination_key must contain 1–256 characters")
        if statuses is not None:
            if not statuses or any(status not in ALLOWED_SESSION_STATUSES for status in statuses):
                raise ValueError("statuses must be a nonempty set of canonical session statuses")
            statuses = sorted(set(statuses))
        if isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise ValueError("timeout must be greater than zero and at most 3600 seconds")
        if waiter_session_id == owner_session_id:
            raise ValueError("The coordination owner must be another session")
        condition_key = json.dumps([coordination_key, statuses], separators=(",", ":"))
        with self.db.transaction() as conn:
            # Fence committed message/status producers before checking/registering.
            owner = conn.execute(
                "SELECT id FROM sessions WHERE id = %s FOR UPDATE", (owner_session_id,)
            ).fetchone()
            existing = conn.execute(
                "SELECT * FROM coordination_waits WHERE waiter_session_id = %s "
                "AND owner_session_id = %s AND condition_key = %s",
                (waiter_session_id, owner_session_id, condition_key),
            ).fetchone()
            if existing is not None:
                wait_id = existing["id"]
            else:
                if owner is None:
                    raise ValueError("Coordination owner session does not exist")
                if (
                    conn.execute(
                        "SELECT id FROM sessions WHERE id = %s", (waiter_session_id,)
                    ).fetchone()
                    is None
                ):
                    raise ValueError("Waiting session does not exist")
                wait_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO coordination_waits "
                    "(id, waiter_session_id, owner_session_id, condition_key, coordination_key, "
                    "statuses, expires_at) VALUES (%s, %s, %s, %s, %s, %s, "
                    "clock_timestamp() + %s * interval '1 second')",
                    (
                        wait_id,
                        waiter_session_id,
                        owner_session_id,
                        condition_key,
                        coordination_key,
                        statuses,
                        timeout,
                    ),
                )
            conn.execute("SELECT resolve_coordination_wait(%s)", (wait_id,))
            row = conn.execute(
                "SELECT * FROM coordination_waits WHERE id = %s", (wait_id,)
            ).fetchone()
            assert row is not None
            return dict(row)

    def cancel(self, wait_id: str, waiter_session_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM coordination_waits WHERE id = %s AND waiter_session_id = %s "
                "FOR UPDATE",
                (wait_id, waiter_session_id),
            ).fetchone()
            if row is None:
                raise ValueError("Coordination wait does not belong to this session")
            conn.execute("SELECT resolve_coordination_wait(%s)", (wait_id,))
            conn.execute(
                "UPDATE coordination_waits SET outcome = 'cancelled', "
                "completed_at = clock_timestamp() WHERE id = %s AND outcome = 'waiting'",
                (wait_id,),
            )
            row = conn.execute(
                "SELECT * FROM coordination_waits WHERE id = %s", (wait_id,)
            ).fetchone()
            assert row is not None
            return dict(row)

    def refresh(self, wait_id: str, machine_id: str) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT w.id FROM coordination_waits w JOIN sessions s "
                "ON s.id = w.waiter_session_id WHERE w.id = %s AND s.machine_id = %s",
                (wait_id, machine_id),
            ).fetchone()
            if row is None:
                return None
            conn.execute("SELECT resolve_coordination_wait(%s)", (wait_id,))
            row = conn.execute(
                "SELECT * FROM coordination_waits WHERE id = %s", (wait_id,)
            ).fetchone()
            return dict(row) if row else None

    def pending_ids(self, machine_id: str) -> list[str]:
        return [
            row["id"]
            for row in self.db.fetchall(
                "SELECT w.id FROM coordination_waits w JOIN sessions s ON s.id = w.waiter_session_id "
                "WHERE s.machine_id = %s AND w.delivered_at IS NULL",
                (machine_id,),
            )
        ]

    def mark_delivered(self, wait_id: str) -> None:
        self.db.execute(
            "UPDATE coordination_waits SET delivered_at = clock_timestamp() "
            "WHERE id = %s AND outcome <> 'waiting' AND delivered_at IS NULL",
            (wait_id,),
        )


def coordination_wait_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": True,
        "wait_id": row["id"],
        "owner_session_id": str(row["owner_session_id"]),
        "outcome": row["outcome"],
        "completed": row["outcome"] != "waiting",
        "notification_registered": row["outcome"] == "waiting",
        "expires_at": row["expires_at"].isoformat(),
        "matched_status": row["matched_status"],
        "message_id": row["message_id"],
    }
