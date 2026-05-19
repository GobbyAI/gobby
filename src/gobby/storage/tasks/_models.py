"""Task models, exceptions, and constants.

This module contains:
- Task dataclass with serialization methods
- Task-related exceptions
- Priority and category constants
- Validation and normalization helpers
"""

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from gobby.storage.hub.protocol import Row
from gobby.tasks.categories import TDD_ELIGIBLE_CATEGORIES as TDD_ELIGIBLE_CATEGORIES
from gobby.tasks.state_semantics import serialize_task_state

# Priority name to numeric value mapping
PRIORITY_MAP = {"backlog": 4, "low": 3, "medium": 2, "high": 1, "critical": 0}

# Valid task categories (enum-like constraint)
VALID_CATEGORIES: frozenset[str] = frozenset(
    {
        "code",  # Implementation tasks
        "config",  # Configuration file changes
        "docs",  # Documentation tasks
        "refactor",  # Refactor tasks, including updating tests (emitted by expansion)
        "test",  # Test infrastructure tasks (fixtures, helpers)
        "research",  # Investigation/exploration tasks
        "planning",  # Design/architecture tasks
        "manual",  # Manual functional testing (observe output)
    }
)

# Valid task types exposed across storage, CLI, HTTP, and MCP creation surfaces.
TASK_TYPE_CHOICES: tuple[str, ...] = (
    "task",
    "bug",
    "feature",
    "epic",
    "chore",
    "refactor",
    "simple_fix",
    "research_spike",
    "architecture_doc",
    "prd_doc",
    "review_anchor",
)
VALID_TASK_TYPES: frozenset[str] = frozenset(TASK_TYPE_CHOICES)
TASK_TYPE_ALIASES: dict[str, str] = {
    "docs": "chore",
    "fix": "simple_fix",
    "nit": "simple_fix",
    "performance": "task",
    "research": "research_spike",
    "test": "task",
}


def validate_task_type(task_type: str | None) -> str:
    """Validate and normalize a task type value."""
    if task_type is None:
        raise ValueError("task_type is required")
    if not isinstance(task_type, str):
        raise ValueError("task_type must be a string")
    raw = task_type.lower().strip()
    normalized = TASK_TYPE_ALIASES.get(raw, raw)
    if normalized not in VALID_TASK_TYPES:
        allowed = ", ".join(TASK_TYPE_CHOICES)
        raise ValueError(f"Invalid task_type '{task_type}'. Expected one of: {allowed}.")
    return normalized


def task_type_filter_values(task_type: str) -> tuple[str, ...]:
    """Return canonical and legacy storage values for a task type filter."""
    canonical = validate_task_type(task_type)
    aliases = tuple(alias for alias, target in TASK_TYPE_ALIASES.items() if target == canonical)
    return (canonical, *aliases)


class Isolation(StrEnum):
    none = "none"
    worktree = "worktree"
    clone = "clone"


