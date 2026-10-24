"""Who may address the Orchestrator, and which stale seat refs follow a relaunch (plan 3.4).

Roles come from each session's durable ``_agent_type``. Only Assistant and Triage address
the Orchestrator; every seat still reaches Triage, its Dev Manager and its peers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from gobby.storage.hub.protocol import HubDatabase, Row
from gobby.storage.sessions import LIVE_SESSION_STATUSES, system_session_id

ORCHESTRATOR = "orchestrator"
TRIAGE = "triage"
# The Orchestrator's own sessions keep rotation briefs and successor notes.
ORCHESTRATOR_SENDERS = frozenset({"assistant", TRIAGE, ORCHESTRATOR})
# Admin seats are one per project, so a stale ref to one follows its relaunch.
ADMIN_ROLES = frozenset({"assistant", ORCHESTRATOR, "archivist", "release-manager", TRIAGE})
RECIPIENT_REFUSED = "message_recipient_not_allowed"
REFUSAL = (
    "Only Assistant and Triage message the Orchestrator. Report to Triage or your Dev Manager."
)


@dataclass(frozen=True)
class RecipientPolicy:
    withheld: frozenset[str]  # recipients this sender may not address
    queued: bool  # Triage to the Orchestrator: an omitted wake stays queued


_SEATS = (
    "SELECT s.id, s.agent_run_id, s.agent_depth, v.variables ->> '_agent_type' AS role "
    "FROM sessions s LEFT JOIN session_variables v ON v.session_id = s.id "
)


def operator_terminal(row: Row) -> bool:
    """The operator's own terminal is no seat: no agent run, no depth and no seat role."""
    return not row["agent_run_id"] and not row["agent_depth"] and row["role"] in (None, "default")


def triage_or_operator(db: HubDatabase, session_id: str) -> bool:
    """Triage or the operator's own terminal; an unknown session fails closed."""
    row = db.fetchone(_SEATS + "WHERE s.id = %s::uuid", (session_id,))
    return row is not None and (row["role"] == TRIAGE or operator_terminal(row))


def recipient_policy(
    db: HubDatabase, sender_id: str, recipient_ids: Sequence[str]
) -> RecipientPolicy:
    if sender_id == system_session_id():
        return RecipientPolicy(frozenset(), queued=False)
    rows = db.fetchall(_SEATS + "WHERE s.id = ANY(%s::uuid[])", ([sender_id, *recipient_ids],))
    roles = {str(row["id"]): row["role"] for row in rows}
    orchestrators = frozenset(r for r in recipient_ids if roles.get(r) == ORCHESTRATOR)
    sender = next((row for row in rows if str(row["id"]) == sender_id), None)
    role = sender["role"] if sender else None
    # An unknown sender fails closed.
    if role in ORCHESTRATOR_SENDERS or (sender is not None and operator_terminal(sender)):
        return RecipientPolicy(frozenset(), queued=role == TRIAGE and bool(orchestrators))
    return RecipientPolicy(orchestrators, queued=False)


def relaunched_admin_seat(db: HubDatabase, session_id: str, project_id: str) -> str | None:
    """The one live session now holding a stale Admin seat's role, if exactly one does."""
    rows = db.fetchall(
        """
SELECT s.id FROM sessions s JOIN session_variables v ON v.session_id = s.id
WHERE s.project_id = %s AND s.status = ANY(%s)
AND v.variables ->> '_agent_type' = ANY(%s)
AND v.variables ->> '_agent_type' = (
    SELECT variables ->> '_agent_type' FROM session_variables WHERE session_id = %s
)
LIMIT 2
""",
        (project_id, list(LIVE_SESSION_STATUSES), list(ADMIN_ROLES), session_id),
    )
    return str(rows[0]["id"]) if len(rows) == 1 else None
