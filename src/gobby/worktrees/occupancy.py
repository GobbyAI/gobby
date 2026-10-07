"""The guard every managed worktree delete runs: a live session inside blocks removal."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions._constants import LIVE_SESSION_STATUS_ORDER
from gobby.utils.machine_id import require_machine_id
from gobby.worktrees.containment import path_is_within


def refuse_occupied_worktree(
    db: HubDatabase,
    worktree_path: str,
    *,
    worktree_id: str | None = None,
    exclude_session_id: str | None = None,
) -> str | None:
    """Name this machine's live sessions working in or bound to the worktree, if any.

    A session's working directory counts when it is the worktree or a descendant,
    and the worktree's claimant counts wherever it runs. Deleting the directory
    under a live session strands its process on a dead cwd (#23631).
    ``exclude_session_id`` is the session asking for its own worktree's removal.
    """
    rows = db.fetchall(
        """
        WITH bound AS (SELECT agent_session_id FROM worktrees WHERE id = %s)
        SELECT s.id::text AS id, s.seq_num, s.workspace_path,
               s.project_id::text AS project_id, p.name AS project_name,
               s.id IN (SELECT agent_session_id FROM bound) AS bound
        FROM sessions s
        LEFT JOIN projects p ON p.id = s.project_id
        WHERE s.status = ANY(%s)
          AND s.machine_id = %s
          AND (s.workspace_path IS NOT NULL OR s.id IN (SELECT agent_session_id FROM bound))
        """,
        (worktree_id, list(LIVE_SESSION_STATUS_ORDER), require_machine_id()),
    )
    refs = sorted(
        {
            _session_ref(row)
            for row in rows
            if row["id"] != exclude_session_id
            and (
                row["bound"]
                or (row["workspace_path"] and path_is_within(row["workspace_path"], worktree_path))
            )
        }
    )
    if not refs:
        return None
    noun = "session" if len(refs) == 1 else "sessions"
    return f"Live {noun} {', '.join(refs)} working in {worktree_path}; it was not deleted"


def _session_ref(row: Mapping[str, Any]) -> str:
    # Session.ref without loading whole session rows.
    if row["seq_num"] is None:
        return str(row["id"])
    project = str(row["project_name"] or "").strip() or str(row["project_id"])
    return f"{project}#{row['seq_num']}"
