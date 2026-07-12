import json
import logging
import time
from datetime import UTC, datetime
from typing import Any, Literal

from gobby.storage.hub.protocol import HubDatabase, SessionVariableMutation
from gobby.storage.session_resolution import is_session_uuid
from gobby.utils.datetime import parse_stored_datetime, require_stored_datetime

from .definitions import WorkflowInstance

logger = logging.getLogger(__name__)


def _decode_variables_payload(variables: Any) -> dict[str, Any]:
    if isinstance(variables, dict):
        return variables
    if isinstance(variables, str | bytes | bytearray) and variables:
        try:
            loaded = json.loads(variables)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Failed to decode workflow variables payload: %s", exc)
            return {}
        if isinstance(loaded, dict):
            return loaded
        logger.warning("Ignoring non-object workflow variables payload: %s", type(loaded).__name__)
    return {}


class WorkflowInstanceManager:
    """Manages CRUD operations for workflow instances (multi-workflow per session)."""

    def __init__(self, db: HubDatabase):
        self.db = db

    def get_instance(self, session_id: str, workflow_name: str) -> WorkflowInstance | None:
        """Get a specific workflow instance by session and workflow name."""
        if not is_session_uuid(session_id):
            return None
        row = self.db.fetchone(
            "SELECT * FROM workflow_instances WHERE session_id = %s AND workflow_name = %s",
            (session_id, workflow_name),
        )
        if not row:
            return None
        return self._row_to_instance(row)

    def get_active_instances(self, session_id: str) -> list[WorkflowInstance]:
        """Get all enabled workflow instances for a session, sorted by priority."""
        if not is_session_uuid(session_id):
            return []
        rows = self.db.fetchall(
            "SELECT * FROM workflow_instances WHERE session_id = %s AND enabled = %s "
            "ORDER BY priority ASC",
            (session_id, True),
        )
        return [self._row_to_instance(row) for row in rows]

    def save_instance(self, instance: WorkflowInstance) -> None:
        """Create or update a workflow instance (upsert on session_id + workflow_name)."""
        if not is_session_uuid(instance.session_id):
            return
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            INSERT INTO workflow_instances (
                id, session_id, workflow_name, enabled, priority,
                current_step, step_entered_at, step_action_count, total_action_count,
                variables, context_injected, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                instance.enabled,
                instance.priority,
                instance.current_step,
                instance.step_entered_at.isoformat() if instance.step_entered_at else None,
                instance.step_action_count,
                instance.total_action_count,
                json.dumps(instance.variables),
                instance.context_injected,
                now,
                now,
            ),
        )

    def delete_instance(self, session_id: str, workflow_name: str) -> None:
        """Delete a workflow instance."""
        if not is_session_uuid(session_id):
            return
        self.db.execute(
            "DELETE FROM workflow_instances WHERE session_id = %s AND workflow_name = %s",
            (session_id, workflow_name),
        )

    def delete_instances_for_session(self, session_id: str) -> int:
        """Delete all workflow instances for a session and return deleted row count."""
        if not is_session_uuid(session_id):
            return 0
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM workflow_instances WHERE session_id = %s",
                (session_id,),
            )
            return cursor.rowcount

    def set_enabled(self, session_id: str, workflow_name: str, enabled: bool) -> None:
        """Toggle the enabled state of a workflow instance."""
        if not is_session_uuid(session_id):
            return
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            "UPDATE workflow_instances SET enabled = %s, updated_at = %s "
            "WHERE session_id = %s AND workflow_name = %s",
            (enabled, now, session_id, workflow_name),
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
            step_entered_at=parse_stored_datetime(row["step_entered_at"]),
            step_action_count=row["step_action_count"],
            total_action_count=row["total_action_count"],
            variables=_decode_variables_payload(row["variables"]),
            context_injected=bool(row["context_injected"]),
            created_at=require_stored_datetime(row["created_at"], "created_at"),
            updated_at=require_stored_datetime(row["updated_at"], "updated_at"),
        )


