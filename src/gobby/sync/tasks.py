import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from gobby.storage.tasks import LocalTaskManager
from gobby.sync.jsonl_io import atomic_write_text, export_file_lock
from gobby.tasks.state_semantics import serialize_task_state
from gobby.utils.json_helpers import json_dumps

TASK_BACKUP_PAGE_SIZE = 100_000

logger = logging.getLogger(__name__)


class TaskRestoreError(ValueError):
    """Raised when a task backup cannot be restored safely."""


class TaskBackupError(RuntimeError):
    """Raised when a task backup cannot be written safely."""


def _parse_timestamp(ts: str | datetime) -> datetime:
    """Parse ISO 8601 timestamp string to datetime.

    Handles both Z suffix and +HH:MM offset formats for compatibility
    with existing data that may use either format.

    Args:
        ts: ISO 8601 timestamp string (e.g., "2026-01-25T01:43:54Z" or
            "2026-01-25T01:43:54.123456+00:00")

    Returns:
        Timezone-aware datetime object in UTC
    """
    if isinstance(ts, datetime):
        dt = ts
    else:
        # Handle Z suffix for fromisoformat compatibility
        parse_ts = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(parse_ts)

    # Ensure timezone is UTC
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _normalize_timestamp(ts: str | datetime | None) -> str | None:
    """Normalize timestamp to consistent RFC 3339 format.

    Ensures all timestamps have:
    - Microsecond precision (.ffffff)
    - UTC timezone as +00:00 suffix

    Args:
        ts: ISO 8601 timestamp string

    Returns:
        Timestamp in format YYYY-MM-DDTHH:MM:SS.ffffff+00:00, or None if input was None
    """
    if ts is None:
        return None

    try:
        dt = _parse_timestamp(ts)
    except ValueError:
        # If parsing fails, return original (shouldn't happen with valid ISO 8601)
        return ts.isoformat() if isinstance(ts, datetime) else ts

    # Format with consistent microseconds and +00:00 suffix
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{dt.microsecond:06d}+00:00"


def _parse_optional_timestamp(ts: str | datetime | None) -> datetime | None:
    if ts is None:
        return None
    return _parse_timestamp(ts)


