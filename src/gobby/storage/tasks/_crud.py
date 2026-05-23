"""Compatibility facade for task storage CRUD helpers.

Implementation lives in focused modules; this file preserves direct imports
from ``gobby.storage.tasks._crud`` while new code imports the focused modules.
"""

from gobby.storage.tasks._automation import (
    _is_unattended,
    is_blocked_by_deps,
    list_automation_candidates,
    sweep_stale_claims,
)
from gobby.storage.tasks._build_cascade import cascade_build_state_to_subtree
from gobby.storage.tasks._creation import create_task
from gobby.storage.tasks._ownership import _derive_claimed_by_session_id, _session_exists
from gobby.storage.tasks._read import find_task_by_prefix, find_tasks_by_prefix, get_task
from gobby.storage.tasks._updates import update_task, update_task_metadata

__all__ = [
    "_derive_claimed_by_session_id",
    "_is_unattended",
    "_session_exists",
    "cascade_build_state_to_subtree",
    "create_task",
    "find_task_by_prefix",
    "find_tasks_by_prefix",
    "get_task",
    "is_blocked_by_deps",
    "list_automation_candidates",
    "sweep_stale_claims",
    "update_task",
    "update_task_metadata",
]
