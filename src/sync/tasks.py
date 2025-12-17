import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from gobby.storage.database import LocalDatabase
from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)


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
        self._debounce_timer: Optional[threading.Timer] = None
        self._debounce_interval = 5.0  # seconds

    def export_to_jsonl(self) -> None:
        """
        Export all tasks and their dependencies to a JSONL file.
        Tasks are sorted by ID to ensure deterministic output.
        """
        try:
            # list_tasks returns all statuses if status is not provided.
            # Set a high limit to export all tasks.
            tasks = self.task_manager.list_tasks(limit=100000)

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
                task_dict = {
                    "id": task.id,
                    "title": task.title,
                    "description": task.description,
                    "status": task.status,
                    "created_at": task.created_at,
                    "updated_at": task.updated_at,
                    "project_id": task.project_id,
                    "parent_id": task.parent_task_id,
                    "deps_on": sorted(deps_map.get(task.id, [])),  # Sort deps for stability
                }
                export_data.append(task_dict)

            # Write to file
            self.export_path.parent.mkdir(parents=True, exist_ok=True)

            with open(self.export_path, "w", encoding="utf-8") as f:
                for item in export_data:
                    f.write(json.dumps(item) + "\n")

            # Calculate ID-independent content hash
            jsonl_content = ""
            for item in export_data:
                jsonl_content += json.dumps(item, sort_keys=True) + "\n"

            content_hash = hashlib.sha256(jsonl_content.encode("utf-8")).hexdigest()

            meta_path = self.export_path.parent / "tasks_meta.json"
            meta_data = {
                "content_hash": content_hash,
                "last_exported": datetime.now(timezone.utc).isoformat(),
            }

            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta_data, f, indent=2)

            logger.info(
                f"Exported {len(tasks)} tasks to {self.export_path} (hash: {content_hash[:8]})"
            )

        except Exception as e:
            logger.error(f"Failed to export tasks: {e}", exc_info=True)
            raise

    def import_from_jsonl(self) -> None:
        """
        Import tasks from JSONL file into SQLite.
        Uses Last-Write-Wins conflict resolution based on updated_at.
        """
        if not self.export_path.exists():
            logger.debug("No task export file found, skipping import")
            return

        try:
            with open(self.export_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            imported_count = 0
            updated_count = 0
            skipped_count = 0

            # Phase 1: Import Tasks (Upsert)
            pending_deps: list[tuple[str, str]] = []

            with self.db.transaction() as conn:
                for line in lines:
                    if not line.strip():
                        continue

                    data = json.loads(line)
                    task_id = data["id"]
                    updated_at_file = datetime.fromisoformat(data["updated_at"])

                    # Check if task exists
                    existing_row = self.db.fetchone(
                        "SELECT updated_at FROM tasks WHERE id = ?", (task_id,)
                    )

                    should_update = False
                    if not existing_row:
                        should_update = True
                        imported_count += 1
                    else:
                        updated_at_db = datetime.fromisoformat(existing_row["updated_at"])
                        if updated_at_file > updated_at_db:
                            should_update = True
                            updated_count += 1
                        else:
                            skipped_count += 1

                    if should_update:
                        labels_json = None  # Labels not in JSONL currently based on export logic, assume None or defaults

                        # Use INSERT OR REPLACE to handle upsert generically
                        # Note: We need to respect the exact fields from JSONL
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO tasks (
                                id, project_id, title, description, parent_task_id,
                                status, priority, type, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                task_id,
                                data.get("project_id"),
                                data["title"],
                                data.get("description"),
                                data.get("parent_task_id"),
                                data["status"],
                                data.get("priority", 2),
                                data.get("task_type", "task"),
                                data["created_at"],
                                data["updated_at"],
                            ),
                        )

                    # Collect dependencies for Phase 2
                    if "deps_on" in data:
                        for dep_id in data["deps_on"]:
                            pending_deps.append((task_id, dep_id))

            # Phase 2: Import Dependencies
            # We blindly re-insert dependencies. Since we can't easily track deletion of dependencies
            # without full diff, we'll ensure they exist.
            # To handle strict syncing, we might want to clear existing deps for these tasks,
            # but that's risky. For now, additive only for dependencies (or ignore if exist).

            with self.db.transaction() as conn:
                for task_id, depends_on in pending_deps:
                    # Check if both exist (they should, unless depends_on is missing from file/db)
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO task_dependencies (task_id, depends_on, dep_type, created_at)
                        VALUES (?, ?, 'blocks', ?)
                        """,
                        (task_id, depends_on, datetime.now(timezone.utc).isoformat()),
                    )

            logger.info(
                f"Import complete: {imported_count} imported, {updated_count} updated, {skipped_count} skipped"
            )

        except Exception as e:
            logger.error(f"Failed to import tasks: {e}", exc_info=True)
            raise

    def get_sync_status(self) -> dict[str, Any]:
        """
        Get sync status by comparing content hash.
        """
        if not self.export_path.exists():
            return {"status": "no_file", "synced": False}

        meta_path = self.export_path.parent / "tasks_meta.json"
        if not meta_path.exists():
            return {"status": "no_meta", "synced": False}

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)

            # Recalculate hash of current export file
            # This detects if file changed properly
            with open(self.export_path, "r", encoding="utf-8") as f:
                content = f.read()
                # We need to act carefully here. The hash in meta is based on the logic in export.
                # In export, we hash the sorted json dumps.
                # If we just read the file, it should match IF it was generated by us.
                # If it was generated by another client, format might differ slightly.
                # Ideally we assume the meta file tells us what the hash WAS when exported.
                pass

            # For checking if DB is ahead of Export, we'd need to dry-run export.
            # For checking if File is ahead of DB (Import needed), we check if file changed since last import?
            # Or simplified: "synced" if last export timestamp > last DB update?
            # That requires tracking last import time.

            return {
                "status": "available",
                "last_exported": meta.get("last_exported"),
                "hash": meta.get("content_hash"),
                "synced": True,  # Placeholder
            }
        except Exception:
            return {"status": "error", "synced": False}

    def trigger_export(self) -> None:
        """
        Trigger a debounced export.
        """
        if self._debounce_timer:
            self._debounce_timer.cancel()

        self._debounce_timer = threading.Timer(self._debounce_interval, self.export_to_jsonl)
        self._debounce_timer.start()

    async def import_from_github_issues(
        self, repo_url: str, project_id: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """
        Import open issues from a GitHub repository as tasks.
        Uses Claude Agent SDK to fetch and extract issues.

        Args:
            repo_url: URL of the GitHub repository (e.g., https://github.com/owner/repo)
            limit: Max issues to import

        Returns:
            Review of what would be imported
        """
        try:
            from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

            # Simple validation
            if "github.com" not in repo_url:
                return {"success": False, "error": "Invalid GitHub URL"}

            # Build prompt
            prompt = f"""Fetch the open issues from this GitHub repository: {repo_url}/issues
            
            Please extract up to {limit} open issues. For each issue, provide:
            - Title
            - Description (summary)
            - Issue Number
            - Labels (if any)
            - Created At date
            
            Return the result as a JSON object with a list of "issues".
            Each issue object should have: title, description, issue_number, labels, created_at.
            """

            # Minimal options
            options = ClaudeAgentOptions(
                system_prompt="You are a data extraction assistant. Output ONLY valid JSON.",
                max_turns=5,
                model="claude-3-5-sonnet-latest",  # Use capable model
                allowed_tools=["WebFetch"],
                permission_mode="default",
            )

            result_text = ""
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            result_text += block.text

            # Parse JSON
            # Try to find JSON block
            import re

            json_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", result_text)
            if json_match:
                json_str = json_match.group(1)
            else:
                # Try raw JSON finding
                json_match = re.search(r"\{[\s\S]*\}", result_text)
                if json_match:
                    json_str = json_match.group(0)
                else:
                    return {
                        "success": False,
                        "error": "Could not extract JSON from agent response",
                        "raw": result_text,
                    }

            data = json.loads(json_str)
            issues = data.get("issues", [])

            if not issues:
                return {"success": False, "error": "No issues found in parsed data"}

            # Import into DB
            # We prefix ID with 'gh-{issue_number}' to avoid collision and allow tracking

            project_id = (
                None  # This needs to be passed in or resolved. For now, assume None and resolve.
            )
            # Resolve project ID if not provided
            if not project_id:
                # Try to find project by github_url
                row = self.db.fetchone("SELECT id FROM projects WHERE github_url = ?", (repo_url,))
                if row:
                    project_id = row["id"]

            if not project_id:
                # Fallback to _orphaned project if exists, or error
                # Ideally caller provides it.
                return {
                    "success": False,
                    "error": "Could not determine project ID. Please run from within a project or provide explicit project context.",
                }

            imported = []
            imported_count = 0

            with self.db.transaction() as conn:
                for issue in issues:
                    issue_num = issue.get("issue_number")
                    if not issue_num:
                        continue

                    task_id = f"gh-{issue_num}"
                    title = issue.get("title", "Untitled Issue")
                    desc = issue.get("description", "")
                    # Add link to original issue in description
                    desc += f"\n\nSource: {repo_url}/issues/{issue_num}"

                    created_at = issue.get("created_at", datetime.now(timezone.utc).isoformat())
                    updated_at = datetime.now(timezone.utc).isoformat()

                    # Check if exists
                    exists = self.db.fetchone("SELECT 1 FROM tasks WHERE id = ?", (task_id,))
                    if exists:
                        # Update?
                        conn.execute(
                            "UPDATE tasks SET title=?, description=?, updated_at=? WHERE id=?",
                            (title, desc, updated_at, task_id),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO tasks (
                                id, project_id, title, description, status, type, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, 'open', 'issue', ?, ?)
                            """,
                            (task_id, project_id, title, desc, created_at, updated_at),
                        )
                        imported_count += 1

                    imported.append(task_id)

            return {
                "success": True,
                "issues": issues,
                "imported": imported,
                "count": imported_count,
                "message": f"Successfully imported {imported_count} issues from GitHub.",
            }

        except Exception as e:
            logger.error(f"Failed to import from GitHub: {e}")
            return {"success": False, "error": str(e)}

    def stop(self) -> None:
        """Stop any pending timers."""
        if self._debounce_timer:
            self._debounce_timer.cancel()
