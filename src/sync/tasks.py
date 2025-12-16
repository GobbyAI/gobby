import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional
import time
import threading

from gobby.storage.database import LocalDatabase
from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)


class TaskSyncManager:
    """
    Manages synchronization of tasks to the filesystem (JSONL) for Git versioning.
    """

    def __init__(self, db: LocalDatabase, export_path: str = ".gobby/tasks.jsonl"):
        """
        Initialize TaskSyncManager.

        Args:
            db: LocalDatabase instance
            export_path: Path to the JSONL export file
        """
        self.db = db
        self.task_manager = LocalTaskManager(db)
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
            deps_map: dict[str, List[str]] = {}
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

            # Write key metadata if needed (not in this pass, but good placeholder)

            with open(self.export_path, "w", encoding="utf-8") as f:
                for item in export_data:
                    f.write(json.dumps(item) + "\n")

            # Calculate ID-independent content hash
            # To do this robustly, we should hash the canonical JSONL content
            # But we can just hash the file content we just wrote.
            # Actually, let's hash the export_data structure to avoid file I/O dependence

            # We need a stable representation. JSON dumps with sort_keys=True is good.
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
            raise  # Re-raise explicitely for tests/debugging if needed, or remove for production resilience

    def trigger_export(self) -> None:
        """
        Trigger a debounced export.
        """
        if self._debounce_timer:
            self._debounce_timer.cancel()

        self._debounce_timer = threading.Timer(self._debounce_interval, self.export_to_jsonl)
        self._debounce_timer.start()

    def stop(self) -> None:
        """Stop any pending timers."""
        if self._debounce_timer:
            self._debounce_timer.cancel()
