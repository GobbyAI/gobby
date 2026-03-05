"""Task models, exceptions, and constants.

This module contains:
- Task dataclass with serialization methods
- Task-related exceptions
- Priority and category constants
- Validation and normalization helpers
"""

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal

# Priority name to numeric value mapping
PRIORITY_MAP = {"backlog": 4, "low": 3, "medium": 2, "high": 1, "critical": 0}

# Valid task categories (enum-like constraint)
VALID_CATEGORIES: frozenset[str] = frozenset(
    {
        "code",  # Implementation tasks
        "config",  # Configuration file changes
        "docs",  # Documentation tasks
        "test",  # Test infrastructure tasks (fixtures, helpers)
        "refactor",  # Refactoring tasks (including updating existing tests)
        "research",  # Investigation/exploration tasks
        "planning",  # Design/architecture tasks
        "manual",  # Manual functional testing (observe output)
    }
)

# Sentinel for unset optional parameters
UNSET: Any = object()


def validate_category(category: str | None) -> str | None:
    """Validate and normalize a category value.

    Args:
        category: Category string to validate (case-insensitive)

    Returns:
        Normalized lowercase category if valid, None otherwise
    """
    if category is None:
        return None
    normalized = category.lower().strip()
    return normalized if normalized in VALID_CATEGORIES else None


def normalize_priority(priority: int | str | None) -> int:
    """Convert priority to numeric value for sorting."""
    if priority is None:
        return 999
    if isinstance(priority, str):
        # Check if it's a named priority
        if priority.lower() in PRIORITY_MAP:
            return PRIORITY_MAP[priority.lower()]
        # Try to parse as int
        try:
            return int(priority)
        except ValueError:
            return 999
    return int(priority)


class TaskIDCollisionError(Exception):
    """Raised when a unique task ID cannot be generated."""

    pass


class TaskNotFoundError(Exception):
    """Raised when a task reference cannot be resolved to an existing task."""

    pass