class UnsetType:
    """Sentinel type for optional parameters that were not provided."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET = UnsetType()
type MaybeUnset[T] = T | UnsetType


class TaskAlreadyEscalatedError(ValueError):
    """Raised when an escalation is requested for an already escalated task."""

    def __init__(self, task_id: str, reason: str | None) -> None:
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"Cannot escalate task {task_id}: task is already escalated.")


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


class SeqNumCollisionError(Exception):
    """Raised when a unique seq_num cannot be allocated."""

    pass


class TaskNotFoundError(Exception):
    """Raised when a task reference cannot be resolved to an existing task."""

    pass


class TaskClosedError(ValueError):
    """Raised when a task operation is blocked by a closed task."""

    pass


class TaskAlreadyClaimedError(ValueError):
    """Raised when a task is already claimed by another session."""

    def __init__(self, task_id: str, claimed_by: str) -> None:
        self.task_id = task_id
        self.claimed_by = claimed_by
        super().__init__(f"Task {task_id} is already claimed by session '{claimed_by}'")


class TaskHasChildrenError(ValueError):
    """Raised when deleting a task that has children without cascade."""

    pass


class TaskHasDependentsError(ValueError):
    """Raised when deleting a task that has dependents without cascade/unlink."""

    pass


@dataclass
class Task:
    id: str
    project_id: str
    title: str
    priority: int
    # task, bug, feature, epic, chore, refactor, simple_fix, research_spike,
    # architecture_doc, prd_doc, review_anchor
    task_type: str
    created_at: str
    updated_at: str
    # Optional fields
    description: str | None = None
    parent_task_id: str | None = None
    created_in_session_id: str | None = None
    claimed_by_session_id: str | None = None
    closed_in_session_id: str | None = None
    closed_commit_sha: str | None = None
    closed_at: str | None = None
    assignee: str | None = None
    labels: list[str] | None = None
    closed_reason: str | None = None
    validation_status: Literal["pending", "valid", "invalid"] | None = None
    validation_feedback: str | None = None
    category: str | None = None
    validation_criteria: str | None = None
    validation_fail_count: int = 0
    dispatch_failure_count: int = 0
    validation_override_reason: str | None = None  # Why agent bypassed validation
    # Commit linking
    commits: list[str] | None = None
    # Escalation fields
    escalated_at: str | None = None
    escalation_reason: str | None = None
    is_escalated: bool = False
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
    # Scheduling fields (Gantt chart)
    start_date: str | None = None
    due_date: str | None = None
    # Automation dispatch fields
    allow_automation: bool = False
    unattended: bool = False
    isolation: Isolation = Isolation.worktree
    assigned_agent: str | None = None
    additional_skills: list[str] | None = None
    # Dependency fields (populated on demand, not stored in tasks table)
    blocked_by: set[str] = field(default_factory=set)
    active_blocked_by: set[str] = field(default_factory=set)
    # Stage manifest rows (populated on demand, not stored in tasks table)
    stages: tuple[Any, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Normalize enum-backed fields for manually constructed tasks."""
        self.task_type = validate_task_type(self.task_type)
        self.isolation = Isolation(self.isolation)
        if self.escalated_at and not self.closed_at:
            self.is_escalated = True

    @classmethod
    def from_row(cls, row: Row) -> "Task":
        """Convert database row to Task object."""
        labels_json = row["labels"]
        labels = json.loads(labels_json) if labels_json else []

        # Handle optional columns that might not exist yet if migration pending
        keys = row.keys()
        closed_at = row["closed_at"] if "closed_at" in keys else None
        escalated_at = row["escalated_at"] if "escalated_at" in keys else None
        is_escalated = bool(row["is_escalated"]) if "is_escalated" in keys else bool(escalated_at)
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
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
            claimed_by_session_id=(
                row["claimed_by_session_id"] if "claimed_by_session_id" in keys else None
            ),
            closed_in_session_id=(
                row["closed_in_session_id"] if "closed_in_session_id" in keys else None
            ),
            closed_commit_sha=row["closed_commit_sha"] if "closed_commit_sha" in keys else None,
            closed_at=closed_at,
            assignee=row["assignee"],
            labels=labels,
            closed_reason=row["closed_reason"],
            validation_status=row["validation_status"] if "validation_status" in keys else None,
            validation_feedback=(
                row["validation_feedback"] if "validation_feedback" in keys else None
            ),
            category=row["category"] if "category" in keys else None,
            validation_criteria=(
                row["validation_criteria"] if "validation_criteria" in keys else None
            ),
            validation_fail_count=(
                row["validation_fail_count"] if "validation_fail_count" in keys else 0
            ),
            dispatch_failure_count=(
                row["dispatch_failure_count"] if "dispatch_failure_count" in keys else 0
            ),
            validation_override_reason=(
                row["validation_override_reason"] if "validation_override_reason" in keys else None
            ),
            commits=json.loads(row["commits"]) if "commits" in keys and row["commits"] else None,
            escalated_at=escalated_at,
            escalation_reason=row["escalation_reason"] if "escalation_reason" in keys else None,
            is_escalated=is_escalated,
            github_issue_number=(
                row["github_issue_number"] if "github_issue_number" in keys else None
            ),
            github_pr_number=row["github_pr_number"] if "github_pr_number" in keys else None,
            github_repo=row["github_repo"] if "github_repo" in keys else None,
            linear_issue_id=row["linear_issue_id"] if "linear_issue_id" in keys else None,
            linear_team_id=row["linear_team_id"] if "linear_team_id" in keys else None,
            seq_num=row["seq_num"] if "seq_num" in keys else None,
            path_cache=row["path_cache"] if "path_cache" in keys else None,
            start_date=row["start_date"] if "start_date" in keys else None,
            due_date=row["due_date"] if "due_date" in keys else None,
            allow_automation=bool(row["allow_automation"]) if "allow_automation" in keys else False,
            unattended=bool(row["unattended"]) if "unattended" in keys else False,
            isolation=(
                Isolation(row["isolation"])
                if "isolation" in keys and row["isolation"] is not None
                else Isolation.worktree
            ),
            assigned_agent=row["assigned_agent"] if "assigned_agent" in keys else None,
            additional_skills=(
                json.loads(row["additional_skills"])
                if "additional_skills" in keys and row["additional_skills"]
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert Task to dictionary."""
        state = serialize_task_state(self)
        return {
            "ref": f"#{self.seq_num}" if self.seq_num else self.id[:8],
            "project_id": self.project_id,
            "title": self.title,
            "state": state,
            "priority": self.priority,
            "task_type": self.task_type,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "description": self.description,
            "parent_task_id": self.parent_task_id,
            "created_in_session_id": self.created_in_session_id,
            "claimed_by_session_id": self.claimed_by_session_id,
            "closed_in_session_id": self.closed_in_session_id,
            "closed_commit_sha": self.closed_commit_sha,
            "closed_at": self.closed_at,
            "assignee": self.assignee,
            "labels": self.labels,
            "closed_reason": self.closed_reason,
            "validation_status": self.validation_status,
            "validation_feedback": self.validation_feedback,
            "category": self.category,
            "validation_criteria": self.validation_criteria,
            "validation_fail_count": self.validation_fail_count,
            "dispatch_failure_count": self.dispatch_failure_count,
            "validation_override_reason": self.validation_override_reason,
            "commits": self.commits,
            "escalated_at": self.escalated_at,
            "escalation_reason": self.escalation_reason,
            "is_escalated": self.is_escalated,
            "github_issue_number": self.github_issue_number,
            "github_pr_number": self.github_pr_number,
            "github_repo": self.github_repo,
            "linear_issue_id": self.linear_issue_id,
            "linear_team_id": self.linear_team_id,
            "seq_num": self.seq_num,
            "path_cache": self.path_cache,
            "start_date": self.start_date,
            "due_date": self.due_date,
            "allow_automation": self.allow_automation,
            "unattended": self.unattended,
            "isolation": self.isolation,
            "assigned_agent": self.assigned_agent,
            "additional_skills": self.additional_skills,
            "id": self.id,  # UUID at end for backwards compat
        }

    def to_brief(self) -> dict[str, Any]:
        """Convert Task to brief discovery format.

        Returns only essential fields needed for task discovery.
        Use get_task(brief=False) for full task details.

        This follows the progressive discovery pattern used for MCP tools:
        - list_tasks() returns brief format (~22 fields)
        - get_task() returns brief format by default, full with brief=False (~35 fields)
        """
        state = serialize_task_state(self)
        return {
            "ref": f"#{self.seq_num}" if self.seq_num else self.id[:8],
            "title": self.title,
            "state": state,
            "priority": self.priority,
            "task_type": self.task_type,
            "parent_task_id": self.parent_task_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "seq_num": self.seq_num,
            "path_cache": self.path_cache,
            "assignee": self.assignee,
            "claimed_by_session_id": self.claimed_by_session_id,
            "category": self.category,
            "closed_at": self.closed_at,
            "closed_in_session_id": self.closed_in_session_id,
            "validation_fail_count": self.validation_fail_count,
            "dispatch_failure_count": self.dispatch_failure_count,
            "escalated_at": self.escalated_at,
            "is_escalated": self.is_escalated,
            "start_date": self.start_date,
            "due_date": self.due_date,
            "github_issue_number": self.github_issue_number,
            "github_repo": self.github_repo,
            "github_pr_number": self.github_pr_number,
            "allow_automation": self.allow_automation,
            "unattended": self.unattended,
            "isolation": self.isolation,
            "assigned_agent": self.assigned_agent,
            "additional_skills": self.additional_skills,
            "id": self.id,  # UUID at end for backwards compat
        }