class SessionVariableManager:
    """Manages session-scoped shared variables (visible to all workflows).

    Variable resolution layers definition defaults under session overrides,
    ensuring presets are always available even if never explicitly materialized
    into the session row (e.g., ``gobby init`` run mid-session).
    """

    _DEFAULTS_CACHE_TTL = 10.0  # seconds

    def __init__(self, db: HubDatabase):
        self.db = db
        self._defaults_cache: dict[str, Any] | None = None
        self._defaults_cache_time: float = 0.0

    def get_variables(self, session_id: str) -> dict[str, Any]:
        """Get all session variables with definition defaults applied.

        Layers: variable definition defaults < session-stored overrides.
        This ensures presets are always available even if they were never
        explicitly materialized into the session row.
        """
        defaults = self._get_variable_defaults()

        row = self.db.fetchone(
            "SELECT variables FROM session_variables WHERE session_id = %s",
            (session_id,),
        )
        session_vars = {}
        if row:
            session_vars = _decode_variables_payload(row["variables"])

        if not defaults:
            return session_vars
        return {**defaults, **session_vars}

    def _get_variable_defaults(self) -> dict[str, Any]:
        """Load default values from enabled, installed variable definitions.

        Results are cached for ``_DEFAULTS_CACHE_TTL`` seconds to avoid
        per-hook DB overhead.  The definition set only changes on
        ``gobby sync`` / ``gobby init`` which are rare operations.
        """
        now = time.monotonic()
        if (
            self._defaults_cache is not None
            and (now - self._defaults_cache_time) < self._DEFAULTS_CACHE_TTL
        ):
            return dict(self._defaults_cache)

        rows = self.db.fetchall(
            "SELECT name, definition_json FROM workflow_definitions "
            "WHERE workflow_type = 'variable' AND enabled = %s AND source = 'installed'",
            (True,),
        )
        defaults: dict[str, Any] = {}
        for row in rows:
            try:
                body = json.loads(row["definition_json"])
                defaults[body.get("variable", row["name"])] = body.get("value")
            except (json.JSONDecodeError, KeyError):
                continue

        self._defaults_cache = defaults
        self._defaults_cache_time = now
        return defaults

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
        with self.db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            if row:
                current = _decode_variables_payload(row["variables"])
                current.update(updates)
                conn.execute(
                    "UPDATE session_variables SET variables = %s, updated_at = %s "
                    "WHERE session_id = %s",
                    (json.dumps(current), now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (session_id, json.dumps(updates), now),
                )
        return True

    def adjust_counter_and_derive_boolean(
        self,
        session_id: str,
        counter_name: str,
        delta: int,
        *,
        boolean_name: str,
    ) -> int:
        """Atomically adjust a non-negative counter and derive its boolean flag."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            current_vars = _decode_variables_payload(row["variables"]) if row else {}
            raw_count = current_vars.get(counter_name, 0)
            stored_count: int
            if isinstance(raw_count, int) and not isinstance(raw_count, bool):
                stored_count = raw_count
            else:
                stored_count = 0
            count = max(0, stored_count + delta)
            current_vars[counter_name] = count
            current_vars[boolean_name] = count > 0

            if row:
                conn.execute(
                    "UPDATE session_variables SET variables = %s, updated_at = %s "
                    "WHERE session_id = %s",
                    (json.dumps(current_vars), now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (session_id, json.dumps(current_vars), now),
                )
        return count

    def append_to_set_variable(self, session_id: str, name: str, values: list[str]) -> bool:
        """Atomically append values to a list variable (deduped, sorted).

        Uses BEGIN IMMEDIATE to serialize the read-modify-write, preventing
        concurrent AFTER_TOOL events from clobbering each other.

        Args:
            session_id: Session ID to scope the variable to.
            name: Variable name (the list to append to).
            values: New values to add (duplicates are ignored).

        Returns:
            True always (creates row if needed).
        """
        if not values:
            return True
        now = datetime.now(UTC).isoformat()
        with self.db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            current_vars = _decode_variables_payload(row["variables"]) if row else {}
            stored = current_vars.get(name, [])
            if not isinstance(stored, list):
                stored = [stored] if stored else []
            existing = set(stored)
            existing.update(values)
            current_vars[name] = sorted(existing)
            if row:
                conn.execute(
                    "UPDATE session_variables SET variables = %s, updated_at = %s "
                    "WHERE session_id = %s",
                    (json.dumps(current_vars), now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (session_id, json.dumps(current_vars), now),
                )
        return True

    def append_to_set_variable_and_conditional_merge(
        self,
        session_id: str,
        name: str,
        values: list[str],
        *,
        condition_name: str,
        updates: dict[str, Any],
    ) -> bool:
        """Append set values and conditionally merge updates in one transaction.

        The condition is evaluated against the same row snapshot that receives
        the append, so edit tracking and evidence reset cannot interleave.
        """
        if not values and not updates:
            return True

        now = datetime.now(UTC).isoformat()
        with self.db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            current_vars = _decode_variables_payload(row["variables"]) if row else {}

            if values:
                stored = current_vars.get(name, [])
                if not isinstance(stored, list):
                    stored = [stored] if stored else []
                existing = set(stored)
                existing.update(values)
                current_vars[name] = sorted(existing)

            if current_vars.get(condition_name) is True:
                current_vars.update(updates)

            if row:
                conn.execute(
                    "UPDATE session_variables SET variables = %s, updated_at = %s "
                    "WHERE session_id = %s",
                    (json.dumps(current_vars), now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (session_id, json.dumps(current_vars), now),
                )
        return True

    def record_edited_file(
        self,
        session_id: str,
        repo_relative_path: str,
        *,
        condition_name: str,
        updates: dict[str, Any],
    ) -> bool:
        """Record a successful repo file edit in session and active-task ledgers."""
        if not repo_relative_path and not updates:
            return True

        from gobby.workflows.task_claim_state import active_task_id_for_edit

        now = datetime.now(UTC).isoformat()
        with self.db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            current_vars = _decode_variables_payload(row["variables"]) if row else {}

            stored = current_vars.get("session_edited_files", [])
            if not isinstance(stored, list):
                stored = [stored] if stored else []
            session_files = {str(file) for file in stored if file}
            if repo_relative_path:
                session_files.add(repo_relative_path)
            current_vars["session_edited_files"] = sorted(session_files)

            task_id = active_task_id_for_edit(current_vars)
            if task_id and repo_relative_path:
                raw_task_files = current_vars.get("task_edited_files") or {}
                task_files = raw_task_files if isinstance(raw_task_files, dict) else {}
                stored_for_task = task_files.get(task_id, [])
                if not isinstance(stored_for_task, list):
                    stored_for_task = [stored_for_task] if stored_for_task else []
                files_for_task = {str(file) for file in stored_for_task if file}
                files_for_task.add(repo_relative_path)
                task_files = dict(task_files)
                task_files[task_id] = sorted(files_for_task)
                current_vars["task_edited_files"] = task_files

            if current_vars.get(condition_name) is True:
                current_vars.update(updates)

            if row:
                conn.execute(
                    "UPDATE session_variables SET variables = %s, updated_at = %s "
                    "WHERE session_id = %s",
                    (json.dumps(current_vars), now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (session_id, json.dumps(current_vars), now),
                )
        return True

    def claim_startup_context(self, session_id: str) -> Literal["full", "live"]:
        """Atomically claim the startup context for this session.

        Returns:
            'full' if this call owns the startup context (first caller).
            'live' if another concurrent caller already claimed it.
        """
        now = datetime.now(UTC).isoformat()
        with self.db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            current_vars = _decode_variables_payload(row["variables"]) if row else {}

            if current_vars.get("_startup_context_injected") is True:
                return "live"

            current_vars["_startup_context_injected"] = True
            if row:
                conn.execute(
                    "UPDATE session_variables SET variables = %s, updated_at = %s "
                    "WHERE session_id = %s",
                    (json.dumps(current_vars), now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (session_id, json.dumps(current_vars), now),
                )
        return "full"

    def delete_variables(self, session_id: str) -> None:
        """Delete all session variables for a session."""
        self.db.execute(
            "DELETE FROM session_variables WHERE session_id = %s",
            (session_id,),
        )
