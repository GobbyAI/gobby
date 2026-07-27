import json
import logging
import time
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Literal

from gobby.storage.hub.protocol import (
    HubDatabase,
    SessionVariableMutation,
    WorkflowInstanceMutation,
)
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


def _sanitize_variables_payload(value: Any) -> Any:
    """Replace PostgreSQL-incompatible NUL characters in JSON-compatible values."""
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd")
    if isinstance(value, dict):
        return {
            _sanitize_variables_payload(key) if isinstance(key, str) else key: (
                _sanitize_variables_payload(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_variables_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_variables_payload(item) for item in value)
    return value


def _encode_variables_payload(variables: Any) -> str:
    return json.dumps(_sanitize_variables_payload(variables))


def _normalize_string_list(value: Any) -> list[str]:
    """Return the string entries from a stored list variable."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


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
        """Get all enabled workflow instances for a session, sorted deterministically."""
        if not is_session_uuid(session_id):
            return []
        rows = self.db.fetchall(
            "SELECT * FROM workflow_instances WHERE session_id = %s AND enabled = %s "
            "ORDER BY priority ASC, workflow_name ASC",
            (session_id, True),
        )
        return [self._row_to_instance(row) for row in rows]

    def save_instance(self, instance: WorkflowInstance) -> None:
        """Create or update a workflow instance (upsert on session_id + workflow_name)."""
        if not is_session_uuid(instance.session_id):
            return
        now = datetime.now(UTC).isoformat()
        persisted = self.db.fetchone(
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
            RETURNING id, created_at, updated_at
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
                _encode_variables_payload(instance.variables),
                instance.context_injected,
                instance.created_at.isoformat(),
                now,
            ),
        )
        if persisted is None:  # pragma: no cover - PostgreSQL RETURNING always yields a row.
            raise RuntimeError("Workflow instance upsert returned no row")
        instance.id = persisted["id"]
        instance.created_at = require_stored_datetime(persisted["created_at"], "created_at")
        instance.updated_at = require_stored_datetime(persisted["updated_at"], "updated_at")

    def merge_instance_variables(
        self,
        session_id: str,
        workflow_name: str,
        updates: dict[str, Any],
    ) -> bool:
        """Atomically merge variables without rewriting workflow execution state."""
        if not updates or not is_session_uuid(session_id):
            return False
        lock = WorkflowInstanceMutation(session_id=session_id, workflow_name=workflow_name)
        with self.db.transaction_immediate(lock) as conn:
            now = datetime.now(UTC).isoformat()
            cursor = conn.execute(
                """
                UPDATE workflow_instances
                SET variables = COALESCE(variables, '{}'::jsonb) || %s::jsonb,
                    updated_at = %s
                WHERE session_id = %s AND workflow_name = %s
                """,
                (_encode_variables_payload(updates), now, session_id, workflow_name),
            )
            return cursor.rowcount > 0

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
        row = self.db.fetchone(
            "SELECT variables FROM session_variables WHERE session_id = %s",
            (session_id,),
        )
        session_vars = {}
        if row:
            session_vars = _decode_variables_payload(row["variables"])

        return self._apply_variable_defaults(session_vars)

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
            return deepcopy(self._defaults_cache)

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
            except (json.JSONDecodeError, TypeError, AttributeError, KeyError):
                continue

        self._defaults_cache = defaults
        self._defaults_cache_time = now
        return deepcopy(defaults)

    def _apply_variable_defaults(self, variables: dict[str, Any]) -> dict[str, Any]:
        """Layer stored variables over installed definition defaults."""
        defaults = self._get_variable_defaults()
        if not defaults:
            return variables
        return {**defaults, **variables}

    def set_variable(self, session_id: str, name: str, value: Any) -> None:
        """Set a single session variable (atomic read-modify-write)."""
        self.merge_variables(session_id, {name: value})

    def merge_variables(self, session_id: str, updates: dict[str, Any]) -> bool:
        """Atomically merge variable updates into session variables.

        A PostgreSQL transaction-scoped advisory lock serializes the read-modify-write,
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
                    (_encode_variables_payload(current), now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (session_id, _encode_variables_payload(updates), now),
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
                    (_encode_variables_payload(current_vars), now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (session_id, _encode_variables_payload(current_vars), now),
                )
        return count

    def append_to_bounded_list_variable(
        self,
        session_id: str,
        name: str,
        item: Any,
        *,
        max_items: int,
        updates: dict[str, Any] | None = None,
    ) -> int:
        """Atomically append an item to a bounded list and merge related updates."""
        if max_items < 1:
            raise ValueError("max_items must be positive")
        now = datetime.now(UTC).isoformat()
        with self.db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            current_vars = _decode_variables_payload(row["variables"]) if row else {}
            stored = current_vars.get(name, [])
            items = stored if isinstance(stored, list) else []
            bounded_items = [*items, item][-max_items:]
            current_vars[name] = bounded_items
            if updates:
                current_vars.update(updates)

            if row:
                conn.execute(
                    "UPDATE session_variables SET variables = %s, updated_at = %s "
                    "WHERE session_id = %s",
                    (_encode_variables_payload(current_vars), now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (session_id, _encode_variables_payload(current_vars), now),
                )
        return len(bounded_items)

    def upsert_bounded_list_variable(
        self,
        session_id: str,
        name: str,
        item: Any,
        *,
        identity: Mapping[str, Any],
        max_items: int,
        updates: dict[str, Any] | None = None,
    ) -> int:
        """Atomically replace one identified list item and merge related updates."""
        if not identity:
            raise ValueError("identity must not be empty")
        if max_items < 1:
            raise ValueError("max_items must be positive")

        now = datetime.now(UTC).isoformat()
        with self.db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            current_vars = _decode_variables_payload(row["variables"]) if row else {}
            stored = current_vars.get(name, [])
            items = stored if isinstance(stored, list) else []
            retained = [
                existing
                for existing in items
                if not (
                    isinstance(existing, Mapping)
                    and all(existing.get(key) == value for key, value in identity.items())
                )
            ]
            bounded_items = [*retained, item][-max_items:]
            current_vars[name] = bounded_items
            if updates:
                current_vars.update(updates)

            if row:
                conn.execute(
                    "UPDATE session_variables SET variables = %s, updated_at = %s "
                    "WHERE session_id = %s",
                    (_encode_variables_payload(current_vars), now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (session_id, _encode_variables_payload(current_vars), now),
                )
        return len(bounded_items)

    def upsert_open_tool_error(
        self,
        session_id: str,
        tool: str,
        target_key: str,
        error: str,
        *,
        occurred_at: datetime,
    ) -> None:
        """Atomically insert or increment one canonical unresolved tool error."""
        from gobby.hooks.tool_error_tracker import (
            MAX_TOOL_ERROR_COUNT,
            normalize_open_tool_error_records,
        )

        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        timestamp = occurred_at.astimezone(UTC).isoformat(timespec="seconds")
        incoming = normalize_open_tool_error_records(
            [
                {
                    "tool": tool,
                    "target_key": target_key,
                    "error": error,
                    "first_at": timestamp,
                    "last_at": timestamp,
                    "count": 1,
                }
            ]
        )[0]
        now = datetime.now(UTC).isoformat()
        with self.db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            current_vars = _decode_variables_payload(row["variables"]) if row else {}
            records = normalize_open_tool_error_records(current_vars.get("open_tool_errors", []))
            match = next(
                (
                    record
                    for record in records
                    if record["tool"] == incoming["tool"]
                    and record["target_key"] == incoming["target_key"]
                ),
                None,
            )
            if match is None:
                records.append(incoming)
            else:
                match["error"] = incoming["error"]
                match["last_at"] = max(match["last_at"], incoming["last_at"])
                match["count"] = min(MAX_TOOL_ERROR_COUNT, match["count"] + 1)
            current_vars["open_tool_errors"] = normalize_open_tool_error_records(records)

            if row:
                conn.execute(
                    "UPDATE session_variables SET variables = %s, updated_at = %s "
                    "WHERE session_id = %s",
                    (_encode_variables_payload(current_vars), now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (session_id, _encode_variables_payload(current_vars), now),
                )

    def resolve_open_tool_errors(
        self,
        session_id: str,
        tool: str,
        target_key: str,
    ) -> None:
        """Atomically remove the exact canonical tool-and-target error."""
        from gobby.hooks.tool_error_tracker import (
            normalize_open_tool_error_records,
            render_bounded_identity,
            sanitize_record_text,
        )

        canonical_tool = render_bounded_identity(sanitize_record_text(tool))
        canonical_target = render_bounded_identity(sanitize_record_text(target_key))
        now = datetime.now(UTC).isoformat()
        with self.db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            if not row:
                return
            current_vars = _decode_variables_payload(row["variables"])
            records = normalize_open_tool_error_records(current_vars.get("open_tool_errors", []))
            current_vars["open_tool_errors"] = [
                record
                for record in records
                if (record["tool"], record["target_key"]) != (canonical_tool, canonical_target)
            ]
            conn.execute(
                "UPDATE session_variables SET variables = %s, updated_at = %s "
                "WHERE session_id = %s",
                (_encode_variables_payload(current_vars), now, session_id),
            )

    def append_to_set_variable(self, session_id: str, name: str, values: list[str]) -> bool:
        """Atomically append strings to a string-list variable (deduped, sorted).

        A PostgreSQL transaction-scoped advisory lock serializes the read-modify-write,
        preventing concurrent events from clobbering each other. Stored scalars and
        non-string list entries are discarded to preserve the string-list contract.

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
            stored_vars = _decode_variables_payload(row["variables"]) if row else {}
            current_vars = self._apply_variable_defaults(stored_vars)
            existing = set(_normalize_string_list(current_vars.get(name)))
            existing.update(values)
            current_vars[name] = sorted(existing)
            if row:
                conn.execute(
                    "UPDATE session_variables SET variables = %s, updated_at = %s "
                    "WHERE session_id = %s",
                    (_encode_variables_payload(current_vars), now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (session_id, _encode_variables_payload(current_vars), now),
                )
        return True

    def claim_set_variable_values(
        self,
        session_id: str,
        name: str,
        values: list[str],
    ) -> list[str]:
        """Atomically store and return values that were not already present.

        The returned values preserve input order and contain no duplicates. The
        transaction serializes the read and write so concurrent callers cannot
        both claim the same value.
        """
        if not values:
            return []

        now = datetime.now(UTC).isoformat()
        with self.db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            current_vars = _decode_variables_payload(row["variables"]) if row else {}
            existing = set(_normalize_string_list(current_vars.get(name)))
            claimed: list[str] = []
            for value in values:
                if value not in existing:
                    existing.add(value)
                    claimed.append(value)

            if not claimed:
                return []

            current_vars[name] = sorted(existing)
            if row:
                conn.execute(
                    "UPDATE session_variables SET variables = %s, updated_at = %s "
                    "WHERE session_id = %s",
                    (_encode_variables_payload(current_vars), now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (session_id, _encode_variables_payload(current_vars), now),
                )
        return claimed

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
            stored_vars = _decode_variables_payload(row["variables"]) if row else {}
            current_vars = self._apply_variable_defaults(stored_vars)

            if values:
                existing = set(_normalize_string_list(current_vars.get(name)))
                existing.update(values)
                current_vars[name] = sorted(existing)

            if current_vars.get(condition_name) is True:
                current_vars.update(updates)

            if row:
                conn.execute(
                    "UPDATE session_variables SET variables = %s, updated_at = %s "
                    "WHERE session_id = %s",
                    (_encode_variables_payload(current_vars), now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (session_id, _encode_variables_payload(current_vars), now),
                )
        return True

    def record_edited_file(
        self,
        session_id: str,
        repo_relative_path: str,
    ) -> bool:
        """Record a successful repo file edit in session and active-task ledgers."""
        if not repo_relative_path:
            return True

        from gobby.workflows.task_claim_state import active_task_id_for_edit

        now = datetime.now(UTC).isoformat()
        with self.db.transaction_immediate(SessionVariableMutation(session_id=session_id)) as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            stored_vars = _decode_variables_payload(row["variables"]) if row else {}
            current_vars = self._apply_variable_defaults(stored_vars)

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

            if row:
                conn.execute(
                    "UPDATE session_variables SET variables = %s, updated_at = %s "
                    "WHERE session_id = %s",
                    (_encode_variables_payload(current_vars), now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (session_id, _encode_variables_payload(current_vars), now),
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
            stored_vars = _decode_variables_payload(row["variables"]) if row else {}
            current_vars = self._apply_variable_defaults(stored_vars)

            if current_vars.get("_startup_context_injected") is True:
                return "live"

            current_vars["_startup_context_injected"] = True
            if row:
                conn.execute(
                    "UPDATE session_variables SET variables = %s, updated_at = %s "
                    "WHERE session_id = %s",
                    (_encode_variables_payload(current_vars), now, session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_variables (session_id, variables, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (session_id, _encode_variables_payload(current_vars), now),
                )
        return "full"
