"""Owner, next action and blocker recorded for a registered worktree."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from gobby.storage.hub.protocol import HubDatabase


@dataclass(frozen=True)
class WorktreeNote:
    owner: str
    next_action: str
    blocker: str
    updated_at: datetime
    eta: str | None = None


class WorktreeNoteStore:
    """Reads and writes the reconcile note the status report pairs with a worktree row."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def upsert(
        self,
        worktree_id: str,
        *,
        owner: str,
        next_action: str,
        blocker: str,
        updated_at: datetime,
        eta: str | None = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO worktree_notes (worktree_id, owner, next_action, blocker, updated_at, eta)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (worktree_id) DO UPDATE SET
                owner = EXCLUDED.owner,
                next_action = EXCLUDED.next_action,
                blocker = EXCLUDED.blocker,
                updated_at = EXCLUDED.updated_at,
                eta = EXCLUDED.eta
            """,
            (worktree_id, owner, next_action, blocker, updated_at, eta),
        )

    def list_for(self, worktree_ids: Sequence[str]) -> dict[str, WorktreeNote]:
        if not worktree_ids:
            return {}
        rows = self.db.fetchall(
            """
            SELECT worktree_id, owner, next_action, blocker, updated_at, eta
            FROM worktree_notes
            WHERE worktree_id = ANY(%s::uuid[])
            """,
            (list(worktree_ids),),
        )
        return {
            str(row["worktree_id"]): WorktreeNote(
                owner=row["owner"],
                next_action=row["next_action"],
                blocker=row["blocker"],
                updated_at=row["updated_at"],
                eta=row["eta"],
            )
            for row in rows
        }
