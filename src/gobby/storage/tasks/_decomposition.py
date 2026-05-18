"""Task decomposition convenience methods for LocalTaskManager."""

from __future__ import annotations

from typing import Any


class TaskDecompositionMixin:
    """Convenience result-shaping helpers used by task decomposition tools."""

    def create_task_with_decomposition(
        self: Any,
        project_id: str,
        title: str,
        description: str | None = None,
        parent_task_id: str | None = None,
        created_in_session_id: str | None = None,
        priority: int = 2,
        task_type: str = "task",
        assignee: str | None = None,
        labels: list[str] | None = None,
        category: str | None = None,
        validation_criteria: str | None = None,
        assigned_agent: str | None = None,
        additional_skills: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create a child task and return the decomposition tool payload.

        Args:
            project_id: Project that owns the new task.
            title: Human-readable task title.
            description: Optional task description.
            parent_task_id: Optional parent task UUID for decomposition children.
            created_in_session_id: Session that requested the child task.
            priority: Task priority, where 1 is highest.
            task_type: Task type such as ``task``, ``bug``, or ``feature``.
            assignee: Optional assignee identifier.
            labels: Optional task labels.
            category: Optional validation category.
            validation_criteria: Observable acceptance criteria for code tasks.
            assigned_agent: Optional agent assignment.
            additional_skills: Optional skills requested for the task.
            **kwargs: Unexpected task metadata fields.

        Returns:
            Dict containing the created task projection under ``task``.

        Raises:
            ValueError: If task metadata or field names are invalid.
            TaskIDCollisionError: If ID generation cannot find a free ID.

        Example:
            ``manager.create_task_with_decomposition(project_id, "Write tests")``
            returns ``{"task": created_task.to_dict()}``.
        """
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise ValueError(f"Unexpected decomposition task fields: {unexpected}")

        task = self.create_task(
            project_id=project_id,
            title=title,
            description=description,
            parent_task_id=parent_task_id,
            created_in_session_id=created_in_session_id,
            priority=priority,
            task_type=task_type,
            assignee=assignee,
            labels=labels,
            category=category,
            validation_criteria=validation_criteria,
            assigned_agent=assigned_agent,
            additional_skills=additional_skills,
        )
        return {"task": task.to_dict()}

    def update_task_with_result(
        self: Any,
        task_id: str,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Update a task description and return the decomposition tool payload.

        Args:
            task_id: Task ID or resolvable task reference.
            description: Replacement task description.

        Returns:
            Dict containing the updated task projection under ``task``.
        """
        updated = self.update_task(task_id, description=description)
        return {"task": updated.to_dict()}
