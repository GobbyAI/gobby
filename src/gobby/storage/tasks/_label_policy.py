"""Authorize protected label removals under the mutation's task row lock."""

import json
from collections.abc import Iterable

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import get_current_session_id
from gobby.workflows.agent_resolver import is_operator_session, session_agent_definition
from gobby.workflows.state_manager import SessionVariableManager


def authorize_label_removal(
    db: HubDatabase,
    conn: Transaction,
    task_id: str,
    *,
    labels: Iterable[str] | None = None,
    remove_labels: Iterable[str] = (),
) -> None:
    row = conn.execute(
        "SELECT labels, project_id FROM tasks WHERE id = %s FOR UPDATE", (task_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Task {task_id} not found")
    previous = row["labels"] or []
    if isinstance(previous, str):
        previous = json.loads(previous)
    if "security-review" not in previous:
        return
    if "security-review" not in remove_labels and (labels is None or "security-review" in labels):
        return
    actor_id = get_current_session_id()
    actor = SessionManager(db).get(actor_id) if actor_id else None
    if actor is not None and actor.project_id == str(row["project_id"]):
        resolved = session_agent_definition(db, actor)
        definition = resolved[0] if resolved else None
        role = (
            SessionVariableManager(db)
            .get_variable_subset(actor.id, ("_agent_type",))
            .get("_agent_type")
        )
        if is_operator_session(actor, role) or (
            definition and "remove_security_review_label" in definition.capabilities
        ):
            return
    raise ValueError("Removing security-review requires the operator or authorized policy owner.")
