"""
Task tree builder module.

Creates task hierarchies from JSON tree structures. Simpler alternative
to TaskHierarchyBuilder which parses markdown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)


@dataclass
class TreeBuildResult:
    """Result of building a task tree."""

    tasks_created: int
    epic_ref: str | None  # Short ref for the root task (e.g., "#42")
    task_refs: list[str]  # All created task refs
    errors: list[str]  # Any errors encountered


class TaskTreeBuilder:
    """Builds task trees from JSON structures.

    Creates tasks with parent-child relationships and wires dependencies
    between siblings based on `depends_on` references.

    Example tree:
        {
            "title": "Epic Title",
            "task_type": "epic",
            "children": [
                {
                    "title": "Phase 1",
                    "children": [
                        {"title": "Task A", "category": "code"},
                        {"title": "Task B", "category": "code", "depends_on": ["Task A"]}
                    ]
                }
            ]
        }
    """

    def __init__(
        self,
        task_manager: LocalTaskManager,
        project_id: str,
        session_id: str | None = None,
    ) -> None:
        """Initialize the builder.

        Args:
            task_manager: LocalTaskManager instance for creating tasks
            project_id: Project ID for created tasks
            session_id: Optional session ID for tracking
        """
        self.task_manager = task_manager
        self.project_id = project_id
        self.session_id = session_id
        self._title_to_id: dict[str, str] = {}  # Map title -> task_id for dependency resolution
        self._sibling_index_map: dict[
            str | None, dict[int, str]
        ] = {}  # parent_id -> {sibling_index -> task_id}
        self._created_tasks: list[str] = []
        self._errors: list[str] = []

    def build(self, tree: dict[str, Any]) -> TreeBuildResult:
        """Build task tree from JSON structure.

        Args:
            tree: JSON tree with title, task_type, children, etc.

        Returns:
            TreeBuildResult with created task refs
        """
        self._title_to_id = {}
        self._sibling_index_map = {}
        self._created_tasks = []
        self._errors = []

        # Create the root task
        root_id = self._create_node(tree, parent_task_id=None, sibling_index=0)

        # Wire dependencies after all tasks are created
        self._wire_dependencies(tree)

        # Get short refs for all created tasks
        task_refs = []
        epic_ref = None
        for task_id in self._created_tasks:
            task = self.task_manager.get_task(task_id)
            if task and task.seq_num:
                ref = f"#{task.seq_num}"
                task_refs.append(ref)
                if task_id == root_id:
                    epic_ref = ref

        return TreeBuildResult(
            tasks_created=len(self._created_tasks),
            epic_ref=epic_ref,
            task_refs=task_refs,
            errors=self._errors,
        )

    def get_id_for_title(self, title: str) -> str | None:
        """Get the task ID for a given title.

        Args:
            title: The task title to look up

        Returns:
            The task ID if found, None otherwise
        """
        return self._title_to_id.get(title)

    def _create_node(
        self,
        node: dict[str, Any],
        parent_task_id: str | None,
        sibling_index: int = 0,
    ) -> str | None:
        """Create a task node and its children recursively.

        Args:
            node: Task node dict with title, children, etc.
            parent_task_id: ID of parent task (None for root)
            sibling_index: Index of this node among its siblings (for numeric dependency refs)

        Returns:
            Created task ID, or None if creation failed
        """
        title = node.get("title")
        if not title:
            self._errors.append("Node missing required 'title' field")
            return None

        # Extract task fields
        task_type = node.get("task_type", "task")
        description = node.get("description")
        priority = node.get("priority", 2)
        category = node.get("category")
        labels = node.get("labels", [])
        validation_criteria = node.get("validation_criteria")
        requires_user_review = node.get("requires_user_review", False)

        try:
            # Create the task
            task = self.task_manager.create_task(
                title=title,
                project_id=self.project_id,
                task_type=task_type,
                parent_task_id=parent_task_id,
                description=description,
                priority=priority,
                category=category,
                labels=labels,
                validation_criteria=validation_criteria,
                requires_user_review=requires_user_review,
                created_in_session_id=self.session_id,
            )

            self._created_tasks.append(task.id)

            # Store affected files if provided
            affected_files = node.get("affected_files", [])
            if affected_files:
                from gobby.storage.task_affected_files import TaskAffectedFileManager

                af_manager = TaskAffectedFileManager(self.task_manager.db)
                af_manager.set_files(task.id, affected_files, source="expansion")

            # Store parallel_group as a label with parallel: prefix
            parallel_group = node.get("parallel_group")
            if parallel_group:
                try:
                    self.task_manager.add_label(task.id, f"parallel:{parallel_group}")
                except Exception as e:
                    self._errors.append(f"Failed to add parallel label to task {task.id}: {e}")

            # Check for duplicate titles (warn but continue for partial functionality)
            if title in self._title_to_id:
                existing_id = self._title_to_id[title]
                self._errors.append(
                    f"Duplicate task title '{title}': conflicts with existing task {existing_id}"
                )
            self._title_to_id[title] = task.id

            # Track sibling index for numeric dependency references
            if parent_task_id not in self._sibling_index_map:
                self._sibling_index_map[parent_task_id] = {}
            self._sibling_index_map[parent_task_id][sibling_index] = task.id

            logger.debug(f"Created task {task.id} (#{task.seq_num}): {title}")

            # Create children with their sibling indices
            children = node.get("children", [])
            for i, child in enumerate(children):
                self._create_node(child, parent_task_id=task.id, sibling_index=i)

            return task.id

        except Exception as e:
            self._errors.append(f"Failed to create task '{title}': {e}")
            return None

    def _wire_dependencies(self, tree: dict[str, Any]) -> None:
        """Wire dependencies after all tasks are created.

        Resolves `depends_on` references to task IDs. Supports:
        - Title strings: `"depends_on": ["Task A"]` → lookup in _title_to_id
        - Numeric indices: `"depends_on": [0]` → lookup sibling by index

        Args:
            tree: The original tree structure
        """
        from gobby.storage.task_dependencies import TaskDependencyManager

        dep_manager = TaskDependencyManager(self.task_manager.db)

        def process_node(node: dict[str, Any], parent_task_id: str | None) -> None:
            title = node.get("title")
            depends_on = node.get("depends_on", [])

            if title and depends_on and title in self._title_to_id:
                task_id = self._title_to_id[title]

                for dep in depends_on:
                    blocker_id: str | None = None
                    dep_display: str = str(dep)  # For error messages

                    if isinstance(dep, int):
                        # Numeric index - look up sibling by index
                        sibling_map = self._sibling_index_map.get(parent_task_id, {})
                        blocker_id = sibling_map.get(dep)
                        if blocker_id is None:
                            self._errors.append(f"Sibling index {dep} not found for task '{title}'")
                            continue
                    elif isinstance(dep, str):
                        # Title string - look up by title
                        blocker_id = self._title_to_id.get(dep)
                        if blocker_id is None:
                            self._errors.append(f"Dependency not found: '{dep}' for task '{title}'")
                            continue
                    else:
                        self._errors.append(
                            f"Invalid dependency type {type(dep).__name__} for task '{title}'"
                        )
                        continue

                    try:
                        dep_manager.add_dependency(
                            task_id=task_id,
                            depends_on=blocker_id,
                            dep_type="blocks",
                        )
                        logger.debug(f"Added dependency: {title} depends on {dep_display}")
                    except ValueError as e:
                        # Ignore duplicate dependency errors
                        if "already exists" not in str(e):
                            self._errors.append(
                                f"Failed to add dependency {title} -> {dep_display}: {e}"
                            )

            # Get this node's task_id to pass as parent for children
            node_task_id = self._title_to_id.get(title) if title else None

            # Process children
            for child in node.get("children", []):
                process_node(child, parent_task_id=node_task_id)

        process_node(tree, parent_task_id=None)
