import logging
from datetime import UTC, datetime
from typing import Any, Literal

from gobby.storage.database import LocalDatabase
from gobby.storage.tasks import Task

logger = logging.getLogger(__name__)

SessionTaskAction = Literal["worked_on", "discovered", "mentioned", "closed"]


class SessionTaskManager:
    VALID_ACTIONS = {"worked_on", "discovered", "mentioned", "closed"}

    def __init__(self, db: LocalDatabase):
        self.db = db

    def link_task(
        self,
        session_id: str,
        task_id: str,
        action: str = "worked_on",
    ) -> None:
        """
        Link a task to a session with a specific action.
        Actions: worked_on, discovered, mentioned, closed
        """
        if action not in self.VALID_ACTIONS:
            raise ValueError(f"Invalid action '{action}'. Must be one of {self.VALID_ACTIONS}")

        now = datetime.now(UTC).isoformat()

        with self.db.transaction() as conn:
            # Use INSERT OR IGNORE to handle duplicate links gracefully
            conn.execute(
                """
                INSERT OR IGNORE INTO session_tasks (
                    session_id, task_id, action, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (session_id, task_id, action, now),
            )
            logger.debug(f"Linked task {task_id} to session {session_id} with action {action}")

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
                WHERE session_id = ? AND task_id = ? AND action = ?
                """,
                (session_id, task_id, action),
            )
            logger.debug(f"Unlinked task {task_id} from session {session_id} for action {action}")

    def get_session_tasks(self, session_id: str) -> list[dict[str, Any]]:
        """
        Get all tasks associated with a session.
        Returns a list of dicts with task details and the action.
        """
        query = """
        SELECT t.*, st.action as session_action, st.created_at as link_created_at
        FROM tasks t
        JOIN session_tasks st ON t.id = st.task_id
        WHERE st.session_id = ?
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
            "SELECT * FROM session_tasks WHERE task_id = ? ORDER BY created_at DESC", (task_id,)
        )
        return [dict(row) for row in rows]
