"""
Task management CLI commands.

This package contains the task management commands, split into logical modules:
- _utils: Shared utilities (formatting, task resolution)
- ai: AI-powered commands (validate, expand, suggest, complexity)
- crud: CRUD operations (list, create, show, update, close, delete)
- deps: Dependency management subgroup
- hooks: Git hooks management subgroup
- labels: Label management subgroup
- stages/review: Stage-manifest operator commands
- main: Entry point and misc commands (sync, compact, import, doctor, clean)
"""

from gobby.cli.tasks._utils import (
    cascade_progress,
    check_tasks_enabled,
    get_sync_manager,
    get_task_manager,
    parse_task_refs,
)
from gobby.cli.tasks.main import tasks

__all__ = [
    "cascade_progress",
    "check_tasks_enabled",
    "get_task_manager",
    "get_sync_manager",
    "parse_task_refs",
    "tasks",
]