@dataclass
class Task:
    id: str
    project_id: str
    title: str
    status: Literal[
        "open",
        "in_progress",
        "needs_review",
        "review_approved",
        "closed",
        "escalated",
    ]
    priority: int
    task_type: str  # bug, feature, task, epic, chore
    created_at: str
    updated_at: str
    # Optional fields
    description: str | None = None
    parent_task_id: str | None = None
    created_in_session_id: str | None = None
    closed_in_session_id: str | None = None
    closed_commit_sha: str | None = None
    closed_at: str | None = None
    assignee: str | None = None
    labels: list[str] | None = None
    closed_reason: str | None = None
    validation_status: Literal["pending", "valid", "invalid"] | None = None
    validation_feedback: str | None = None
    category: str | None = None
    complexity_score: int | None = None
    estimated_subtasks: int | None = None
    expansion_context: str | None = None
    validation_criteria: str | None = None
    use_external_validator: bool = False
    validation_fail_count: int = 0
    validation_override_reason: str | None = None  # Why agent bypassed validation
    # Workflow integration fields
    workflow_name: str | None = None
    verification: str | None = None
    sequence_order: int | None = None
    # Commit linking
    commits: list[str] | None = None
    # Escalation fields
    escalated_at: str | None = None
    escalation_reason: str | None = None
    # GitHub integration fields
    github_issue_number: int | None = None
    github_pr_number: int | None = None
    github_repo: str | None = None
    # Linear integration fields
    linear_issue_id: str | None = None
    linear_team_id: str | None = None
    # Human-friendly ID fields (task renumbering)
    seq_num: int | None = None
    path_cache: str | None = None
    # Agent configuration
    agent_name: str | None = None  # Subagent config file to use for this task
    # Spec traceability
    reference_doc: str | None = None  # Path to source specification document
    # Processing flags for idempotent operations
    is_expanded: bool = False  # Subtasks have been created
    # Skill-based expansion status (for new /gobby-expand flow)
    expansion_status: Literal["none", "pending", "completed"] = "none"
    # Review status fields (HITL support)
    requires_user_review: bool = False  # Task requires user sign-off before closing
    accepted_by_user: bool = False  # Set True when user moves review → closed
    # Scheduling fields (Gantt chart)
    start_date: str | None = None
    due_date: str | None = None
    # Dependency fields (populated on demand, not stored in tasks table)
    blocked_by: set[str] = field(default_factory=set)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        """Convert database row to Task object."""
        labels_json = row["labels"]
        labels = json.loads(labels_json) if labels_json else []

        # Handle optional columns that might not exist yet if migration pending
        keys = row.keys()

        return cls(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            status=row["status"],
            priority=normalize_priority(row["priority"]),
            task_type=row["task_type"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            description=row["description"],
            parent_task_id=row["parent_task_id"],
            created_in_session_id=(
                row["created_in_session_id"]
                if "created_in_session_id" in keys
                else (
                    row["discovered_in_session_id"] if "discovered_in_session_id" in keys else None
                )
            ),
            closed_in_session_id=(
                row["closed_in_session_id"] if "closed_in_session_id" in keys else None
            ),
            closed_commit_sha=row["closed_commit_sha"] if "closed_commit_sha" in keys else None,
            closed_at=row["closed_at"] if "closed_at" in keys else None,
            assignee=row["assignee"],
            labels=labels,
            closed_reason=row["closed_reason"],
            validation_status=row["validation_status"] if "validation_status" in keys else None,
            validation_feedback=(
                row["validation_feedback"] if "validation_feedback" in keys else None
            ),
            category=row["category"] if "category" in keys else None,
            complexity_score=row["complexity_score"] if "complexity_score" in keys else None,
            estimated_subtasks=row["estimated_subtasks"] if "estimated_subtasks" in keys else None,
            expansion_context=row["expansion_context"] if "expansion_context" in keys else None,
            validation_criteria=(
                row["validation_criteria"] if "validation_criteria" in keys else None
            ),
            use_external_validator=(
                bool(row["use_external_validator"]) if "use_external_validator" in keys else False
            ),
            validation_fail_count=(
                row["validation_fail_count"] if "validation_fail_count" in keys else 0
            ),
            validation_override_reason=(
                row["validation_override_reason"] if "validation_override_reason" in keys else None
            ),
            workflow_name=row["workflow_name"] if "workflow_name" in keys else None,
            verification=row["verification"] if "verification" in keys else None,
            sequence_order=row["sequence_order"] if "sequence_order" in keys else None,
            commits=json.loads(row["commits"]) if "commits" in keys and row["commits"] else None,
            escalated_at=row["escalated_at"] if "escalated_at" in keys else None,
            escalation_reason=row["escalation_reason"] if "escalation_reason" in keys else None,
            github_issue_number=(
                row["github_issue_number"] if "github_issue_number" in keys else None
            ),
            github_pr_number=row["github_pr_number"] if "github_pr_number" in keys else None,
            github_repo=row["github_repo"] if "github_repo" in keys else None,
            linear_issue_id=row["linear_issue_id"] if "linear_issue_id" in keys else None,
            linear_team_id=row["linear_team_id"] if "linear_team_id" in keys else None,
            seq_num=row["seq_num"] if "seq_num" in keys else None,
            path_cache=row["path_cache"] if "path_cache" in keys else None,
            agent_name=row["agent_name"] if "agent_name" in keys else None,
            reference_doc=row["reference_doc"] if "reference_doc" in keys else None,
            is_expanded=bool(row["is_expanded"]) if "is_expanded" in keys else False,
            expansion_status=(
                row["expansion_status"]
                if "expansion_status" in keys and row["expansion_status"]
                else "none"
            ),
            requires_user_review=(
                bool(row["requires_user_review"]) if "requires_user_review" in keys else False
            ),
            accepted_by_user=(
                bool(row["accepted_by_user"]) if "accepted_by_user" in keys else False
            ),
            start_date=row["start_date"] if "start_date" in keys else None,
            due_date=row["due_date"] if "due_date" in keys else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert Task to dictionary."""
        return {
            "ref": f"#{self.seq_num}" if self.seq_num else self.id[:8],
            "project_id": self.project_id,
            "title": self.title,
            "status": self.status,
            "priority": self.priority,
            "type": self.task_type,  # Use 'type' for API compatibility
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "description": self.description,
            "parent_task_id": self.parent_task_id,
            "created_in_session_id": self.created_in_session_id,
            "closed_in_session_id": self.closed_in_session_id,
            "closed_commit_sha": self.closed_commit_sha,
            "closed_at": self.closed_at,
            "assignee": self.assignee,
            "labels": self.labels,
            "closed_reason": self.closed_reason,
            "validation_status": self.validation_status,
            "validation_feedback": self.validation_feedback,
            "category": self.category,
            "complexity_score": self.complexity_score,
            "estimated_subtasks": self.estimated_subtasks,
            "expansion_context": self.expansion_context,
            "validation_criteria": self.validation_criteria,
            "use_external_validator": self.use_external_validator,
            "validation_fail_count": self.validation_fail_count,
            "validation_override_reason": self.validation_override_reason,
            "workflow_name": self.workflow_name,
            "verification": self.verification,
            "sequence_order": self.sequence_order,
            "commits": self.commits,
            "escalated_at": self.escalated_at,
            "escalation_reason": self.escalation_reason,
            "github_issue_number": self.github_issue_number,
            "github_pr_number": self.github_pr_number,
            "github_repo": self.github_repo,
            "linear_issue_id": self.linear_issue_id,
            "linear_team_id": self.linear_team_id,
            "seq_num": self.seq_num,
            "path_cache": self.path_cache,
            "agent_name": self.agent_name,
            "reference_doc": self.reference_doc,
            "is_expanded": self.is_expanded,
            "expansion_status": self.expansion_status,
            "requires_user_review": self.requires_user_review,
            "accepted_by_user": self.accepted_by_user,
            "start_date": self.start_date,
            "due_date": self.due_date,
            "id": self.id,  # UUID at end for backwards compat
        }

    def to_brief(self) -> dict[str, Any]:
        """Convert Task to brief discovery format for list operations.

        Returns only essential fields needed for task discovery.
        Use get_task() with to_dict() for full task details.

        This follows the progressive discovery pattern used for MCP tools:
        - list_tasks() returns brief format (20 fields)
        - get_task() returns full format (33 fields)
        """
        return {
            "ref": f"#{self.seq_num}" if self.seq_num else self.id[:8],
            "title": self.title,
            "status": self.status,
            "priority": self.priority,
            "type": self.task_type,
            "parent_task_id": self.parent_task_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "seq_num": self.seq_num,
            "path_cache": self.path_cache,
            "requires_user_review": self.requires_user_review,
            "assignee": self.assignee,
            "agent_name": self.agent_name,
            "category": self.category,
            "closed_at": self.closed_at,
            "closed_in_session_id": self.closed_in_session_id,
            "validation_fail_count": self.validation_fail_count,
            "escalated_at": self.escalated_at,
            "sequence_order": self.sequence_order,
            "start_date": self.start_date,
            "due_date": self.due_date,
            "id": self.id,  # UUID at end for backwards compat
        }
