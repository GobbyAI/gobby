"""Persistence and publication for agent attention episodes."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
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
AttentionRosterKind = Literal["run", "session"]


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


@dataclass(frozen=True)
class AttentionRosterTerminal:
    """Terminal fields needed to assemble one roster entry without another query."""

    id: str
    backend: str
    state: str
    machine_id: str
    host_epoch: str | None
    session_name: str | None
    locator: Mapping[str, object] | None


@dataclass(frozen=True)
class AttentionRosterRow:
    """One run or interactive session loaded by the bounded roster query."""

    kind: AttentionRosterKind
    source_id: str
    session_id: str | None
    lifecycle_status: str
    task_id: str | None
    task_ref: str | None
    task_stage: str | None
    provider: str
    model: str | None
    pid: int | None
    updated_at: object
    terminal_context: Mapping[str, object]
    terminal_id: str | None
    terminal: AttentionRosterTerminal | None

    @classmethod
    def from_row(cls, row: Row) -> AttentionRosterRow:
        terminal_id = row.get("terminal_id")
        terminal = None
        if terminal_id is not None and row.get("terminal_backend") is not None:
            raw_locator = row.get("terminal_locator")
            terminal = AttentionRosterTerminal(
                id=str(terminal_id),
                backend=str(row["terminal_backend"]),
                state=str(row["terminal_state"]),
                machine_id=str(row["terminal_machine_id"]),
                host_epoch=(
                    str(row["terminal_host_epoch"])
                    if row.get("terminal_host_epoch") is not None
                    else None
                ),
                session_name=(
                    str(row["terminal_session_name"])
                    if row.get("terminal_session_name") is not None
                    else None
                ),
                locator=dict(raw_locator) if isinstance(raw_locator, Mapping) else None,
            )
        raw_context = row.get("terminal_context")
        return cls(
            kind=cast(AttentionRosterKind, str(row["roster_kind"])),
            source_id=str(row["source_id"]),
            session_id=str(row["session_id"]) if row.get("session_id") is not None else None,
            lifecycle_status=str(row["lifecycle_status"]),
            task_id=str(row["task_id"]) if row.get("task_id") is not None else None,
            task_ref=str(row["task_ref"]) if row.get("task_ref") is not None else None,
            task_stage=(str(row["task_stage"]) if row.get("task_stage") is not None else None),
            provider=str(row["provider"]),
            model=str(row["model"]) if row.get("model") is not None else None,
            pid=int(row["pid"]) if row.get("pid") is not None else None,
            updated_at=row.get("updated_at"),
            terminal_context=(dict(raw_context) if isinstance(raw_context, Mapping) else {}),
            terminal_id=str(terminal_id) if terminal_id is not None else None,
            terminal=terminal,
        )


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

    def load_roster_rows(
        self,
        machine_id: str,
        *,
        live_session_statuses: Sequence[str],
    ) -> list[AttentionRosterRow]:
        """Load runs, sessions, tasks, and terminals in one database round trip."""
        rows = self.db.fetchall(
            """
            SELECT
                'run' AS roster_kind,
                run.id::text AS source_id,
                run.child_session_id::text AS session_id,
                run.status AS lifecycle_status,
                run.task_id::text AS task_id,
                CASE
                    WHEN task.id IS NULL THEN NULL
                    WHEN task.seq_num IS NOT NULL THEN '#' || task.seq_num::text
                    ELSE LEFT(task.id::text, 8)
                END AS task_ref,
                current_stage.stage_name AS task_stage,
                run.provider,
                run.model,
                run.pid,
                run.updated_at,
                '{}'::jsonb AS terminal_context,
                run.terminal_id::text AS terminal_id,
                terminal.backend AS terminal_backend,
                terminal.state AS terminal_state,
                terminal.machine_id::text AS terminal_machine_id,
                terminal.host_epoch AS terminal_host_epoch,
                terminal.session_name AS terminal_session_name,
                terminal.locator AS terminal_locator
            FROM agent_runs run
            LEFT JOIN tasks task ON task.id = run.task_id
            LEFT JOIN LATERAL (
                SELECT stage.stage_name
                FROM task_stage_states stage
                WHERE stage.task_id = task.id AND stage.state != 'done'
                ORDER BY stage.position
                LIMIT 1
            ) current_stage ON TRUE
            LEFT JOIN terminals terminal ON terminal.id = run.terminal_id
            WHERE run.status IN ('queued', 'running', 'pending')
              AND run.machine_id = %s

            UNION ALL

            SELECT
                'session' AS roster_kind,
                session.id::text AS source_id,
                session.id::text AS session_id,
                session.status AS lifecycle_status,
                NULL::text AS task_id,
                NULL::text AS task_ref,
                NULL::text AS task_stage,
                session.source AS provider,
                session.model,
                NULL::bigint AS pid,
                session.updated_at,
                COALESCE(session.terminal_context, '{}'::jsonb) AS terminal_context,
                terminal.id::text AS terminal_id,
                terminal.backend AS terminal_backend,
                terminal.state AS terminal_state,
                terminal.machine_id::text AS terminal_machine_id,
                terminal.host_epoch AS terminal_host_epoch,
                terminal.session_name AS terminal_session_name,
                terminal.locator AS terminal_locator
            FROM sessions session
            LEFT JOIN LATERAL (
                SELECT candidate.*
                FROM terminals candidate
                WHERE candidate.session_id = session.id
                  AND candidate.state IN ('pending', 'live')
                ORDER BY candidate.updated_at DESC
                LIMIT 1
            ) terminal ON TRUE
            WHERE session.status = ANY(%s)

            ORDER BY roster_kind, source_id
            """,
            (machine_id, list(live_session_statuses)),
        )
        return [AttentionRosterRow.from_row(row) for row in rows]

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
        with self.db.transaction() as transaction:
            existing_row = transaction.execute(
                "SELECT * FROM attention_states WHERE entry_id = %s FOR UPDATE",
                (entry_id,),
            ).fetchone()
            existing = AttentionState.from_row(existing_row) if existing_row is not None else None

            if existing is None:
                target_payload = dict(payload or {})
                if state is None and not target_payload:
                    return AttentionTransitionResult(applied=False, current=None), False
                serialized_payload = json.dumps(target_payload)
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
            if "turn_lifecycle" not in target_payload:
                lifecycle = existing.payload.get("turn_lifecycle")
                if lifecycle is not None:
                    target_payload["turn_lifecycle"] = lifecycle
            if state is None:
                lifecycle = target_payload.get("turn_lifecycle")
                target_payload = {"turn_lifecycle": lifecycle} if lifecycle is not None else {}
            serialized_payload = json.dumps(target_payload)
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
            ) or (
                state is None
                and existing.state is None
                and existing.payload == target_payload
                and not mark_seen
            )
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
            next_payload = serialized_payload
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