def _normalize_date(value: str | date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _known_session_id(value: str | None, existing_session_ids: set[str]) -> str | None:
    if value is None:
        return None
    if value in existing_session_ids:
        return value
    logger.warning("Dropping task restore session reference for unknown session %s", value)
    return None


def _legacy_github_issue_uuid_seed(owner: str, repo: str, issue_num: int) -> str:
    normalized_repo = repo.removesuffix(".git").lower()
    return f"{owner.lower()}/{normalized_repo}/issues/{issue_num}"


def _github_issue_uuid_seed(project_id: str, owner: str, repo: str, issue_num: int) -> str:
    return f"{project_id}/github/{_legacy_github_issue_uuid_seed(owner, repo, issue_num)}"


def _compute_path_cache(
    conn: Any,
    project_id: str | None,
    seq_num: int,
    parent_task_id: str | None,
    existing_tasks: dict[str, dict[str, Any]] | None = None,
) -> str:
    path_parts = [str(seq_num)]
    current_parent = parent_task_id
    for _ in range(100):
        if not current_parent:
            break
        parent_row = None
        if existing_tasks is not None:
            cached_parent = existing_tasks.get(current_parent)
            if cached_parent and cached_parent["project_id"] == project_id:
                parent_row = cached_parent
        if parent_row is None:
            if project_id is None:
                parent_row = conn.execute(
                    "SELECT seq_num, parent_task_id FROM tasks WHERE project_id IS NULL AND id = %s",
                    (current_parent,),
                ).fetchone()
            else:
                parent_row = conn.execute(
                    "SELECT seq_num, parent_task_id FROM tasks WHERE project_id = %s AND id = %s",
                    (project_id, current_parent),
                ).fetchone()
        if not parent_row or parent_row["seq_num"] is None:
            break
        path_parts.insert(0, str(parent_row["seq_num"]))
        current_parent = parent_row["parent_task_id"]
    return ".".join(path_parts)


def _ensure_task_sequence_metadata(
    conn: Any,
    *,
    project_id: str,
    task_id: str,
    updated_at: datetime,
) -> None:
    row = conn.execute(
        """
        SELECT seq_num, path_cache, parent_task_id
          FROM tasks
         WHERE project_id = %s
           AND id = %s
        """,
        (project_id, task_id),
    ).fetchone()
    if row is None:
        return

    seq_num = row["seq_num"]
    if seq_num is None:
        max_seq_row = conn.execute(
            "SELECT MAX(seq_num) as max_seq FROM tasks WHERE project_id = %s",
            (project_id,),
        ).fetchone()
        seq_num = ((max_seq_row["max_seq"] if max_seq_row else None) or 0) + 1
        conn.execute(
            "UPDATE tasks SET seq_num = %s, updated_at = %s WHERE project_id = %s AND id = %s",
            (seq_num, updated_at, project_id, task_id),
        )

    if row["path_cache"]:
        return

    conn.execute(
        "UPDATE tasks SET path_cache = %s, updated_at = %s WHERE project_id = %s AND id = %s",
        (
            _compute_path_cache(conn, project_id, seq_num, row["parent_task_id"]),
            updated_at,
            project_id,
            task_id,
        ),
    )


def _validate_uuid(value: Any, *, field: str, line_num: int, required: bool = False) -> None:
    if value is None and not required:
        return
    if not isinstance(value, str):
        raise TaskRestoreError(f"Task backup line {line_num}: {field} must be a UUID string")
    try:
        UUID(value)
    except ValueError as exc:
        raise TaskRestoreError(
            f"Task backup line {line_num}: {field} is not a valid UUID: {value}"
        ) from exc


def _load_task_backup(path: Path) -> list[dict[str, Any]]:
    """Parse and validate an entire task backup before any database access."""
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TaskRestoreError(f"Failed to read task backup {path}: {exc}") from exc

    for line_num, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TaskRestoreError(f"Invalid JSON in task backup on line {line_num}") from exc
        if not isinstance(data, dict):
            raise TaskRestoreError(f"Task backup line {line_num}: record must be an object")
        if "_deleted" in data:
            raise TaskRestoreError(
                f"Task backup line {line_num}: tombstone records are not supported"
            )

        task_id = data.get("id")
        _validate_uuid(task_id, field="id", line_num=line_num, required=True)
        assert isinstance(task_id, str)
        if task_id in seen_ids:
            raise TaskRestoreError(f"Task backup line {line_num}: duplicate id {task_id}")
        seen_ids.add(task_id)

        title = data.get("title")
        if not isinstance(title, str) or not title.strip():
            raise TaskRestoreError(f"Task backup line {line_num}: title must be non-empty")

        for field in ("created_at", "updated_at"):
            value = data.get(field)
            if not isinstance(value, str):
                raise TaskRestoreError(
                    f"Task backup line {line_num}: {field} must be an ISO timestamp"
                )
            try:
                _parse_timestamp(value)
            except ValueError as exc:
                raise TaskRestoreError(
                    f"Task backup line {line_num}: invalid {field} timestamp"
                ) from exc

        state = data.get("state") or {}
        for field in ("closed_at", "escalated_at"):
            value = data.get(field, state.get(field))
            if value is not None:
                try:
                    _parse_timestamp(value)
                except (TypeError, ValueError) as exc:
                    raise TaskRestoreError(
                        f"Task backup line {line_num}: invalid {field} timestamp"
                    ) from exc

        uuid_fields = (
            "project_id",
            "parent_id",
            "created_in_session_id",
            "claimed_by_session_id",
            "closed_in_session_id",
        )
        for field in uuid_fields:
            _validate_uuid(data.get(field), field=field, line_num=line_num)
        _validate_uuid(
            state.get("owner_session_id"), field="state.owner_session_id", line_num=line_num
        )
        _validate_uuid(
            state.get("closed_in_session_id"),
            field="state.closed_in_session_id",
            line_num=line_num,
        )

        for field in ("deps_on", "commits", "labels"):
            value = data.get(field)
            if value is not None and not isinstance(value, list):
                raise TaskRestoreError(f"Task backup line {line_num}: {field} must be a list")
            if (
                value is not None
                and field != "deps_on"
                and not all(isinstance(item, str) for item in value)
            ):
                raise TaskRestoreError(
                    f"Task backup line {line_num}: {field} entries must be strings"
                )
        for dependency in data.get("deps_on") or []:
            _validate_uuid(dependency, field="deps_on entry", line_num=line_num, required=True)

        for field in ("state", "validation"):
            value = data.get(field)
            if value is not None and not isinstance(value, dict):
                raise TaskRestoreError(f"Task backup line {line_num}: {field} must be an object")

        seq_num = data.get("seq_num")
        if seq_num is not None and (type(seq_num) is not int or seq_num <= 0):
            raise TaskRestoreError(
                f"Task backup line {line_num}: seq_num must be a positive integer"
            )

        records.append(data)

    return records


class TaskBackupManager:
    """Create and restore deterministic task JSONL backups."""

    def __init__(
        self,
        task_manager: LocalTaskManager,
        backup_path: str | Path | None = None,
    ) -> None:
        self.task_manager = task_manager
        self.db = task_manager.db
        self.backup_path = Path(backup_path or ".gobby/tasks.jsonl")
        self._custom_backup_path = backup_path is not None

    def _get_backup_path(self, project_id: str | None) -> Path:
        """Resolve the configured or project-local backup path."""
        if self._custom_backup_path or not project_id:
            return self.backup_path

        # Try to find project
        from gobby.storage.projects import LocalProjectManager

        project_manager = LocalProjectManager(self.db)
        project = project_manager.get(project_id)

        if project and project.repo_path:
            return Path(project.repo_path) / ".gobby" / "tasks.jsonl"

        return self.backup_path

    def backup(self, project_id: str | None = None) -> int:
        """Write current live task rows as a deterministic atomic JSONL snapshot."""
        try:
            target_path = self._get_backup_path(project_id)

            tasks = []
            offset = 0
            while page := self.task_manager.list_tasks(
                limit=TASK_BACKUP_PAGE_SIZE, offset=offset, project_id=project_id
            ):
                tasks.extend(page)
                offset += len(page)

            deps_rows = self.db.fetchall("SELECT task_id, depends_on FROM task_dependencies")

            deps_map: dict[str, list[str]] = {}
            for row in deps_rows:
                task_id = row["task_id"]
                if task_id not in deps_map:
                    deps_map[task_id] = []
                deps_map[task_id].append(row["depends_on"])

            tasks.sort(key=lambda t: t.id)

            backup_data: list[dict[str, Any]] = []
            for task in tasks:
                state = serialize_task_state(task)
                state["closed_at"] = _normalize_timestamp(task.closed_at)
                state["escalated_at"] = _normalize_timestamp(task.escalated_at)
                task_dict = {
                    "id": task.id,
                    "title": task.title,
                    "description": task.description,
                    "state": state,
                    "priority": task.priority,
                    "task_type": task.task_type,
                    "created_at": _normalize_timestamp(task.created_at),
                    "updated_at": _normalize_timestamp(task.updated_at),
                    "project_id": task.project_id,
                    "parent_id": task.parent_task_id,
                    "created_in_session_id": task.created_in_session_id,
                    "claimed_by_session_id": task.claimed_by_session_id,
                    "deps_on": sorted(deps_map.get(task.id, [])),  # Sort deps for stability
                    "commits": sorted(set(task.commits)) if task.commits else [],
                    "closed_at": _normalize_timestamp(task.closed_at),
                    "closed_reason": task.closed_reason,
                    "closed_in_session_id": task.closed_in_session_id,
                    "closed_commit_sha": task.closed_commit_sha,
                    "labels": task.labels if task.labels else None,
                    "validation": (
                        {
                            "state": task.validation_status,
                            "feedback": task.validation_feedback,
                            "fail_count": task.validation_fail_count,
                            "criteria": task.validation_criteria,
                            "override_reason": task.validation_override_reason,
                        }
                        if task.validation_status
                        else None
                    ),
                    # Expansion fields
                    "category": task.category,
                    # External integrations
                    "github_issue_number": task.github_issue_number,
                    "github_pr_number": task.github_pr_number,
                    "github_repo": task.github_repo,
                    "linear_issue_id": task.linear_issue_id,
                    "linear_team_id": task.linear_team_id,
                    # Scheduling fields
                    "start_date": _normalize_date(task.start_date),
                    "due_date": _normalize_date(task.due_date),
                    # Escalation fields (normalize timestamps)
                    "escalated_at": _normalize_timestamp(task.escalated_at),
                    "escalation_reason": task.escalation_reason,
                    # Human-friendly IDs (preserve across backup and restore)
                    "seq_num": task.seq_num,
                    "path_cache": task.path_cache,
                }
                backup_data.append(task_dict)

            with export_file_lock(target_path):
                content = "".join(json_dumps(item) + "\n" for item in backup_data)
                atomic_write_text(target_path, content)

            logger.info("Backed up %s tasks to %s", len(backup_data), target_path)
            return len(backup_data)

        except Exception as e:
            logger.exception("Failed to back up tasks: %s", e)
            raise TaskBackupError(str(e)) from e

    def restore(self, project_id: str | None = None) -> int:
        """Non-destructively restore tasks when backup timestamps win."""
        target_path = self._get_backup_path(project_id)

        if not target_path.exists():
            logger.debug("No task backup file found at %s, skipping restore", target_path)
            return 0

        try:
            records = _load_task_backup(target_path)

            imported_count = 0
            updated_count = 0
            skipped_count = 0

            # Phase 1: restore task rows with last-write-wins semantics.
            pending_deps: dict[str, list[str]] = {}
            pending_path_rebuilds: list[str] = []

            with self.db.transaction() as conn:
                conn.execute("SET CONSTRAINTS ALL DEFERRED")
                # Use the write transaction's snapshot for LWW and sequence decisions.
                existing_tasks: dict[str, dict[str, Any]] = {}
                for row in conn.execute(
                    "SELECT id, updated_at, seq_num, path_cache, project_id, parent_task_id, "
                    "claimed_by_session_id, created_in_session_id, closed_in_session_id "
                    "FROM tasks"
                ).fetchall():
                    existing_tasks[row["id"]] = {
                        "updated_at": row["updated_at"],
                        "seq_num": row["seq_num"],
                        "path_cache": row["path_cache"],
                        "project_id": row["project_id"],
                        "parent_task_id": row["parent_task_id"],
                        "claimed_by_session_id": row["claimed_by_session_id"],
                        "created_in_session_id": row["created_in_session_id"],
                        "closed_in_session_id": row["closed_in_session_id"],
                    }

                occupied_seq_nums: dict[str | None, set[int]] = {}
                max_seq_tracker: dict[str | None, int] = {}
                for task_meta in existing_tasks.values():
                    pid = task_meta["project_id"]
                    sn = task_meta["seq_num"]
                    if sn is not None:
                        occupied_seq_nums.setdefault(pid, set()).add(sn)
                        max_seq_tracker[pid] = max(max_seq_tracker.get(pid, 0), sn)
                batch_claimed: dict[str | None, set[int]] = {}
                existing_session_ids = {
                    row["id"] for row in conn.execute("SELECT id FROM sessions").fetchall()
                }
                for data in records:
                    task_id = data["id"]
                    updated_at_file = _parse_timestamp(data["updated_at"])

                    # Check against bulk-loaded existing task data
                    existing_row = existing_tasks.get(task_id)

                    should_update = False
                    existing_seq_num = None
                    existing_path_cache = None
                    if not existing_row:
                        should_update = True
                    else:
                        # Handle NULL timestamps in DB (treat as infinitely old)
                        db_updated_at = existing_row["updated_at"]
                        if db_updated_at is None:
                            updated_at_db = datetime.min.replace(tzinfo=UTC)
                        else:
                            try:
                                updated_at_db = _parse_timestamp(db_updated_at)
                            except ValueError as e:
                                logger.warning(
                                    "Task %s: failed to parse DB timestamp '%s': %s, treating as old",
                                    task_id,
                                    db_updated_at,
                                    e,
                                )
                                updated_at_db = datetime.min.replace(tzinfo=UTC)
                        existing_seq_num = existing_row["seq_num"]
                        existing_path_cache = existing_row["path_cache"]
                        if updated_at_file > updated_at_db:
                            should_update = True
                        else:
                            skipped_count += 1

                    if should_update:
                        state = data.get("state") or {}
                        try:
                            created_at_file = _parse_timestamp(data["created_at"])
                            closed_at_file = _parse_optional_timestamp(
                                data.get("closed_at", state.get("closed_at"))
                            )
                            escalated_at_file = _parse_optional_timestamp(
                                data.get("escalated_at", state.get("escalated_at"))
                            )
                        except (KeyError, TypeError, ValueError) as e:
                            logger.warning(
                                "Task %s: malformed timestamp field: %s, skipping", task_id, e
                            )
                            skipped_count += 1
                            continue

                        # Handle commits array stored as JSON in the hub.
                        commits_json = json.dumps(data["commits"]) if data.get("commits") else None

                        # Handle validation object (extract fields)
                        validation = data.get("validation") or {}
                        validation_status = validation.get("state") or validation.get("status")
                        validation_feedback = validation.get("feedback")
                        validation_fail_count = validation.get("fail_count", 0)
                        validation_criteria = validation.get("criteria")
                        validation_override_reason = validation.get("override_reason")

                        # Handle labels stored as JSON in the hub.
                        labels_raw = data.get("labels")
                        labels_json = json.dumps(labels_raw) if labels_raw else None

                        claimed_by_session_id = data.get("claimed_by_session_id")
                        if claimed_by_session_id is None:
                            claimed_by_session_id = state.get("owner_session_id")
                        if claimed_by_session_id is None and existing_row:
                            claimed_by_session_id = existing_row["claimed_by_session_id"]

                        created_in_session_id = data.get("created_in_session_id")
                        if created_in_session_id is None and existing_row:
                            created_in_session_id = existing_row["created_in_session_id"]

                        closed_in_session_id = data.get("closed_in_session_id")
                        if closed_in_session_id is None:
                            closed_in_session_id = state.get("closed_in_session_id")
                        if closed_in_session_id is None and existing_row:
                            closed_in_session_id = existing_row["closed_in_session_id"]

                        claimed_by_session_id = _known_session_id(
                            claimed_by_session_id,
                            existing_session_ids,
                        )
                        created_in_session_id = _known_session_id(
                            created_in_session_id,
                            existing_session_ids,
                        )
                        closed_in_session_id = _known_session_id(
                            closed_in_session_id,
                            existing_session_ids,
                        )

                        restored_values = {
                            "project_id": data.get("project_id"),
                            "title": data["title"],
                            "description": data.get("description"),
                            "parent_task_id": data.get("parent_id"),
                            "priority": data.get("priority", 2),
                            "task_type": data.get("task_type", "task"),
                            "created_at": created_at_file,
                            "updated_at": updated_at_file,
                            "created_in_session_id": created_in_session_id,
                            "claimed_by_session_id": claimed_by_session_id,
                            "commits": commits_json,
                            "closed_at": closed_at_file,
                            "closed_reason": data.get("closed_reason", state.get("closed_reason")),
                            "closed_in_session_id": closed_in_session_id,
                            "closed_commit_sha": data.get(
                                "closed_commit_sha", state.get("closed_commit_sha")
                            ),
                            "labels": labels_json,
                            "validation_status": validation_status,
                            "validation_feedback": validation_feedback,
                            "validation_fail_count": validation_fail_count,
                            "validation_criteria": validation_criteria,
                            "validation_override_reason": validation_override_reason,
                            "category": data.get("category"),
                            # Preserve automation and routing policy packed into state on export.
                            "allow_automation": state.get("allow_automation", False),
                            "unattended": state.get("unattended", False),
                            "isolation": state.get("isolation", "worktree"),
                            "assigned_agent": state.get("assigned_agent"),
                            "implementation_domain": state.get("implementation_domain"),
                            "additional_skills": (
                                json.dumps(state["additional_skills"])
                                if state.get("additional_skills") is not None
                                else None
                            ),
                            "github_issue_number": data.get("github_issue_number"),
                            "github_pr_number": data.get("github_pr_number"),
                            "github_repo": data.get("github_repo"),
                            "linear_issue_id": data.get("linear_issue_id"),
                            "linear_team_id": data.get("linear_team_id"),
                            "start_date": data.get("start_date"),
                            "due_date": data.get("due_date"),
                            "escalated_at": escalated_at_file,
                            "escalation_reason": data.get(
                                "escalation_reason", state.get("escalation_reason")
                            ),
                            "seq_num": existing_seq_num if existing_row else data.get("seq_num"),
                            "path_cache": (
                                existing_path_cache if existing_row else data.get("path_cache")
                            ),
                        }

                        if not existing_row:
                            # New task: preserve JSONL seq_num if available and unclaimed.
                            task_project_id = restored_values.get("project_id")
                            jsonl_seq = restored_values.get("seq_num")
                            occupied = occupied_seq_nums.get(
                                task_project_id, set()
                            ) | batch_claimed.get(task_project_id, set())

                            if jsonl_seq is not None and jsonl_seq not in occupied:
                                final_seq = jsonl_seq
                            else:
                                current_max = max_seq_tracker.get(task_project_id, 0)
                                final_seq = current_max + 1

                            restored_values["seq_num"] = final_seq
                            batch_claimed.setdefault(task_project_id, set()).add(final_seq)
                            max_seq_tracker[task_project_id] = max(
                                max_seq_tracker.get(task_project_id, 0), final_seq
                            )

                            # Rebuild after every task is present so file order cannot
                            # truncate a child's hierarchy.
                            restored_values["path_cache"] = str(final_seq)

                            columns = ", ".join(["id"] + list(restored_values.keys()))
                            placeholders = ", ".join(["%s"] * (1 + len(restored_values)))
                            cursor = conn.execute(
                                f"INSERT INTO {'tasks'} ({columns}) VALUES ({placeholders}) "
                                "ON CONFLICT (id) DO NOTHING",
                                (task_id, *restored_values.values()),
                            )
                            if cursor.rowcount == 0:
                                skipped_count += 1
                                continue
                            pending_path_rebuilds.append(task_id)
                            existing_tasks[task_id] = {
                                "updated_at": restored_values["updated_at"],
                                "seq_num": restored_values["seq_num"],
                                "path_cache": restored_values["path_cache"],
                                "project_id": restored_values["project_id"],
                                "parent_task_id": restored_values["parent_task_id"],
                                "claimed_by_session_id": restored_values["claimed_by_session_id"],
                                "created_in_session_id": restored_values["created_in_session_id"],
                                "closed_in_session_id": restored_values["closed_in_session_id"],
                            }
                            imported_count += 1
                        else:
                            set_clause = ", ".join(f"{col} = %s" for col in restored_values)
                            cursor = conn.execute(
                                f"UPDATE tasks SET {set_clause} WHERE id = %s "
                                "AND (updated_at IS NULL OR updated_at < %s)",
                                (*restored_values.values(), task_id, updated_at_file),
                            )
                            if cursor.rowcount == 0:
                                skipped_count += 1
                                continue
                            existing_tasks[task_id] = {
                                **existing_row,
                                "updated_at": restored_values["updated_at"],
                                "seq_num": restored_values["seq_num"],
                                "path_cache": restored_values["path_cache"],
                                "project_id": restored_values["project_id"],
                                "parent_task_id": restored_values["parent_task_id"],
                                "claimed_by_session_id": restored_values["claimed_by_session_id"],
                                "created_in_session_id": restored_values["created_in_session_id"],
                                "closed_in_session_id": restored_values["closed_in_session_id"],
                            }
                            updated_count += 1

                    # Collect dependencies for Phase 2
                    if should_update and "deps_on" in data:
                        pending_deps[task_id] = data["deps_on"]

                # Phase 2: Rebuild paths after all parents are available.
                for task_id in pending_path_rebuilds:
                    task_meta = existing_tasks[task_id]
                    path_cache = _compute_path_cache(
                        conn,
                        task_meta["project_id"],
                        task_meta["seq_num"],
                        task_meta["parent_task_id"],
                        existing_tasks,
                    )
                    conn.execute(
                        "UPDATE tasks SET path_cache = %s WHERE id = %s",
                        (path_cache, task_id),
                    )
                    task_meta["path_cache"] = path_cache

                # Phase 3: Import Dependencies
                for task_id, dependencies in pending_deps.items():
                    conn.execute(
                        "DELETE FROM task_dependencies WHERE task_id = %s AND dep_type = 'blocks'",
                        (task_id,),
                    )
                    for depends_on in dependencies:
                        if task_id not in existing_tasks or depends_on not in existing_tasks:
                            logger.warning(
                                "Skipping dependency %s -> %s: endpoint missing after import",
                                task_id,
                                depends_on,
                            )
                            continue
                        conn.execute(
                            """
                            INSERT INTO task_dependencies (
                                task_id, depends_on, dep_type, created_at
                            ) VALUES (%s, %s, 'blocks', %s)
                            ON CONFLICT (task_id, depends_on, dep_type) DO NOTHING
                            """,
                            (task_id, depends_on, datetime.now(UTC)),
                        )

            logger.info(
                "Restore complete: %s imported, %s updated, %s skipped",
                imported_count,
                updated_count,
                skipped_count,
            )

            if imported_count > 0 or updated_count > 0:
                try:
                    stats = self.task_manager.reindex_search(project_id)
                    logger.debug("Search index rebuilt with %s tasks", stats.get("item_count", 0))
                except Exception as e:
                    logger.warning("Failed to rebuild search index: %s", e)
            return imported_count + updated_count

        except Exception as e:
            logger.exception("Failed to restore tasks: %s", e)
            raise
