import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.state_semantics import lifecycle_stage_from_status, serialize_task_state

logger = logging.getLogger(__name__)

# Removed in 0.2.28: continuous sync machinery (trigger_export, _process_export_queue,
# stop, shutdown, debounce state). The DB is the source of truth; JSONL export now
# happens on-demand via pre-push hook, CLI, and MCP tools. JSONL import is explicit.


def _parse_timestamp(ts: str) -> datetime:
    """Parse ISO 8601 timestamp string to datetime.

    Handles both Z suffix and +HH:MM offset formats for compatibility
    with existing data that may use either format.

    Args:
        ts: ISO 8601 timestamp string (e.g., "2026-01-25T01:43:54Z" or
            "2026-01-25T01:43:54.123456+00:00")

    Returns:
        Timezone-aware datetime object in UTC
    """
    # Handle Z suffix for fromisoformat compatibility
    parse_ts = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(parse_ts)

    # Ensure timezone is UTC
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _normalize_timestamp(ts: str | None) -> str | None:
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
        return ts

    # Format with consistent microseconds and +00:00 suffix
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{dt.microsecond:06d}+00:00"


class TaskSyncManager:
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
            # Determine target path
            target_path = self._get_export_path(project_id)

            # Filter tasks by project_id if provided
            # This ensures we only export tasks for the specific project

            tasks = self.task_manager.list_tasks(limit=100000, project_id=project_id)

            # Fetch all dependencies
            # We'll use a raw query for efficiency here instead of calling get_blockers for every task
            deps_rows = self.db.fetchall("SELECT task_id, depends_on FROM task_dependencies")

            # Build dependency map: task_id -> list[depends_on]
            deps_map: dict[str, list[str]] = {}
            for task_id, depends_on in deps_rows:
                if task_id not in deps_map:
                    deps_map[task_id] = []
                deps_map[task_id].append(depends_on)

            # Sort tasks by ID for deterministic output
            tasks.sort(key=lambda t: t.id)

            export_data = []
            for task in tasks:
                state = serialize_task_state(task)
                state["closed_at"] = _normalize_timestamp(task.closed_at)
                state["escalated_at"] = _normalize_timestamp(task.escalated_at)
                task_dict = {
                    "id": task.id,
                    "title": task.title,
                    "description": task.description,
                    "state": state,
                    "status": task.status,
                    "priority": task.priority,
                    "task_type": task.task_type,
                    # Normalize timestamps to ensure RFC 3339 compliance (with timezone)
                    "created_at": _normalize_timestamp(task.created_at),
                    "updated_at": _normalize_timestamp(task.updated_at),
                    "project_id": task.project_id,
                    "parent_id": task.parent_task_id,
                    "created_in_session_id": task.created_in_session_id,
                    "claimed_by_session_id": task.claimed_by_session_id,
                    "lifecycle_stage": task.lifecycle_stage,
                    "deps_on": sorted(deps_map.get(task.id, [])),  # Sort deps for stability
                    # Commit SHAs are already normalized at write time by link_commit()
                    "commits": sorted(set(task.commits)) if task.commits else [],
                    # Closed state fields
                    "closed_at": _normalize_timestamp(task.closed_at),
                    "closed_reason": task.closed_reason,
                    "closed_in_session_id": task.closed_in_session_id,
                    "closed_commit_sha": task.closed_commit_sha,
                    # Labels (already a list on Task model)
                    "labels": task.labels if task.labels else None,
                    # Validation history (for tracking validation state across syncs)
                    "validation": (
                        {
                            "status": task.validation_status,
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
                    "start_date": task.start_date,
                    "due_date": task.due_date,
                    # Escalation fields (normalize timestamps)
                    "escalated_at": _normalize_timestamp(task.escalated_at),
                    "escalation_reason": task.escalation_reason,
                    # Human-friendly IDs (preserve across sync)
                    "seq_num": task.seq_num,
                    "path_cache": task.path_cache,
                }
                export_data.append(task_dict)

            # Write JSONL file
            target_path.parent.mkdir(parents=True, exist_ok=True)

            with open(target_path, "w", encoding="utf-8") as f:
                for item in export_data:
                    f.write(json.dumps(item) + "\n")

            logger.info(f"Exported {len(tasks)} tasks to {target_path}")

        except Exception as e:
            logger.error(f"Failed to export tasks: {e}", exc_info=True)
            raise

    def import_from_jsonl(self, project_id: str | None = None) -> None:
        """
        Import tasks from JSONL file into SQLite.
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

            imported_count = 0
            updated_count = 0
            skipped_count = 0

            # Phase 1: Import Tasks (Upsert)
            pending_deps: list[tuple[str, str]] = []

            # Bulk-load existing task metadata in one query to avoid per-task SELECTs
            existing_tasks: dict[str, dict[str, Any]] = {}
            for row in self.db.fetchall(
                "SELECT id, updated_at, seq_num, path_cache, project_id, "
                "claimed_by_session_id, created_in_session_id, closed_in_session_id, "
                "lifecycle_stage FROM tasks"
            ):
                existing_tasks[row["id"]] = {
                    "updated_at": row["updated_at"],
                    "seq_num": row["seq_num"],
                    "path_cache": row["path_cache"],
                    "project_id": row["project_id"],
                    "claimed_by_session_id": row["claimed_by_session_id"],
                    "created_in_session_id": row["created_in_session_id"],
                    "closed_in_session_id": row["closed_in_session_id"],
                    "lifecycle_stage": row["lifecycle_stage"],
                }

            # Track occupied seq_nums per project to preserve JSONL values
            # and only assign fresh ones on collision
            occupied_seq_nums: dict[str | None, set[int]] = {}
            max_seq_tracker: dict[str | None, int] = {}
            for task_meta in existing_tasks.values():
                pid = task_meta["project_id"]
                sn = task_meta["seq_num"]
                if sn is not None:
                    occupied_seq_nums.setdefault(pid, set()).add(sn)
                    max_seq_tracker[pid] = max(max_seq_tracker.get(pid, 0), sn)
            batch_claimed: dict[str | None, set[int]] = {}

            # Temporarily disable foreign keys to allow inserting child tasks
            # before their parents (JSONL order may not be parent-first)
            self.db.execute("PRAGMA foreign_keys = OFF")

            try:
                with self.db.transaction() as conn:
                    for line in lines:
                        if not line.strip():
                            continue

                        data = json.loads(line)
                        task_id = data["id"]
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

                        # Check against bulk-loaded existing task data
                        existing_row = existing_tasks.get(task_id)

                        should_update = False
                        existing_seq_num = None
                        existing_path_cache = None
                        if not existing_row:
                            should_update = True
                            imported_count += 1
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
                                updated_count += 1
                            else:
                                skipped_count += 1

                        if should_update:
                            state = data.get("state") or {}

                            # Handle commits array (stored as JSON in SQLite)
                            commits_json = (
                                json.dumps(data["commits"]) if data.get("commits") else None
                            )

                            # Handle validation object (extract fields)
                            validation = data.get("validation") or {}
                            validation_status = validation.get("status")
                            validation_feedback = validation.get("feedback")
                            validation_fail_count = validation.get("fail_count", 0)
                            validation_criteria = validation.get("criteria")
                            validation_override_reason = validation.get("override_reason")

                            # Handle labels (stored as JSON in SQLite)
                            labels_raw = data.get("labels")
                            labels_json = json.dumps(labels_raw) if labels_raw else None

                            legacy_status = data.get("status") or "open"
                            lifecycle_stage = data.get("lifecycle_stage")
                            if lifecycle_stage is None:
                                lifecycle_stage = state.get("lifecycle_stage")
                            if lifecycle_stage is None:
                                lifecycle_stage = (
                                    existing_row["lifecycle_stage"] if existing_row else None
                                )
                            if lifecycle_stage is None:
                                lifecycle_stage = lifecycle_stage_from_status(legacy_status)

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

                            # Common synced field values
                            synced_values = {
                                "project_id": data.get("project_id"),
                                "title": data["title"],
                                "description": data.get("description"),
                                "parent_task_id": data.get("parent_id"),
                                "status": legacy_status,
                                "priority": data.get("priority", 2),
                                "task_type": data.get("task_type", "task"),
                                "created_at": data["created_at"],
                                "updated_at": data["updated_at"],
                                "created_in_session_id": created_in_session_id,
                                "claimed_by_session_id": claimed_by_session_id,
                                "lifecycle_stage": lifecycle_stage,
                                "commits": commits_json,
                                "closed_at": data.get("closed_at", state.get("closed_at")),
                                "closed_reason": data.get(
                                    "closed_reason", state.get("closed_reason")
                                ),
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
                                "github_issue_number": data.get("github_issue_number"),
                                "github_pr_number": data.get("github_pr_number"),
                                "github_repo": data.get("github_repo"),
                                "linear_issue_id": data.get("linear_issue_id"),
                                "linear_team_id": data.get("linear_team_id"),
                                "start_date": data.get("start_date"),
                                "due_date": data.get("due_date"),
                                "escalated_at": data.get("escalated_at", state.get("escalated_at")),
                                "escalation_reason": data.get(
                                    "escalation_reason", state.get("escalation_reason")
                                ),
                                "seq_num": (
                                    data["seq_num"] if "seq_num" in data else existing_seq_num
                                ),
                                "path_cache": (
                                    data["path_cache"]
                                    if "path_cache" in data
                                    else existing_path_cache
                                ),
                            }

                            if not existing_row:
                                # New task — preserve JSONL seq_num if available
                                # and not already occupied; assign fresh only on collision
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

                                # Rebuild path_cache from the final seq_num
                                parent_id = synced_values.get("parent_task_id")
                                path_parts: list[str] = [str(final_seq)]
                                current_parent = parent_id
                                max_depth = 100
                                depth = 0
                                while current_parent and depth < max_depth:
                                    parent_row = conn.execute(
                                        "SELECT seq_num, parent_task_id FROM tasks WHERE id = ?",
                                        (current_parent,),
                                    ).fetchone()
                                    if not parent_row or parent_row["seq_num"] is None:
                                        break
                                    path_parts.insert(0, str(parent_row["seq_num"]))
                                    current_parent = parent_row["parent_task_id"]
                                    depth += 1
                                synced_values["path_cache"] = ".".join(path_parts)

                                # INSERT with all synced fields
                                columns = ", ".join(["id"] + list(synced_values.keys()))
                                placeholders = ", ".join(["?"] * (1 + len(synced_values)))
                                conn.execute(
                                    f"INSERT INTO tasks ({columns}) VALUES ({placeholders})",
                                    (task_id, *synced_values.values()),
                                )
                            else:
                                # Existing task — UPDATE only synced fields,
                                # preserving session-local columns (assignee,
                                # created_in_session_id, closed_in_session_id,
                                # compacted_at, summary)
                                set_clause = ", ".join(f"{col} = ?" for col in synced_values)
                                conn.execute(
                                    f"UPDATE tasks SET {set_clause} WHERE id = ?",
                                    (*synced_values.values(), task_id),
                                )

                        # Collect dependencies for Phase 2
                        if "deps_on" in data:
                            for dep_id in data["deps_on"]:
                                pending_deps.append((task_id, dep_id))

                # Phase 2: Import Dependencies
                # We blindly re-insert dependencies. Since we can't easily track deletion
                # of dependencies without full diff, we'll ensure they exist.
                # To handle strict syncing, we might want to clear existing deps for these
                # tasks, but that's risky. For now, additive only for deps (or ignore if exist).

                with self.db.transaction() as conn:
                    for task_id, depends_on in pending_deps:
                        # Check if both exist (they should, unless depends_on is missing)
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO task_dependencies (
                                task_id, depends_on, dep_type, created_at
                            ) VALUES (?, ?, 'blocks', ?)
                            """,
                            (task_id, depends_on, datetime.now(UTC).isoformat()),
                        )

                logger.info(
                    f"Import complete: {imported_count} imported, "
                    f"{updated_count} updated, {skipped_count} skipped"
                )

                # Rebuild search index to include imported tasks
                if imported_count > 0 or updated_count > 0:
                    try:
                        stats = self.task_manager.reindex_search(project_id)
                        logger.debug(
                            f"Search index rebuilt with {stats.get('item_count', 0)} tasks"
                        )
                    except Exception as e:
                        logger.warning(f"Failed to rebuild search index: {e}")
            finally:
                # Re-enable foreign keys
                self.db.execute("PRAGMA foreign_keys = ON")

        except Exception as e:
            logger.error(f"Failed to import tasks: {e}", exc_info=True)
            raise

    def get_sync_status(self) -> dict[str, Any]:
        """
        Get sync status based on whether the export file exists.
        """
        if not self.export_path.exists():
            return {"status": "no_file", "synced": False}

        return {"status": "available", "synced": True}

    async def import_from_github_issues(
        self, repo_url: str, project_id: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """
        Import open issues from a GitHub repository as tasks.
        Uses GitHub MCP server when available, falls back to gh CLI.

        Args:
            repo_url: URL of the GitHub repository (e.g., https://github.com/owner/repo)
            project_id: Optional project ID (auto-detected from context if not provided)
            limit: Max issues to import

        Returns:
            Result with imported issue IDs
        """
        import re

        try:
            # Parse repo from URL
            match = re.match(r"https?://github\.com/([^/]+)/([^/]+)/?", repo_url)
            if not match:
                return {
                    "success": False,
                    "error": "Invalid GitHub URL. Expected: https://github.com/owner/repo",
                }

            owner, repo = match.groups()
            repo = repo.rstrip(".git")  # Handle .git suffix

            # Resolve project ID if not provided
            if not project_id:
                row = self.db.fetchone("SELECT id FROM projects WHERE github_url = ?", (repo_url,))
                if row:
                    project_id = row["id"]

            if not project_id:
                from gobby.utils.project_context import get_project_context

                ctx = get_project_context()
                if ctx and ctx.get("id"):
                    project_id = ctx["id"]

            if not project_id:
                return {
                    "success": False,
                    "error": "Could not determine project ID. Run from within a gobby project.",
                }

            # Try GitHub MCP first
            issues = await self._fetch_github_issues_mcp(owner, repo, limit)
            if issues is None:
                # Fallback to gh CLI
                issues = await asyncio.to_thread(
                    self._fetch_github_issues_cli, owner, repo, repo_url, limit
                )
                if issues is None:
                    return {
                        "success": False,
                        "error": "GitHub MCP unavailable and gh CLI not found. "
                        "Install from https://cli.github.com/",
                    }

            if not issues:
                return {
                    "success": True,
                    "message": "No open issues found",
                    "imported": [],
                    "count": 0,
                }

            imported = []
            imported_count = 0

            with self.db.transaction() as conn:
                for issue in issues:
                    issue_num = issue.get("number")
                    if not issue_num:
                        continue

                    task_id = f"gh-{issue_num}"
                    title = issue.get("title", "Untitled Issue")
                    body = issue.get("body") or ""
                    desc = f"{body}\n\nSource: {repo_url}/issues/{issue_num}".strip()

                    labels = [lbl.get("name") for lbl in issue.get("labels", []) if lbl.get("name")]
                    labels_json = json.dumps(labels) if labels else None

                    created_at = (
                        issue.get("createdAt")
                        or issue.get("created_at")
                        or datetime.now(UTC).isoformat()
                    )
                    updated_at = datetime.now(UTC).isoformat()

                    exists = self.db.fetchone("SELECT 1 FROM tasks WHERE id = ?", (task_id,))
                    if exists:
                        conn.execute(
                            "UPDATE tasks SET title=?, description=?, labels=?, updated_at=? WHERE id=?",
                            (title, desc, labels_json, updated_at, task_id),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO tasks (
                                id, project_id, title, description, status, task_type,
                                labels, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, 'open', 'issue', ?, ?, ?)
                            """,
                            (task_id, project_id, title, desc, labels_json, created_at, updated_at),
                        )
                        imported_count += 1

                    imported.append(task_id)

            return {
                "success": True,
                "imported": imported,
                "count": imported_count,
                "message": f"Imported {imported_count} new issues, updated {len(imported) - imported_count} existing.",
            }

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse GitHub response: {e}")
            return {"success": False, "error": f"Failed to parse GitHub response: {e}"}
        except Exception as e:
            logger.error(f"Failed to import from GitHub: {e}")
            return {"success": False, "error": str(e)}

    async def _fetch_github_issues_mcp(
        self, owner: str, repo: str, limit: int
    ) -> list[dict[str, Any]] | None:
        """Fetch issues using GitHub MCP server. Returns None if unavailable."""
        try:
            from gobby.app_context import get_app_context
            from gobby.integrations.github import GitHubIntegration

            ctx = get_app_context()
            if not ctx or not ctx.mcp_manager:
                return None

            gh = GitHubIntegration(ctx.mcp_manager)
            if not gh.is_available():
                return None

            session = await ctx.mcp_manager.get_client_session("github")
            result = await session.call_tool(
                "list_issues",
                {
                    "owner": owner,
                    "repo": repo,
                    "state": "open",
                    "per_page": min(limit, 100),
                },
            )

            # Parse MCP response
            if hasattr(result, "content") and result.content:
                for item in result.content:
                    if hasattr(item, "text"):
                        try:
                            data = json.loads(item.text)
                            return data if isinstance(data, list) else []
                        except (json.JSONDecodeError, TypeError):
                            pass
            return []
        except Exception as e:
            logger.debug(f"GitHub MCP issue fetch failed: {e}")
            return None

    def _fetch_github_issues_cli(
        self, owner: str, repo: str, repo_url: str, limit: int
    ) -> list[dict[str, Any]] | None:
        """Fetch issues using gh CLI. Returns None if gh is not available."""
        import subprocess  # nosec B404

        try:
            subprocess.run(["gh", "--version"], capture_output=True, check=True)  # nosec B603 B607
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

        cmd = [
            "gh",
            "issue",
            "list",
            "--repo",
            f"{owner}/{repo}",
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "number,title,body,labels,createdAt",
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)  # nosec B603
        if result.returncode != 0:
            raise RuntimeError(f"gh command failed: {result.stderr}")

        parsed: list[dict[str, Any]] = json.loads(result.stdout)
        return parsed
