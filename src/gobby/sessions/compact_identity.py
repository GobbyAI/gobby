"""Resolve compact continuations independently of observed provider identity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gobby.sessions.handoff_identity import terminal_process_contexts_match
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session

MAX_COMPACT_CONTINUATION_CANDIDATES = 250


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
    source: str,
    terminal_context: dict[str, Any] | None,
) -> CompactIdentityResolution:
    """Resolve a unique marked compact row for an exact terminal process."""
    if not source or not terminal_context:
        return CompactIdentityResolution()

    rows = db.fetchall(
        """
        SELECT s.*, p.name AS project_name, COALESCE(sv.variables ->> 'handoff_source', '') AS compact_marker
        FROM sessions s
              LEFT JOIN projects p ON p.id = s.project_id
        LEFT JOIN session_variables sv ON sv.session_id = s.id
        WHERE s.source = %s
          AND s.session_type = 'terminal'
        ORDER BY s.created_at DESC, s.id DESC
        LIMIT %s
        """,
        (source, MAX_COMPACT_CONTINUATION_CANDIDATES),
    )
    process_rows = [
        (row, candidate)
        for row in rows
        if terminal_process_contexts_match(
            (candidate := Session.from_row(row)).terminal_context, terminal_context
        )
    ]
    # An expired marked row revives only as its process's newest session: a newer row
    # means the process moved on and the compaction it marked never continued.
    matching = [
        candidate
        for index, (row, candidate) in enumerate(process_rows)
        if candidate.status == "awaiting_handoff"
        or (index == 0 and candidate.status == "expired" and row["compact_marker"] == "compact")
    ]
    if len(matching) == 1:
        return CompactIdentityResolution(session=matching[0])
    if len(matching) > 1:
        return CompactIdentityResolution(
            conflicting_session_ids=tuple(candidate.id for candidate in matching)
        )
    return CompactIdentityResolution()
