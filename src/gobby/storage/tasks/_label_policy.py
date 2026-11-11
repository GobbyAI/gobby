"""Authorize protected label removals under the mutation's task row lock."""

import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import get_current_session_id
from gobby.workflows.agent_resolver import is_operator_session, session_agent_definition
from gobby.workflows.state_manager import SessionVariableManager

_OPERATOR_LABEL_CHANGES: ContextVar[bool] = ContextVar("operator_label_changes", default=False)


@contextmanager
def operator_label_changes() -> Iterator[None]:
    """Enter only from an authenticated operator transport or operator-only CLI."""
    token = _OPERATOR_LABEL_CHANGES.set(True)
    try:
        yield
    finally:
        _OPERATOR_LABEL_CHANGES.reset(token)


def retain_security_review_label(
    previous: list[str] | str | None, proposed: list[str] | None
) -> list[str] | None:
    """Reconciliation and restore retain an existing review requirement."""
    current = json.loads(previous) if isinstance(previous, str) else previous
    if current and "security-review" in current and "security-review" not in (proposed or []):
        return [*(proposed or []), "security-review"]
    return proposed


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
    if _OPERATOR_LABEL_CHANGES.get():
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
            definition and definition.holds("remove_security_review_label")
        ):
            return
    raise ValueError("Removing security-review requires the operator or authorized policy owner.")
