import logging
from typing import Any, Literal

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import Task

logger = logging.getLogger(__name__)

SessionTaskAction = Literal[
    "worked_on",
    "discovered",
    "mentioned",
    "closed",
    "created",
    "claimed",
    "escalated",
    "needs_review",
    "review_approved",
]


class SessionTaskManager:
    VALID_ACTIONS = {
        "worked_on",
        "discovered",
        "mentioned",
        "closed",
        "created",
        "claimed",
        "escalated",
        "needs_review",
        "review_approved",
    }

    def __init__(self, db: HubDatabase):
        self.db = db

    def link_task(
        self,
        session_id: str,
        task_id: str,
        action: str = "worked_on",
    ) -> None:
        """
        Link a task to a session with a specific action.
        Actions: worked_on, discovered, mentioned, closed, created, claimed,
                 escalated, needs_review, review_approved
        """
        if action not in self.VALID_ACTIONS:
            raise ValueError(f"Invalid action '{action}'. Must be one of {self.VALID_ACTIONS}")

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO session_tasks (
                    session_id, task_id, action
                ) VALUES (%s, %s, %s)
                ON CONFLICT (session_id, task_id, action) DO NOTHING
                """,
                (session_id, task_id, action),
            )
            logger.debug("Linked task %s to session %s with action %s", task_id, session_id, action)

    def unlink_task(
        self,
        session_id: str,
        task_id: str,
        action: str,
    ) -> None:
        """Remove a link between a task and a session."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                DELETE FROM session_tasks
                WHERE session_id = %s AND task_id = %s AND action = %s
                """,
                (session_id, task_id, action),
            )
            logger.debug(
                "Unlinked task %s from session %s for action %s", task_id, session_id, action
            )

    def get_session_tasks(self, session_id: str) -> list[dict[str, Any]]:
        """
        Get all tasks associated with a session.
        Returns a list of dicts with task details and the action.
        """
        query = """
        SELECT t.*, st.action as session_action, st.created_at as link_created_at
        FROM tasks t
        JOIN session_tasks st ON t.id = st.task_id
        WHERE st.session_id = %s
        ORDER BY st.created_at DESC
        """
        rows = self.db.fetchall(query, (session_id,))

        results = []
        for row in rows:
            task = Task.from_row(row)
            results.append(
                {
                    "task": task,
                    "action": row["session_action"],
                    "link_created_at": row["link_created_at"],
                }
            )
        return results

    def get_task_sessions(self, task_id: str) -> list[dict[str, Any]]:
        """
        Get all sessions associated with a task.
        """
        # Simple query that relies only on session_tasks to minimize dependencies
        rows = self.db.fetchall(
            "SELECT * FROM session_tasks WHERE task_id = %s ORDER BY created_at DESC", (task_id,)
        )
        return [dict(row) for row in rows]
