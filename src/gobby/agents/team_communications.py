"""Definition-controlled mailbox admission and stale-seat relaunch routing."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from gobby.storage.hub.protocol import HubDatabase, Row
from gobby.storage.sessions import LIVE_SESSION_STATUSES, system_session_id
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.agent_resolver import (
    AgentResolutionError,
    resolve_agent_with_row,
    session_has_capability,
)

RECIPIENT_REFUSED = "message_recipient_not_allowed"
REFUSAL = "The recipient definition does not admit this sender's capabilities."


@dataclass(frozen=True)
class RecipientPolicy:
    withheld: frozenset[str]  # recipients this sender may not address


_SEATS = (
    "SELECT s.id, s.project_id, s.parent_session_id, s.agent_run_id, s.agent_depth, "
    "v.variables ->> '_agent_type' AS role "
    "FROM sessions s LEFT JOIN session_variables v ON v.session_id = s.id "
)


def operator_terminal(row: Row) -> bool:
    """The operator's own terminal is no seat: no agent run, no depth and no seat role."""
    return (
        row["parent_session_id"] is None
        and not row["agent_run_id"]
        and not row["agent_depth"]
        and row["role"] in (None, "default")
    )


def _definition(db: HubDatabase, row: Row) -> AgentDefinitionBody | None:
    role = row["role"]
    if not isinstance(role, str) or not role:
        return None
    try:
        resolved = resolve_agent_with_row(role, db, project_id=row["project_id"])
    except AgentResolutionError:
        return None
    return resolved[0] if resolved is not None and resolved[1].enabled else None


def recipient_policy(
    db: HubDatabase, sender_id: str, recipient_ids: Sequence[str]
) -> RecipientPolicy:
    if sender_id == system_session_id():
        return RecipientPolicy(frozenset())
    rows = db.fetchall(_SEATS + "WHERE s.id = ANY(%s::uuid[])", ([sender_id, *recipient_ids],))
    sender = next((row for row in rows if str(row["id"]) == sender_id), None)
    if sender is not None and operator_terminal(sender):
        return RecipientPolicy(frozenset())
    if sender is None:
        return RecipientPolicy(frozenset(recipient_ids))
    definitions: dict[tuple[str, str], AgentDefinitionBody | None] = {}
    bodies: dict[str, AgentDefinitionBody | None] = {}
    for row in rows:
        key = (str(row["project_id"]), str(row["role"]))
        if key not in definitions:
            definitions[key] = _definition(db, row)
        bodies[str(row["id"])] = definitions[key]
    sender_body = bodies.get(sender_id)
    capabilities = set(sender_body.capabilities) if sender_body is not None else set()
    operator_ids = {str(row["id"]) for row in rows if operator_terminal(row)}
    withheld = set()
    for recipient_id in recipient_ids:
        if recipient_id in operator_ids:
            continue
        body = bodies.get(recipient_id)
        if body is None or (
            body.message_sender_capabilities
            and not capabilities.intersection(body.message_sender_capabilities)
        ):
            withheld.add(recipient_id)
    return RecipientPolicy(frozenset(withheld))


def relaunched_seat(db: HubDatabase, session_id: str, project_id: str) -> str | None:
    """Follow a declared standing identity only when exactly one live holder exists."""
    if not session_has_capability(db, session_id, "follow_relaunch"):
        return None
    rows = db.fetchall(
        """
SELECT s.id FROM sessions s JOIN session_variables v ON v.session_id = s.id
WHERE s.project_id = %s AND s.status = ANY(%s)
AND v.variables ->> '_agent_type' = (
    SELECT variables ->> '_agent_type' FROM session_variables WHERE session_id = %s
)
LIMIT 2
""",
        (project_id, list(LIVE_SESSION_STATUSES), session_id),
    )
    return str(rows[0]["id"]) if len(rows) == 1 else None
