import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from gobby.storage.tasks import LocalTaskManager
from gobby.sync.jsonl_io import atomic_write_text, export_file_lock
from gobby.sync.task_github_sync import GitHubTaskSyncMixin
from gobby.sync.tombstones import (
    apply_tombstone,
    is_tombstone,
    load_tombstones,
    merge_jsonl_records,
    newer_record,
    record_timestamp,
)
from gobby.tasks.state_semantics import serialize_task_state
from gobby.utils.json_helpers import json_dumps

TASK_EXPORT_PAGE_SIZE = 100_000

logger = logging.getLogger(__name__)

# Removed in 0.2.28: continuous sync machinery (trigger_export, _process_export_queue,
# stop, shutdown, debounce state). The DB is the source of truth; JSONL export now
# happens on-demand via pre-push hook, CLI, and MCP tools. JSONL import is explicit.


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
    logger.warning("Dropping task sync session reference for unknown session %s", value)
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


class TaskSyncManager(GitHubTaskSyncMixin):
    """
    Manages synchronization of tasks to the filesystem (JSONL) for Git versioning.
    """

    def __init__(
        self,
        task_manager: LocalTaskManager,
        export_path: str = ".gobby/tasks.jsonl",
    ):
        """
        Initialize TaskSyncManager.

        Args:
            task_manager: LocalTaskManager instance
            export_path: Path to the JSONL export file
        """
        self.task_manager = task_manager
        self.db = task_manager.db
        self.export_path = Path(export_path)

    def _get_export_path(self, project_id: str | None) -> Path:
        """
        Resolve the export path for a given project.

        Resolution order:
        1. If project_id provided -> find project repo_path -> .gobby/tasks.jsonl
        2. Fallback to self.export_path (legacy/default behavior)
        """
        if not project_id:
            return self.export_path

        # Try to find project
        from gobby.storage.projects import LocalProjectManager

        project_manager = LocalProjectManager(self.db)
        project = project_manager.get(project_id)

        if project and project.repo_path:
            return Path(project.repo_path) / ".gobby" / "tasks.jsonl"

        return self.export_path

    def export_to_jsonl(self, project_id: str | None = None) -> None:
        """
        Export tasks and their dependencies to a JSONL file.
        Tasks are sorted by ID to ensure deterministic output.

        Args:
            project_id: Optional project to export. If matches context, uses project path.
        """
        try:
            target_path = self._get_export_path(project_id)

            tasks = []
            offset = 0
            while page := self.task_manager.list_tasks(
                limit=TASK_EXPORT_PAGE_SIZE, offset=offset, project_id=project_id
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

            export_data: list[dict[str, Any]] = []
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
                    # Human-friendly IDs (preserve across sync)
                    "seq_num": task.seq_num,
                    "path_cache": task.path_cache,
                }
                export_data.append(task_dict)

            with export_file_lock(target_path):
                current_records = [
                    *export_data,
                    *load_tombstones(self.db, "task", project_id),
                ]
                merged_records = sorted(
                    merge_jsonl_records(target_path, current_records, logger),
                    key=lambda item: item["id"],
                )
                content = "".join(json_dumps(item) + "\n" for item in merged_records)
                atomic_write_text(target_path, content)

            logger.info(f"Exported {len(merged_records)} tasks to {target_path}")

        except Exception as e:
            logger.error(f"Failed to export tasks: {e}", exc_info=True)
            raise

    def import_from_jsonl(self, project_id: str | None = None) -> None:
        """
        Import tasks from JSONL file into the hub database.
        Uses Last-Write-Wins conflict resolution based on updated_at.

        Args:
            project_id: Optional project to import from. If matches context, uses project path.
        """
        target_path = self._get_export_path(project_id)

        if not target_path.exists():
            logger.debug(f"No task export file found at {target_path}, skipping import")
            return

        try:
            with open(target_path, encoding="utf-8") as f:
                lines = f.readlines()

            tombstones: dict[str, dict[str, Any]] = {}
            for line in lines:
                if not line.strip():
                    continue
                candidate = json.loads(line)
                if not isinstance(candidate, dict) or not is_tombstone(candidate):
                    continue
                task_id = candidate.get("id")
                if not isinstance(task_id, str):
                    continue
                current = tombstones.get(task_id)
                tombstones[task_id] = (
                    candidate if current is None else newer_record(current, candidate)
                )

            imported_count = 0
            updated_count = 0
            deleted_count = 0
            skipped_count = 0

            # Phase 1: Import Tasks (Upsert)
            pending_deps: dict[str, list[str]] = {}
            pending_path_rebuilds: list[str] = []

            with self.db.transaction() as conn:
                conn.execute("SET CONSTRAINTS ALL DEFERRED")
                # Use the write transaction's snapshot for LWW and sequence decisions.
                existing_tasks: dict[str, dict[str, Any]] = {}
                for row in self.db.fetchall(
                    "SELECT id, updated_at, seq_num, path_cache, project_id, parent_task_id, "
                    "claimed_by_session_id, created_in_session_id, closed_in_session_id "
                    "FROM tasks"
                ):
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
                    row["id"] for row in self.db.fetchall("SELECT id FROM sessions")
                }
                for task_id, deletion_record in tombstones.items():
                    deleted_at = record_timestamp(deletion_record)
                    if deleted_at is not None and apply_tombstone(
                        conn, "task", task_id, deleted_at
                    ):
                        existing_tasks.pop(task_id, None)
                        deleted_count += 1
                for line in lines:
                    if not line.strip():
                        continue

                    data = json.loads(line)
                    task_id = data["id"]
                    if is_tombstone(data):
                        continue
                    # Guard against None/missing updated_at in JSONL
                    raw_updated_at = data.get("updated_at")
                    if raw_updated_at is None:
                        # Skip tasks without timestamps or use a safe default
                        logger.warning(f"Task {task_id} missing updated_at, skipping")
                        skipped_count += 1
                        continue
                    try:
                        updated_at_file = _parse_timestamp(raw_updated_at)
                    except ValueError as e:
                        logger.warning(
                            f"Task {task_id}: malformed timestamp '{raw_updated_at}': {e}, skipping"
                        )
                        skipped_count += 1
                        continue

                    task_tombstone = tombstones.get(task_id)
                    tombstone_at = (
                        record_timestamp(task_tombstone) if task_tombstone is not None else None
                    )
                    if tombstone_at is not None and tombstone_at >= updated_at_file:
                        skipped_count += 1
                        continue

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
                                    f"Task {task_id}: failed to parse DB timestamp "
                                    f"'{db_updated_at}': {e}, treating as old"
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
                                f"Task {task_id}: malformed timestamp field: {e}, skipping"
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

                        # Common synced field values
                        synced_values = {
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
                            task_project_id = synced_values.get("project_id")
                            jsonl_seq = synced_values.get("seq_num")
                            occupied = occupied_seq_nums.get(
                                task_project_id, set()
                            ) | batch_claimed.get(task_project_id, set())

                            if jsonl_seq is not None and jsonl_seq not in occupied:
                                final_seq = jsonl_seq
                            else:
                                current_max = max_seq_tracker.get(task_project_id, 0)
                                final_seq = current_max + 1

                            synced_values["seq_num"] = final_seq
                            batch_claimed.setdefault(task_project_id, set()).add(final_seq)
                            max_seq_tracker[task_project_id] = max(
                                max_seq_tracker.get(task_project_id, 0), final_seq
                            )

                            # Rebuild after every task is present so file order cannot
                            # truncate a child's hierarchy.
                            synced_values["path_cache"] = str(final_seq)

                            # INSERT with all synced fields
                            columns = ", ".join(["id"] + list(synced_values.keys()))
                            placeholders = ", ".join(["%s"] * (1 + len(synced_values)))
                            cursor = conn.execute(
                                f"INSERT INTO {'tasks'} ({columns}) VALUES ({placeholders}) "
                                "ON CONFLICT (id) DO NOTHING",
                                (task_id, *synced_values.values()),
                            )
                            if cursor.rowcount == 0:
                                skipped_count += 1
                                continue
                            pending_path_rebuilds.append(task_id)
                            existing_tasks[task_id] = {
                                "updated_at": synced_values["updated_at"],
                                "seq_num": synced_values["seq_num"],
                                "path_cache": synced_values["path_cache"],
                                "project_id": synced_values["project_id"],
                                "parent_task_id": synced_values["parent_task_id"],
                                "claimed_by_session_id": synced_values["claimed_by_session_id"],
                                "created_in_session_id": synced_values["created_in_session_id"],
                                "closed_in_session_id": synced_values["closed_in_session_id"],
                            }
                            imported_count += 1
                        else:
                            # Existing task: update synced fields while preserving local state.
                            set_clause = ", ".join(f"{col} = %s" for col in synced_values)
                            cursor = conn.execute(
                                f"UPDATE tasks SET {set_clause} WHERE id = %s "
                                "AND (updated_at IS NULL OR updated_at < %s)",
                                (*synced_values.values(), task_id, updated_at_file),
                            )
                            if cursor.rowcount == 0:
                                skipped_count += 1
                                continue
                            existing_tasks[task_id] = {
                                **existing_row,
                                "updated_at": synced_values["updated_at"],
                                "seq_num": synced_values["seq_num"],
                                "path_cache": synced_values["path_cache"],
                                "project_id": synced_values["project_id"],
                                "parent_task_id": synced_values["parent_task_id"],
                                "claimed_by_session_id": synced_values["claimed_by_session_id"],
                                "created_in_session_id": synced_values["created_in_session_id"],
                                "closed_in_session_id": synced_values["closed_in_session_id"],
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
                                f"Skipping dependency {task_id} -> {depends_on}: "
                                "endpoint missing after import"
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
                f"Import complete: {imported_count} imported, {deleted_count} deleted, "
                f"{updated_count} updated, {skipped_count} skipped"
            )

            # Rebuild search index to include imported tasks
            if imported_count > 0 or updated_count > 0 or deleted_count > 0:
                try:
                    stats = self.task_manager.reindex_search(project_id)
                    logger.debug(f"Search index rebuilt with {stats.get('item_count', 0)} tasks")
                except Exception as e:
                    logger.warning(f"Failed to rebuild search index: {e}")

        except Exception as e:
            logger.error(f"Failed to import tasks: {e}", exc_info=True)
            raise

    def get_sync_status(self) -> dict[str, Any]:
        """
        Get sync availability based on whether the export file exists.
        """
        result_key = "status"
        if not self.export_path.exists():
            return {result_key: "no_file", "synced": False}

        return {result_key: "available", "synced": True}
