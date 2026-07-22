"""Persistence and publication for agent attention episodes."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, cast

from gobby.storage.hub.protocol import HubDatabase, Row
from gobby.utils.datetime import utc_now

logger = logging.getLogger(__name__)

AttentionKind = Literal["actionable", "non_actionable"]
AttentionStatus = Literal["blocked"]


def run_attention_entry_id(run_id: str) -> str:
    """Return the stable attention entry key for a spawned run."""
    return f"run:{run_id}"


def session_attention_entry_id(session_id: str) -> str:
    """Return the stable attention entry key for an interactive session."""
    return f"session:{session_id}"


AttentionPublisher = Callable[[dict[str, object]], None]
DatabaseRunner = Callable[..., Awaitable[Any]]


class AttentionOrderingCoordinator:
    """Assign one daemon epoch and monotonic cursor to ordered attention state."""

    def __init__(self, *, epoch: str | None = None) -> None:
        self.epoch = epoch or str(uuid.uuid4())
        self._seq = 0
        self._lock = asyncio.Lock()
        self._sync_lock = threading.RLock()

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    @property
    def seq(self) -> int:
        with self._sync_lock:
            return self._seq

    @contextmanager
    def synchronized(self) -> Iterator[None]:
        with self._sync_lock:
            yield

    def next_seq(self) -> int:
        with self._sync_lock:
            self._seq += 1
            return self._seq


@dataclass(frozen=True)
class AttentionState:
    """Latest durable attention episode for one roster entry."""

    entry_id: str
    run_id: str | None
    session_id: str | None
    attention_id: str
    state: AttentionStatus | None
    reason: str | None
    kind: AttentionKind | None
    fingerprint: str | None
    payload: dict[str, object]
    since: str | None
    seen_at: str | None
    updated_at: str

    @classmethod
    def from_row(cls, row: Row) -> AttentionState:
        payload = row.get("payload")
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            payload = {}
        return cls(
            entry_id=str(row["entry_id"]),
            run_id=str(row["run_id"]) if row.get("run_id") is not None else None,
            session_id=(str(row["session_id"]) if row.get("session_id") is not None else None),
            attention_id=str(row["attention_id"]),
            state=row.get("state"),
            reason=str(row["reason"]) if row.get("reason") is not None else None,
            kind=row.get("kind"),
            fingerprint=(str(row["fingerprint"]) if row.get("fingerprint") is not None else None),
            payload=dict(payload),
            since=_timestamp(row.get("since")),
            seen_at=_timestamp(row.get("seen_at")),
            updated_at=_timestamp(row.get("updated_at")) or "",
        )

    def event_payload(self, *, epoch: str, seq: int) -> dict[str, object]:
        """Build the lossless WebSocket/notification representation."""
        return {
            "epoch": epoch,
            "seq": seq,
            "entry_id": self.entry_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "attention_id": self.attention_id,
            "state": self.state,
            "reason": self.reason,
            "kind": self.kind,
            "fingerprint": self.fingerprint,
            "payload": self.payload,
            "since": self.since,
            "seen_at": self.seen_at,
        }


@dataclass(frozen=True)
class AttentionTransitionResult:
    """Outcome of one conditional attention transition."""

    applied: bool
    current: AttentionState | None


@dataclass(frozen=True)
class AttentionRosterSnapshot:
    """Cursor-bounded durable and transient state captured under ordering."""

    epoch: str
    seq: int
    states: tuple[AttentionState, ...]
    metadata: Mapping[str, Mapping[str, object]]


def _timestamp(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class AttentionStateManager:
    """Own the durable compare-and-set transition for attention state."""

    def __init__(
        self,
        db: HubDatabase,
        *,
        event_publisher: AttentionPublisher | None = None,
        notification_publisher: AttentionPublisher | None = None,
        epoch: str | None = None,
        ordering: AttentionOrderingCoordinator | None = None,
    ) -> None:
        self.db = db
        self._event_publisher = event_publisher
        self._notification_publisher = notification_publisher
        self.ordering = ordering or AttentionOrderingCoordinator(epoch=epoch)

    @property
    def epoch(self) -> str:
        return self.ordering.epoch

    @property
    def seq(self) -> int:
        return self.ordering.seq

    def get(self, entry_id: str) -> AttentionState | None:
        """Return the latest state for an entry."""
        row = self.db.fetchone(
            "SELECT * FROM attention_states WHERE entry_id = %s",
            (entry_id,),
        )
        return AttentionState.from_row(row) if row is not None else None

    def list_blocked(self) -> list[AttentionState]:
        """Return currently blocked entries, newest first."""
        rows = self.db.fetchall(
            """
            SELECT * FROM attention_states
            WHERE state = 'blocked'
            ORDER BY updated_at DESC, entry_id
            """
        )
        return [AttentionState.from_row(row) for row in rows]

    def snapshot(
        self,
        *,
        metadata_snapshot: Callable[[], Mapping[str, Mapping[str, object]]] | None = None,
    ) -> AttentionRosterSnapshot:
        """Capture all attention and transient metadata at one cursor."""
        with self.ordering.synchronized():
            rows = self.db.fetchall("SELECT * FROM attention_states ORDER BY entry_id")
            states = tuple(AttentionState.from_row(row) for row in rows)
            raw_metadata = metadata_snapshot() if metadata_snapshot is not None else {}
            metadata = MappingProxyType(
                {
                    entry_id: MappingProxyType(dict(value))
                    for entry_id, value in raw_metadata.items()
                }
            )
            return AttentionRosterSnapshot(
                epoch=self.ordering.epoch,
                seq=self.ordering.seq,
                states=states,
                metadata=metadata,
            )

    async def snapshot_async(
        self,
        run_db: DatabaseRunner,
        *,
        metadata_snapshot: Callable[[], Mapping[str, Mapping[str, object]]] | None = None,
    ) -> AttentionRosterSnapshot:
        async with self.ordering.lock:
            result = await run_db(self.snapshot, metadata_snapshot=metadata_snapshot)
        return cast(AttentionRosterSnapshot, result)

    async def transition_async(
        self,
        run_db: DatabaseRunner,
        entry_id: str,
        *,
        state: AttentionStatus | None,
        run_id: str | None = None,
        session_id: str | None = None,
        reason: str | None = None,
        kind: AttentionKind | None = None,
        fingerprint: str | None = None,
        payload: Mapping[str, object] | None = None,
        expected_attention_id: str | None = None,
        expected_fingerprint: str | None = None,
        mark_seen: bool = False,
    ) -> AttentionTransitionResult:
        async with self.ordering.lock:
            result = await run_db(
                self.transition,
                entry_id,
                state=state,
                run_id=run_id,
                session_id=session_id,
                reason=reason,
                kind=kind,
                fingerprint=fingerprint,
                payload=payload,
                expected_attention_id=expected_attention_id,
                expected_fingerprint=expected_fingerprint,
                mark_seen=mark_seen,
            )
        return cast(AttentionTransitionResult, result)

    def transition(
        self,
        entry_id: str,
        *,
        state: AttentionStatus | None,
        run_id: str | None = None,
        session_id: str | None = None,
        reason: str | None = None,
        kind: AttentionKind | None = None,
        fingerprint: str | None = None,
        payload: Mapping[str, object] | None = None,
        expected_attention_id: str | None = None,
        expected_fingerprint: str | None = None,
        mark_seen: bool = False,
    ) -> AttentionTransitionResult:
        """Conditionally move one entry into or out of a blocked episode.

        Expected identity fields make stale UI/monitor mutations affect zero rows.
        Re-reporting an unchanged blocked episode is a no-op. Clearing and later
        observing the same fingerprint creates a fresh episode and notification.
        """
        if not entry_id:
            raise ValueError("entry_id is required")
        if state == "blocked" and (reason is None or kind is None or fingerprint is None):
            raise ValueError("blocked transitions require reason, kind, and fingerprint")

        with self.ordering.synchronized():
            result, opened_episode = self._transition_locked(
                entry_id,
                state=state,
                run_id=run_id,
                session_id=session_id,
                reason=reason,
                kind=kind,
                fingerprint=fingerprint,
                payload=payload,
                expected_attention_id=expected_attention_id,
                expected_fingerprint=expected_fingerprint,
                mark_seen=mark_seen,
            )
            if not result.applied or result.current is None:
                return result

            seq = self.ordering.next_seq()
            event = result.current.event_payload(epoch=self.ordering.epoch, seq=seq)
            self._publish(self._event_publisher, event, "attention event")
            if opened_episode:
                self._publish(self._notification_publisher, event, "attention notification")
            return result

    def _transition_locked(
        self,
        entry_id: str,
        *,
        state: AttentionStatus | None,
        run_id: str | None,
        session_id: str | None,
        reason: str | None,
        kind: AttentionKind | None,
        fingerprint: str | None,
        payload: Mapping[str, object] | None,
        expected_attention_id: str | None,
        expected_fingerprint: str | None,
        mark_seen: bool,
    ) -> tuple[AttentionTransitionResult, bool]:
        now = utc_now()
        serialized_payload = json.dumps(dict(payload or {}))
        with self.db.transaction() as transaction:
            existing_row = transaction.execute(
                "SELECT * FROM attention_states WHERE entry_id = %s FOR UPDATE",
                (entry_id,),
            ).fetchone()
            existing = AttentionState.from_row(existing_row) if existing_row is not None else None

            if existing is None:
                if state is None:
                    return AttentionTransitionResult(applied=False, current=None), False
                attention_id = str(uuid.uuid4())
                inserted = transaction.execute(
                    """
                    INSERT INTO attention_states (
                        entry_id, run_id, session_id, attention_id, state, reason,
                        kind, fingerprint, payload, since, seen_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                    ON CONFLICT (entry_id) DO NOTHING
                    RETURNING *
                    """,
                    (
                        entry_id,
                        run_id,
                        session_id,
                        attention_id,
                        state,
                        reason,
                        kind,
                        fingerprint,
                        serialized_payload,
                        now,
                        now if mark_seen else None,
                        now,
                    ),
                ).fetchone()
                if inserted is None:
                    current_row = transaction.execute(
                        "SELECT * FROM attention_states WHERE entry_id = %s",
                        (entry_id,),
                    ).fetchone()
                    current = (
                        AttentionState.from_row(current_row) if current_row is not None else None
                    )
                    return AttentionTransitionResult(applied=False, current=current), False
                current = AttentionState.from_row(inserted)
                return AttentionTransitionResult(applied=True, current=current), True

            target_payload = dict(payload or {})
            unchanged = (
                state == "blocked"
                and existing.state == "blocked"
                and existing.run_id == (run_id or existing.run_id)
                and existing.session_id == (session_id or existing.session_id)
                and existing.reason == reason
                and existing.kind == kind
                and existing.fingerprint == fingerprint
                and existing.payload == target_payload
                and (not mark_seen or existing.seen_at is not None)
            ) or (state is None and existing.state is None and not mark_seen)
            if unchanged:
                return AttentionTransitionResult(applied=False, current=existing), False

            same_episode = (
                state == "blocked"
                and existing.state == "blocked"
                and existing.reason == reason
                and existing.kind == kind
                and existing.fingerprint == fingerprint
            )
            opened_episode = state == "blocked" and not same_episode
            attention_id = str(uuid.uuid4()) if opened_episode else existing.attention_id
            next_run_id = run_id if run_id is not None else existing.run_id
            next_session_id = session_id if session_id is not None else existing.session_id
            next_reason = reason if state == "blocked" else None
            next_kind = kind if state == "blocked" else None
            next_payload = serialized_payload if state == "blocked" else json.dumps({})
            next_since = (now if opened_episode else existing.since) if state == "blocked" else None
            next_seen_at = now if mark_seen else (existing.seen_at if state == "blocked" else None)

            conditions = ["entry_id = %s"]
            condition_params: list[object] = [entry_id]
            if expected_attention_id is not None:
                conditions.append("attention_id = %s")
                condition_params.append(expected_attention_id)
            if expected_fingerprint is not None:
                conditions.append("fingerprint = %s")
                condition_params.append(expected_fingerprint)
            # The SQL condition fragments are selected from fixed strings above.
            updated = transaction.execute(
                f"""
                UPDATE attention_states
                SET run_id = %s,
                    session_id = %s,
                    attention_id = %s,
                    state = %s,
                    reason = %s,
                    kind = %s,
                    fingerprint = %s,
                    payload = %s::jsonb,
                    since = %s,
                    seen_at = %s,
                    updated_at = %s
                WHERE {" AND ".join(conditions)}
                RETURNING *
                """,  # nosec B608
                (
                    next_run_id,
                    next_session_id,
                    attention_id,
                    state,
                    next_reason,
                    next_kind,
                    fingerprint if state == "blocked" else existing.fingerprint,
                    next_payload,
                    next_since,
                    next_seen_at,
                    now,
                    *condition_params,
                ),
            ).fetchone()
            if updated is None:
                current_row = transaction.execute(
                    "SELECT * FROM attention_states WHERE entry_id = %s",
                    (entry_id,),
                ).fetchone()
                current = AttentionState.from_row(current_row) if current_row is not None else None
                return AttentionTransitionResult(applied=False, current=current), False
            current = AttentionState.from_row(updated)
            return AttentionTransitionResult(applied=True, current=current), opened_episode

    @staticmethod
    def _publish(
        publisher: AttentionPublisher | None,
        event: dict[str, object],
        label: str,
    ) -> None:
        if publisher is None:
            return
        try:
            publisher(event)
        except Exception:
            logger.warning("Failed to publish %s", label, exc_info=True)
