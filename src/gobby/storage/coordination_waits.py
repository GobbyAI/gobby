"""Durable, idempotent subscriptions to a release, an owner status, or a reply."""

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
        return self.count_active_waits(session_id) > 0

    def count_active_waits(self, session_id: str) -> int:
        """Count the same unresolved holds that protect a session's stop."""
        row = self.db.fetchone(
            "SELECT COUNT(*) AS active_count FROM coordination_waits w "
            "JOIN sessions owner ON owner.id = w.owner_session_id "
            "WHERE w.waiter_session_id = %s AND w.outcome = 'waiting' "
            "AND w.expires_at > clock_timestamp() "
            "AND owner.status NOT IN ('completed', 'cancelled', 'closed', 'expired', 'deleted')",
            (session_id,),
        )
        return int(row["active_count"]) if row is not None else 0

    def register(
        self,
        waiter_session_id: str,
        owner_session_id: str,
        *,
        coordination_key: str | None = None,
        statuses: list[str] | None = None,
        reply: bool = False,
        background_task_id: str | None = None,
        timeout: float = 900,
    ) -> dict[str, Any]:
        conditions = (coordination_key, statuses, reply or None, background_task_id)
        if sum(condition is not None for condition in conditions) != 1:
            raise ValueError(
                "Provide exactly one of coordination_key, statuses, reply, or background_task_id"
            )
        if background_task_id is not None:
            # The session holds on its own provider background task. Nothing releases a
            # self-owned keyed wait, so only expiry or the next prompt ends it.
            if not background_task_id.strip() or len(background_task_id) > 256:
                raise ValueError("background_task_id must contain 1–256 characters")
            if waiter_session_id != owner_session_id:
                raise ValueError("A background task wait's owner must be the waiting session")
            coordination_key = background_task_id
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
        if background_task_id is None and waiter_session_id == owner_session_id:
            raise ValueError("The coordination owner must be another session")
        # A reply wait names neither a key nor statuses, so [null,null] is its identity.
        condition_key = json.dumps([coordination_key, statuses], separators=(",", ":"))
        with self.db.transaction() as conn:
            # Fence committed message/status producers before checking/registering.
            owner = conn.execute(
                "SELECT id FROM sessions WHERE id = %s FOR UPDATE", (owner_session_id,)
            ).fetchone()
            existing = conn.execute(
                "SELECT * FROM coordination_waits WHERE waiter_session_id = %s "
                "AND owner_session_id = %s AND condition_key = %s AND outcome = 'waiting'",
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
                    "statuses, reply, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, "
                    "clock_timestamp() + %s * interval '1 second')",
                    (
                        wait_id,
                        waiter_session_id,
                        owner_session_id,
                        condition_key,
                        coordination_key,
                        statuses,
                        bool(reply),
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

    def clear_background_waits(self, session_id: str) -> int:
        """End the session's background task waits when its next prompt arrives.

        That prompt is the provider's completion notification or the operator, so the
        turn is already live: mark the outcome delivered in the same write to keep the
        delivery service from waking the session into it.
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE coordination_waits SET outcome = 'cancelled', "
                "completed_at = clock_timestamp(), delivered_at = clock_timestamp() "
                "WHERE waiter_session_id = %s AND owner_session_id = waiter_session_id "
                "AND outcome = 'waiting'",
                (session_id,),
            )
            return cursor.rowcount

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

    def rearm_lapsed(self, machine_id: str, grace_seconds: int = 900) -> int:
        """Give waits whose deadline passed while no resolver ran a fresh grace window.

        Run once at daemon start: a deadline that ran out during downtime is not a
        timeout the waiter could have observed, so the hold survives the restart.
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE coordination_waits w "
                "SET expires_at = clock_timestamp() + make_interval(secs => %s) "
                "FROM sessions s WHERE s.id = w.waiter_session_id AND s.machine_id = %s "
                "AND w.outcome = 'waiting' AND w.expires_at <= clock_timestamp()",
                (grace_seconds, machine_id),
            )
            return cursor.rowcount

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
