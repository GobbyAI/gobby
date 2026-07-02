"""Active-session claim resolution for task CLI rendering."""

import json
import logging

from gobby.storage.hub.runtime import open_runtime_hub_database

logger = logging.getLogger(__name__)


def get_claimed_task_owners() -> dict[str, str]:
    """Map active-session claimed task IDs to their owner session IDs.

    Queries workflow_states for active sessions that have a session_task variable set,
    indicating the task is being actively worked on by that session.

    Supports session_task in multiple formats:
      - #N: Resolved to UUID via seq_num lookup
      - UUID: Used directly
      - Partial UUID prefix: Used for prefix matching

    Returns:
        Mapping of task UUID to active owner session UUID.
    """
    try:
        db = open_runtime_hub_database(apply_migrations=False)
        try:
            # Join workflow_states with sessions to find active sessions with session_task
            rows = db.fetchall(
                """
                SELECT ws.variables, ws.session_id, s.project_id
                FROM workflow_states ws
                JOIN sessions s ON ws.session_id = s.id
                WHERE s.status = 'active'
                AND ws.variables IS NOT NULL
                AND ws.variables != '{}'
                """
            )

            claimed_owners: dict[str, str] = {}

            def resolve_task_ref(ref: str, project_id: str | None) -> str | None:
                """Resolve a task reference to UUID."""
                if not ref or ref == "*":
                    return None

                # #N format - resolve via seq_num
                if ref.startswith("#"):
                    try:
                        seq_num = int(ref[1:])
                        row = db.fetchone(
                            "SELECT id FROM tasks WHERE project_id = %s AND seq_num = %s",
                            (project_id, seq_num),
                        )
                        return row["id"] if row else None
                    except (ValueError, TypeError):
                        return None

                # Check if it looks like a UUID (36 chars with dashes)
                if len(ref) == 36 and ref.count("-") == 4:
                    return ref

                # Partial UUID prefix - find matching task
                row = db.fetchone(
                    "SELECT id FROM tasks WHERE id::text LIKE %s AND project_id = %s",
                    (f"{ref}%", project_id),
                )
                return row["id"] if row else None

            for row in rows:
                try:
                    variables = json.loads(row["variables"]) if row["variables"] else {}
                    session_id = row["session_id"]
                    project_id = row["project_id"]
                    if session_task := variables.get("session_task"):
                        # session_task can be: string, list of strings, or "*" (wildcard)
                        if isinstance(session_task, list):
                            for task_ref in session_task:
                                if resolved := resolve_task_ref(task_ref, project_id):
                                    claimed_owners[resolved] = session_id
                        elif session_task != "*":
                            if resolved := resolve_task_ref(session_task, project_id):
                                claimed_owners[resolved] = session_id
                except (json.JSONDecodeError, TypeError):
                    continue

            return claimed_owners
        finally:
            db.close()
    except (RuntimeError, json.JSONDecodeError, KeyError) as e:
        logger.debug("Failed to get claimed task owners: %s", e, exc_info=True)
        return {}


def get_claimed_task_ids() -> set[str]:
    """Get task IDs that are claimed by active sessions via session_task variable."""
    return set(get_claimed_task_owners())
