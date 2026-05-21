"""Durable pending interaction manager for web chat approval flows.

Coordinates tool approvals, plan approvals, and ask-user questions with
in-memory waiters backed by SQLite persistence. Replaces the bifurcated
approval state (pending_plan_path + in-memory asyncio.Event).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from gobby.storage.database import DatabaseProtocol, LocalDatabase

logger = logging.getLogger(__name__)


@dataclass
class PendingInteraction:
    """In-flight interaction waiting for user decision."""

    id: str
    session_id: str
    kind: str  # 'tool', 'plan', 'ask_user'
    provider: str
    tool_name: str | None
    payload: dict[str, Any]
    timeout_seconds: int
    status: str = "pending"
    decision: str | None = None
    response: dict[str, Any] | None = None


class PendingInteractionManager:
    """Coordinates durable pending interactions with in-memory waiters.

    Lifecycle:
    - create() inserts a DB row, creates an asyncio.Event, starts a timeout task
    - wait() blocks until the Event is set (resolve or expire)
    - resolve() sets the decision, wakes the waiter, updates DB
    - expire() marks expired in DB, wakes waiter with timeout decision
    - cleanup() cancels all timeout tasks (called on daemon shutdown)
    - expire_all_pending() marks all pending rows expired (called on startup)
    """

    def __init__(
        self,
        db: LocalDatabase | DatabaseProtocol,
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._db = db
        self._run_db = run_db
        self._waiters: dict[str, asyncio.Event] = {}
        self._results: dict[str, dict[str, Any]] = {}
        self._timeouts: dict[str, asyncio.Task[None]] = {}

    async def _run_sqlite(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db(func, *args, **kwargs)

    async def create(
        self,
        session_id: str,
        kind: str,
        provider: str,
        payload: dict[str, Any],
        tool_name: str | None = None,
        timeout_seconds: int = 300,
    ) -> str:
        """Insert DB row, create asyncio.Event, start timeout task.

        Returns interaction_id. Supersedes any existing pending interaction
        of the same (session_id, kind) before creating.
        """
        await self.supersede(session_id, kind)

        interaction_id = str(uuid.uuid4())

        payload_json = json.dumps(payload)

        # Retry loop for race condition with concurrent supersede + INSERT
        max_retries = 3
        for attempt in range(max_retries):
            try:
                await self._run_sqlite(
                    self._insert_pending,
                    interaction_id,
                    session_id,
                    kind,
                    provider,
                    tool_name,
                    payload_json,
                    timeout_seconds,
                )
                break
            except sqlite3.IntegrityError:
                if attempt < max_retries - 1:
                    await self.supersede(session_id, kind)
                    await asyncio.sleep(0.05 * (attempt + 1))
                    continue
                raise

        # Create in-memory waiter
        event = asyncio.Event()
        self._waiters[interaction_id] = event

        # Start timeout task
        timeout_task = asyncio.create_task(self._timeout_handler(interaction_id, timeout_seconds))
        self._timeouts[interaction_id] = timeout_task

        return interaction_id

    def _insert_pending(
        self,
        interaction_id: str,
        session_id: str,
        kind: str,
        provider: str,
        tool_name: str | None,
        payload_json: str,
        timeout_seconds: int,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """INSERT INTO pending_interactions
                   (id, session_id, kind, provider, tool_name,
                    payload_json, status, timeout_seconds)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    interaction_id,
                    session_id,
                    kind,
                    provider,
                    tool_name,
                    payload_json,
                    timeout_seconds,
                ),
            )

    async def wait(self, interaction_id: str) -> dict[str, Any]:
        """Block until resolved or timeout. Returns decision + response."""
        event = self._waiters.get(interaction_id)
        if not event:
            return {"decision": "deny", "reason": "unknown_interaction"}

        await event.wait()

        # Pop results and waiter
        result = self._results.pop(interaction_id, {"decision": "deny", "reason": "no_result"})
        self._waiters.pop(interaction_id, None)
        return result

    async def resolve(
        self,
        interaction_id: str,
        decision: str,
        response: dict[str, Any] | None = None,
    ) -> bool:
        """Set decision, wake waiter, update DB. Returns False if expired/missing."""
        # Update DB
        updated = await self._run_sqlite(
            self._resolve_pending,
            interaction_id,
            decision,
            json.dumps(response) if response else None,
        )
        if not updated:
            return False

        # Cancel timeout task
        timeout_task = self._timeouts.pop(interaction_id, None)
        if timeout_task:
            timeout_task.cancel()

        # Set result and wake waiter
        self._results[interaction_id] = {"decision": decision, "response": response}
        event = self._waiters.get(interaction_id)
        if event:
            event.set()

        return True

    def _resolve_pending(
        self,
        interaction_id: str,
        decision: str,
        response_json: str | None,
    ) -> bool:
        with self._db.transaction() as conn:
            result = conn.execute(
                """UPDATE pending_interactions
                   SET status = 'resolved', decision = ?, response_json = ?,
                       resolved_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status = 'pending'""",
                (decision, response_json, interaction_id),
            )
            return result.rowcount > 0

    async def expire(self, interaction_id: str) -> None:
        """Mark expired in DB, wake waiter with timeout decision."""
        await self._run_sqlite(self._expire_pending, interaction_id)

        # Cancel timeout task (may already be the one calling us)
        timeout_task = self._timeouts.pop(interaction_id, None)
        if timeout_task and not timeout_task.done():
            timeout_task.cancel()

        # Set result and wake waiter
        self._results[interaction_id] = {"decision": "timeout"}
        event = self._waiters.get(interaction_id)
        if event:
            event.set()

    def _expire_pending(self, interaction_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """UPDATE pending_interactions
                   SET status = 'expired', decision = 'timeout',
                       resolved_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status = 'pending'""",
                (interaction_id,),
            )

    async def rebroadcast(self, session_id: str) -> list[dict[str, Any]]:
        """Return all pending (non-expired, non-resolved) interactions for session.

        Only the latest non-expired per (session_id, kind) is returned.
        """
        rows = await self._run_sqlite(
            self._db.fetchall,
            """SELECT id, kind, provider, tool_name, payload_json, timeout_seconds
               FROM pending_interactions
               WHERE session_id = ? AND status = 'pending'
               ORDER BY created_at DESC""",
            (session_id,),
        )

        # Deduplicate: keep only the latest per kind
        seen_kinds: set[str] = set()
        result: list[dict[str, Any]] = []
        for row in rows:
            kind = row["kind"]
            if kind in seen_kinds:
                continue
            seen_kinds.add(kind)
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            result.append(
                {
                    "interaction_id": row["id"],
                    "kind": kind,
                    "provider": row["provider"],
                    "tool_name": row["tool_name"],
                    **payload,
                }
            )
        return result

    async def count_pending(self, session_id: str) -> int:
        """Count pending interactions for a session (rate limiting)."""
        row = await self._run_sqlite(
            self._db.fetchone,
            "SELECT COUNT(*) as cnt FROM pending_interactions WHERE session_id = ? AND status = 'pending'",
            (session_id,),
        )
        return row["cnt"] if row else 0

    async def cleanup(self) -> None:
        """Cancel all timeout tasks. Called on daemon shutdown."""
        for task in self._timeouts.values():
            task.cancel()
        # Await all cancelled tasks to suppress warnings
        if self._timeouts:
            await asyncio.gather(*self._timeouts.values(), return_exceptions=True)
        self._timeouts.clear()
        self._waiters.clear()
        self._results.clear()

    async def supersede(self, session_id: str, kind: str) -> None:
        """Expire any existing pending interaction of same (session_id, kind)."""
        rows = await self._run_sqlite(
            self._db.fetchall,
            "SELECT id FROM pending_interactions WHERE session_id = ? AND kind = ? AND status = 'pending'",
            (session_id, kind),
        )
        for row in rows:
            await self.expire(row["id"])

    async def expire_all_pending(self) -> None:
        """Mark all pending rows as expired. Called on daemon startup (fail-closed)."""
        await self._run_sqlite(self._expire_all_pending)

        # Clear any orphaned in-memory state
        for event in self._waiters.values():
            event.set()
        self._waiters.clear()
        self._results.clear()
        for task in self._timeouts.values():
            task.cancel()
        self._timeouts.clear()

    def _expire_all_pending(self) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """UPDATE pending_interactions
                   SET status = 'expired', decision = 'timeout',
                       resolved_at = CURRENT_TIMESTAMP
                   WHERE status = 'pending'"""
            )

    async def _timeout_handler(self, interaction_id: str, timeout_seconds: int) -> None:
        """Background task that expires an interaction after timeout."""
        try:
            await asyncio.sleep(timeout_seconds)
            await self.expire(interaction_id)
        except asyncio.CancelledError:
            pass
