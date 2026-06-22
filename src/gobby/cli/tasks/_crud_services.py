"""Dependency bundle for task CRUD command implementations."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

CrudCallable = Callable[..., Any]


@dataclass(frozen=True)
class CrudServices:
    get_task_manager: CrudCallable
    resolve_project_ref: CrudCallable
    filter_tasks_by_stage: CrudCallable
    collect_ancestors: CrudCallable
    sort_tasks_for_tree: CrudCallable
    compute_tree_prefixes: CrudCallable
    get_claimed_task_owners: CrudCallable
    format_task_list: CrudCallable
    parse_task_refs: CrudCallable
    resolve_task_id: CrudCallable
    get_project_context: CrudCallable
    validate_task_isolation_artifacts: CrudCallable
