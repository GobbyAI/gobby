"""Shared utilities for task CLI commands.

Originally a single ``_utils.py`` module, this package keeps the public
import surface stable (callers continue to ``from gobby.cli.tasks._utils
import X``) while splitting implementation across cohesive submodules.
"""

from .cascade import cascade_progress
from .claims import get_claimed_task_ids
from .config import check_tasks_enabled, get_sync_manager, get_task_manager
from .listing import format_task_list
from .rendering import format_task_header, format_task_row, pad_to_width
from .resolution import parse_task_refs, resolve_task_id
from .tree import (
    collect_ancestors,
    compute_tree_prefixes,
    get_all_descendants,
    sort_tasks_for_tree,
)

__all__ = [
    "cascade_progress",
    "check_tasks_enabled",
    "collect_ancestors",
    "compute_tree_prefixes",
    "format_task_header",
    "format_task_list",
    "format_task_row",
    "get_all_descendants",
    "get_claimed_task_ids",
    "get_sync_manager",
    "get_task_manager",
    "pad_to_width",
    "parse_task_refs",
    "resolve_task_id",
    "sort_tasks_for_tree",
]
