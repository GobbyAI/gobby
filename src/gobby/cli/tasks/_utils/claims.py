"""Active-session claim resolution for task CLI rendering."""

import json
import logging
import sqlite3

from gobby.storage.database import LocalDatabase

logger = logging.getLogger(__name__)


def get_claimed_task_ids() -> set[str]:
    """Get task IDs that are claimed by active sessions via session_task variable.

    Queries workflow_states for active sessions that have a session_task variable set,
    indicating the task is being actively worked on by that session.

    Supports session_task in multiple formats:
      - #N: Resolved to UUID via seq_num lookup
      - UUID: Used directly
      - Partial UUID prefix: Used for prefix matching

    Returns:
        Set of task UUIDs claimed by active sessions
    """
    try:
        db = LocalDatabase()
        try:
            # Join workflow_states with sessions to find active sessions with session_task
            rows = db.fetchall(
                """
                SELECT ws.variables, s.project_id
                FROM workflow_states ws
                JOIN sessions s ON ws.session_id = s.id
                WHERE s.status = 'active'
                AND ws.variables IS NOT NULL
                AND ws.variables != '{}'
                """
            )

            claimed_ids: set[str] = set()

            def resolve_task_ref(ref: str, project_id: str | None) -> str | None:
                """Resolve a task reference to UUID."""
                if not ref or ref == "*":
                    return None

                # #N format - resolve via seq_num
                if ref.startswith("#"):
                    try:
                        seq_num = int(ref[1:])
                        row = db.fetchone(
                            "SELECT id FROM tasks WHERE project_id = ? AND seq_num = ?",
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
                    "SELECT id FROM tasks WHERE id LIKE ? AND project_id = ?",
                    (f"{ref}%", project_id),
                )
                return row["id"] if row else None

            for row in rows:
                try:
                    variables = json.loads(row["variables"]) if row["variables"] else {}
                    project_id = row["project_id"]
                    if session_task := variables.get("session_task"):
                        # session_task can be: string, list of strings, or "*" (wildcard)
                        if isinstance(session_task, list):
                            for task_ref in session_task:
                                if resolved := resolve_task_ref(task_ref, project_id):
                                    claimed_ids.add(resolved)
                        elif session_task != "*":
                            if resolved := resolve_task_ref(session_task, project_id):
                                claimed_ids.add(resolved)
                except (json.JSONDecodeError, TypeError):
                    continue

            return claimed_ids
        finally:
            db.close()
    except (sqlite3.Error, json.JSONDecodeError, KeyError) as e:
        logger.debug(f"Failed to get claimed task IDs: {e}")
        return set()
