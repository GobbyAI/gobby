"""Resolve compact continuations independently of observed provider identity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gobby.sessions.handoff_identity import terminal_process_contexts_match
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session


@dataclass(frozen=True)
class CompactIdentityResolution:
    """Unique compact continuation candidate or a bounded conflict."""

    session: Session | None = None
    conflicting_session_ids: tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return bool(self.conflicting_session_ids)


def resolve_compact_continuation(
    db: HubDatabase,
    *,
    machine_id: str,
    source: str,
    terminal_context: dict[str, Any] | None,
) -> CompactIdentityResolution:
    """Resolve a unique marked compact row for an exact terminal process."""
    if not machine_id or not source or not terminal_context:
        return CompactIdentityResolution()

    rows = db.fetchall(
        """
        SELECT s.*, COALESCE(sv.variables ->> 'handoff_source', '') AS compact_marker
        FROM sessions s
        LEFT JOIN session_variables sv ON sv.session_id = s.id
        WHERE s.machine_id = %s
          AND s.source = %s
          AND s.session_type = 'terminal'
          AND s.status IN ('handoff_ready', 'expired')
        ORDER BY s.created_at, s.id
        """,
        (machine_id, source),
    )
    matching = [
        candidate
        for row in rows
        if (
            (candidate := Session.from_row(row)).status == "handoff_ready"
            or row["compact_marker"] == "compact"
        )
        and terminal_process_contexts_match(candidate.terminal_context, terminal_context)
    ]
    if len(matching) == 1:
        return CompactIdentityResolution(session=matching[0])
    if len(matching) > 1:
        return CompactIdentityResolution(
            conflicting_session_ids=tuple(candidate.id for candidate in matching)
        )
    return CompactIdentityResolution()
