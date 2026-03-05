"""MCP tools for task affected files.

Provides tools for managing file annotations on tasks:
- set_affected_files: Bulk set files for a task
- get_affected_files: Query files for a task
- find_file_overlaps: Detect file contention between tasks
- wire_affected_files_from_spec: Extract affected_files from expansion spec and wire to child tasks
"""

import json
import logging
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.task_affected_files import AnnotationSource, TaskAffectedFileManager
from gobby.storage.tasks import TaskNotFoundError

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext

__all__ = ["create_affected_files_registry"]


def create_affected_files_registry(ctx: "RegistryContext") -> InternalToolRegistry:
    """Create a registry with task affected file tools."""

    registry = InternalToolRegistry(
        name="gobby-tasks-affected-files",
        description="Task affected file management tools",
    )

    af_manager = TaskAffectedFileManager(ctx.task_manager.db)

    # --- set_affected_files ---

    def set_affected_files(
        task_id: str,
        files: list[str],
        source: str = "manual",
    ) -> dict[str, Any]:
        """Set affected files for a task."""
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Invalid task_id: {e}"}

        if source not in ("expansion", "manual", "observed"):
            return {
                "error": f"Invalid source: {source}. Must be 'expansion', 'manual', or 'observed'."
            }

        annotation_source: AnnotationSource = source  # type: ignore[assignment]
        results = af_manager.set_files(resolved_id, files, annotation_source)
        return {
            "task_id": resolved_id,
            "files_set": len(results),
            "files": [r.file_path for r in results],
        }

    registry.register(
        name="set_affected_files",
        description="Set affected files for a task. Replaces existing files for the given source.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N, path, or UUID",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of file paths (relative to repo root)",
                },
                "source": {
                    "type": "string",
                    "description": "Annotation source: 'expansion', 'manual' (default), or 'observed'",
                    "default": "manual",
                    "enum": ["expansion", "manual", "observed"],
                },
            },
            "required": ["task_id", "files"],
        },
        func=set_affected_files,
    )

    # --- get_affected_files ---

    def get_affected_files(task_id: str) -> dict[str, Any]:
        """Get affected files for a task."""
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Invalid task_id: {e}"}

        files = af_manager.get_files(resolved_id)
        return {
            "task_id": resolved_id,
            "count": len(files),
            "files": [f.to_dict() for f in files],
        }

    registry.register(
        name="get_affected_files",
        description="Get affected files for a task.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N, path, or UUID",
                },
            },
            "required": ["task_id"],
        },
        func=get_affected_files,
    )

    # --- find_file_overlaps ---

    def find_file_overlaps(task_ids: list[str]) -> dict[str, Any]:
        """Find file overlaps between tasks."""
        resolved_ids = []
        for tid in task_ids:
            try:
                resolved_ids.append(resolve_task_id_for_mcp(ctx.task_manager, tid))
            except (TaskNotFoundError, ValueError) as e:
                return {"error": f"Invalid task_id '{tid}': {e}"}

        overlaps = af_manager.find_overlapping_tasks(resolved_ids)
        # Convert tuple keys to string for JSON serialization
        result = []
        for (task_a, task_b), shared_files in overlaps.items():
            result.append(
                {
                    "task_a": task_a,
                    "task_b": task_b,
                    "shared_files": shared_files,
                    "overlap_count": len(shared_files),
                }
            )

        return {
            "task_count": len(resolved_ids),
            "overlapping_pairs": len(result),
            "overlaps": result,
        }

    registry.register(
        name="find_file_overlaps",
        description="Detect file contention between tasks. Returns pairs of tasks that share affected files.",
        input_schema={
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of task references to check for overlaps",
                },
            },
            "required": ["task_ids"],
        },
        func=find_file_overlaps,
    )

    # --- wire_affected_files_from_spec ---

    def wire_affected_files_from_spec(
        parent_task_id: str,
    ) -> dict[str, Any]:
        """Wire affected files from expansion spec to created child tasks.

        Reads the expansion spec from the parent task's expansion_context,
        extracts affected_files per subtask entry, matches them to child tasks
        by title, and stores via TaskAffectedFileManager.

        Args:
            parent_task_id: Parent task with completed expansion (has children)

        Returns:
            {"wired": int, "total_subtasks": int, "skipped": int}
        """
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, parent_task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Invalid parent_task_id: {e}"}

        # Load parent task and spec
        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return {"error": f"Task {parent_task_id} not found"}

        if not task.expansion_context:
            return {"error": "No expansion spec on parent task"}

        try:
            spec = json.loads(task.expansion_context)
        except json.JSONDecodeError as e:
            return {"error": f"Invalid expansion_context JSON: {e}"}

        subtasks_spec = spec.get("subtasks", [])
        if not subtasks_spec:
            return {"error": "No subtasks in expansion spec"}

        # Get child tasks
        children = ctx.task_manager.list_tasks(
            project_id=task.project_id,
            parent_task_id=resolved_id,
        )
        if not children:
            return {"error": "No child tasks found. Run execute_expansion first."}

        # Build title -> child task ID mapping (exact + normalized fallback)
        title_to_child_id: dict[str, str] = {}
        normalized_to_child_id: dict[str, str] = {}
        for child in children:
            title_to_child_id[child.title] = child.id
            normalized_to_child_id[child.title.strip().lower()] = child.id

        wired = 0
        skipped = 0
        for st in subtasks_spec:
            files = st.get("affected_files", [])
            if not files:
                skipped += 1
                continue

            title = st.get("title", "")
            child_id = title_to_child_id.get(title)
            if not child_id:
                child_id = normalized_to_child_id.get(title.strip().lower())
            if not child_id:
                logger.warning(f"wire_affected_files: no child task matches title '{title}'")
                skipped += 1
                continue

            af_manager.set_files(child_id, files, "expansion")
            wired += 1

        logger.info(
            f"Wired affected files for {wired}/{len(subtasks_spec)} subtasks "
            f"under parent {parent_task_id}"
        )

        return {
            "wired": wired,
            "total_subtasks": len(subtasks_spec),
            "skipped": skipped,
        }

    # --- update_observed_files ---

    def update_observed_files(
        task_id: str,
    ) -> dict[str, Any]:
        """Annotate a task's affected files from its linked commits.

        Looks up commits linked to the task, runs git diff-tree to get
        changed files, and stores them as 'observed' annotations. This
        provides post-hoc file tracking for conflict detection.

        Args:
            task_id: Task reference (#N, path, or UUID)

        Returns:
            Dict with task_id, commits_processed, files_observed, and files list
        """
        import subprocess

        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Invalid task_id: {e}"}

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return {"error": f"Task {task_id} not found"}

        # Get linked commits from task
        commit_shas = task.commits or []

        if not commit_shas:
            return {
                "task_id": resolved_id,
                "commits_processed": 0,
                "files_observed": 0,
                "files": [],
            }

        # Collect changed files from each commit
        all_files: set[str] = set()
        commits_processed = 0
        for sha in commit_shas:
            try:
                result = subprocess.run(
                    ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", sha],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    all_files.update(result.stdout.strip().split("\n"))
                    commits_processed += 1
            except (subprocess.TimeoutExpired, FileNotFoundError):
                logger.warning(f"Failed to get diff-tree for commit {sha}")

        if all_files:
            af_manager.set_files(resolved_id, sorted(all_files), "observed")

        return {
            "task_id": resolved_id,
            "commits_processed": commits_processed,
            "files_observed": len(all_files),
            "files": sorted(all_files),
        }

    registry.register(
        name="update_observed_files",
        description="Annotate a task's affected files from its linked commits. "
        "Runs git diff-tree on each commit to discover actually-changed files "
        "and stores them as 'observed' annotations for conflict detection.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N, path, or UUID",
                },
            },
            "required": ["task_id"],
        },
        func=update_observed_files,
    )

    registry.register(
        name="wire_affected_files_from_spec",
        description="Wire affected files from expansion spec to child tasks. Reads spec from parent, sets files on matching children.",
        input_schema={
            "type": "object",
            "properties": {
                "parent_task_id": {
                    "type": "string",
                    "description": "Parent task reference with completed expansion",
                },
            },
            "required": ["parent_task_id"],
        },
        func=wire_affected_files_from_spec,
    )

    return registry
