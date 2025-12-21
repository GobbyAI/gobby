import json
import logging
from datetime import UTC, datetime

from gobby.storage.database import LocalDatabase

from .definitions import WorkflowState

logger = logging.getLogger(__name__)


class WorkflowStateManager:
    """
    Manages persistence of WorkflowState and Handoffs.
    """

    def __init__(self, db: LocalDatabase):
        self.db = db

    def get_state(self, session_id: str) -> WorkflowState | None:
        row = self.db.fetchone("SELECT * FROM workflow_states WHERE session_id = ?", (session_id,))
        if not row:
            return None

        try:
            return WorkflowState(
                session_id=row["session_id"],
                workflow_name=row["workflow_name"],
                phase=row["phase"],
                phase_entered_at=datetime.fromisoformat(row["phase_entered_at"])
                if row["phase_entered_at"]
                else datetime.now(UTC),
                phase_action_count=row["phase_action_count"],
                total_action_count=row["total_action_count"],
                artifacts=json.loads(row["artifacts"]) if row["artifacts"] else {},
                observations=json.loads(row["observations"]) if row["observations"] else [],
                reflection_pending=bool(row["reflection_pending"]),
                context_injected=bool(row["context_injected"]),
                variables=json.loads(row["variables"]) if row["variables"] else {},
                task_list=json.loads(row["task_list"]) if row["task_list"] else None,
                current_task_index=row["current_task_index"],
                files_modified_this_task=row["files_modified_this_task"],
                updated_at=datetime.fromisoformat(row["updated_at"])
                if row["updated_at"]
                else datetime.now(UTC),
            )
        except Exception as e:
            logger.error(
                f"Failed to parse workflow state for session {session_id}: {e}", exc_info=True
            )
            return None

    def save_state(self, state: WorkflowState) -> None:
        """Upsert workflow state."""
        self.db.execute(
            """
            INSERT INTO workflow_states (
                session_id, workflow_name, phase, phase_entered_at,
                phase_action_count, total_action_count, artifacts,
                observations, reflection_pending, context_injected, variables,
                task_list, current_task_index, files_modified_this_task,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                phase = excluded.phase,
                phase_entered_at = excluded.phase_entered_at,
                phase_action_count = excluded.phase_action_count,
                total_action_count = excluded.total_action_count,
                artifacts = excluded.artifacts,
                observations = excluded.observations,
                reflection_pending = excluded.reflection_pending,
                context_injected = excluded.context_injected,
                variables = excluded.variables,
                task_list = excluded.task_list,
                current_task_index = excluded.current_task_index,
                files_modified_this_task = excluded.files_modified_this_task,
                updated_at = excluded.updated_at
            """,
            (
                state.session_id,
                state.workflow_name,
                state.phase,
                state.phase_entered_at.isoformat(),
                state.phase_action_count,
                state.total_action_count,
                json.dumps(state.artifacts),
                json.dumps(state.observations),
                1 if state.reflection_pending else 0,
                1 if state.context_injected else 0,
                json.dumps(state.variables),
                json.dumps(state.task_list) if state.task_list else None,
                state.current_task_index,
                state.files_modified_this_task,
                datetime.now(UTC).isoformat(),
            ),
        )

    def delete_state(self, session_id: str) -> None:
        self.db.execute("DELETE FROM workflow_states WHERE session_id = ?", (session_id,))
