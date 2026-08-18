"""Durable cleanup fence for conversation and session attachment scopes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from gobby.storage.hub.protocol import Transaction
from gobby.utils.datetime import parse_stored_datetime, utc_now

FenceKind = Literal["conversation", "session"]
FenceState = Literal["idle", "active", "terminal"]

CLEAR_CHAT_FENCE_SECONDS = 60


@dataclass(frozen=True)
class CleanupFence:
    scope_kind: FenceKind
    scope_id: str
    token: str | None
    owner: str | None
    claimed_at: datetime | None
    state: FenceState


class CleanupFenceConflict(ValueError):
    """Raised when a producer loses the cleanup-fence lock."""


def upsert_lock_fence(
    conn: Transaction,
    *,
    scope_kind: FenceKind,
    scope_id: str,
) -> CleanupFence:
    """Insert an idle fence if absent, then lock the existing row."""
    conn.execute(
        """
        INSERT INTO chat_attachment_cleanup_fences (
            scope_kind, scope_id, token, owner, claimed_at, state
        )
        VALUES (%s, %s, NULL, NULL, NULL, 'idle')
        ON CONFLICT (scope_kind, scope_id) DO NOTHING
        """,
        (scope_kind, scope_id),
    )
    row = conn.execute(
        """
        SELECT scope_kind, scope_id, token, owner, claimed_at, state
          FROM chat_attachment_cleanup_fences
         WHERE scope_kind = %s
           AND scope_id = %s
         FOR UPDATE
        """,
        (scope_kind, scope_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("cleanup fence vanished after upsert")
    claimed_at = row["claimed_at"]
    return CleanupFence(
        scope_kind=row["scope_kind"],
        scope_id=str(row["scope_id"]),
        token=None if row["token"] is None else str(row["token"]),
        owner=None if row["owner"] is None else str(row["owner"]),
        claimed_at=parse_stored_datetime(claimed_at) if claimed_at is not None else None,
        state=row["state"],
    )


def assert_producer_allowed(fence: CleanupFence) -> None:
    if fence.state == "idle":
        return
    if fence.state == "terminal":
        raise CleanupFenceConflict("cleanup fence is terminal")
    if fence.state == "active":
        raise CleanupFenceConflict("cleanup fence is active")


def acquire_cleanup_fence(
    conn: Transaction,
    *,
    scope_kind: FenceKind,
    scope_id: str,
    token: str,
    owner: str,
    reclaim_expired_clear_chat: bool = False,
) -> CleanupFence:
    fence = upsert_lock_fence(conn, scope_kind=scope_kind, scope_id=scope_id)
    if fence.state == "terminal":
        raise CleanupFenceConflict("cleanup fence is terminal")
    if fence.state == "active":
        expired = False
        if reclaim_expired_clear_chat and fence.claimed_at is not None:
            expired = fence.claimed_at < utc_now() - timedelta(seconds=CLEAR_CHAT_FENCE_SECONDS)
        if not expired:
            raise CleanupFenceConflict("cleanup fence is active")
    now = utc_now()
    conn.execute(
        """
        UPDATE chat_attachment_cleanup_fences
           SET token = %s,
               owner = %s,
               claimed_at = %s,
               state = 'active'
         WHERE scope_kind = %s
           AND scope_id = %s
        """,
        (token, owner, now, scope_kind, scope_id),
    )
    return CleanupFence(
        scope_kind=scope_kind,
        scope_id=scope_id,
        token=token,
        owner=owner,
        claimed_at=now,
        state="active",
    )


def finish_cleanup_fence(
    conn: Transaction,
    *,
    scope_kind: FenceKind,
    scope_id: str,
    terminal: bool,
) -> None:
    state: FenceState = "terminal" if terminal else "idle"
    conn.execute(
        """
        UPDATE chat_attachment_cleanup_fences
           SET token = NULL,
               owner = NULL,
               claimed_at = NULL,
               state = %s
         WHERE scope_kind = %s
           AND scope_id = %s
        """,
        (state, scope_kind, scope_id),
    )


def lock_producer_scopes(
    conn: Transaction,
    *,
    conversation_id: str | None,
    target_session_id: str | None,
) -> None:
    if conversation_id:
        assert_producer_allowed(
            upsert_lock_fence(conn, scope_kind="conversation", scope_id=conversation_id)
        )
    if target_session_id:
        assert_producer_allowed(
            upsert_lock_fence(conn, scope_kind="session", scope_id=target_session_id)
        )
