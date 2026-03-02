import json
import logging
from datetime import UTC, datetime
from typing import Any

from gobby.storage.database import DatabaseProtocol

from .definitions import WorkflowInstance

logger = logging.getLogger(__name__)


class WorkflowInstanceManager:
    """Manages CRUD operations for workflow instances (multi-workflow per session)."""

    def __init__(self, db: DatabaseProtocol):
        self.db = db

    def get_instance(self, session_id: str, workflow_name: str) -> WorkflowInstance | None:
        """Get a specific workflow instance by session and workflow name."""
        row = self.db.fetchone(
            "SELECT * FROM workflow_instances WHERE session_id = ? AND workflow_name = ?",
            (session_id, workflow_name),
        )
        if not row:
            return None
        return self._row_to_instance(row)

    def get_active_instances(self, session_id: str) -> list[WorkflowInstance]:
        """Get all enabled workflow instances for a session, sorted by priority."""
        rows = self.db.fetchall(
            "SELECT * FROM workflow_instances WHERE session_id = ? AND enabled = 1 "
            "ORDER BY priority ASC",
            (session_id,),
        )
        return [self._row_to_instance(row) for row in rows]

    def save_instance(self, instance: WorkflowInstance) -> None:
        """Create or update a workflow instance (upsert on session_id + workflow_name)."""
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            INSERT INTO workflow_instances (
                id, session_id, workflow_name, enabled, priority,
                current_step, step_entered_at, step_action_count, total_action_count,
                variables, context_injected, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, workflow_name) DO UPDATE SET
                enabled = excluded.enabled,
                priority = excluded.priority,
                current_step = excluded.current_step,
                step_entered_at = excluded.step_entered_at,
                step_action_count = excluded.step_action_count,
                total_action_count = excluded.total_action_count,
                variables = excluded.variables,
                context_injected = excluded.context_injected,
                updated_at = excluded.updated_at
            """,
            (
                instance.id,
                instance.session_id,
                instance.workflow_name,
                1 if instance.enabled else 0,
                instance.priority,
                instance.current_step,
                instance.step_entered_at.isoformat() if instance.step_entered_at else None,
                instance.step_action_count,
                instance.total_action_count,
                json.dumps(instance.variables),
                1 if instance.context_injected else 0,
                now,
                now,
            ),
        )

    def delete_instance(self, session_id: str, workflow_name: str) -> None:
        """Delete a workflow instance."""
        self.db.execute(
            "DELETE FROM workflow_instances WHERE session_id = ? AND workflow_name = ?",
            (session_id, workflow_name),
        )

    def set_enabled(self, session_id: str, workflow_name: str, enabled: bool) -> None:
        """Toggle the enabled state of a workflow instance."""
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            "UPDATE workflow_instances SET enabled = ?, updated_at = ? "
            "WHERE session_id = ? AND workflow_name = ?",
            (1 if enabled else 0, now, session_id, workflow_name),
        )

    @staticmethod
    def _row_to_instance(row: Any) -> WorkflowInstance:
        """Convert a database row to a WorkflowInstance."""
        return WorkflowInstance(
            id=row["id"],
            session_id=row["session_id"],
            workflow_name=row["workflow_name"],
            enabled=bool(row["enabled"]),
            priority=row["priority"],
            current_step=row["current_step"],
            step_entered_at=(
                datetime.fromisoformat(row["step_entered_at"]) if row["step_entered_at"] else None
            ),
            step_action_count=row["step_action_count"],
            total_action_count=row["total_action_count"],
            variables=json.loads(row["variables"]) if row["variables"] else {},
            context_injected=bool(row["context_injected"]),
            created_at=(
                datetime.fromisoformat(row["created_at"])
                if row["created_at"]
                else datetime.now(UTC)
            ),
            updated_at=(
                datetime.fromisoformat(row["updated_at"])
                if row["updated_at"]
                else datetime.now(UTC)
            ),
        )


class SessionVariableManager:
    """Manages session-scoped shared variables (visible to all workflows)."""

    def __init__(self, db: DatabaseProtocol):
        self.db = db

    def get_variables(self, session_id: str) -> dict[str, Any]:
        """Get all session variables. Returns empty dict if no row exists."""
        row = self.db.fetchone(
            "SELECT variables FROM session_variables WHERE session_id = ?",
            (session_id,),
        )
        if not row:
            return {}
        return json.loads(row["variables"]) if row["variables"] else {}

    def set_variable(self, session_id: str, name: str, value: Any) -> None:
        """Set a single session variable (atomic read-modify-write)."""
        self.merge_variables(session_id, {name: value})

    def merge_variables(self, session_id: str, updates: dict[str, Any]) -> bool:
        """Atomically merge variable updates into session variables.

        Uses BEGIN IMMEDIATE to serialize the read-modify-write,
        preventing concurrent evaluations from clobbering each other.
        Creates the row if it doesn't exist.

        Returns:
            True always (creates row if needed).
        """
        if not updates:
            return True
        now = datetime.now(UTC).isoformat()
        with self.db.transaction_immediate() as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row:
                current = json.loads(row["variables"]) if row["variables"] else {}
                current.update(updates)
                conn.execute(
                    "UPDATE session_variables SET variables = ?, updated_at = ? "
                    "WHERE session_id = ?",
                    (json.dumps(current), now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (?, ?, ?)",
                    (session_id, json.dumps(updates), now),
                )
        return True

    def delete_variables(self, session_id: str) -> None:
        """Delete all session variables for a session."""
        self.db.execute(
            "DELETE FROM session_variables WHERE session_id = ?",
            (session_id,),
        )
