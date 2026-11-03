"""Condition helper exposing the running calls that fence a task selection."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from gobby.storage.tasks import TaskNotFoundError
from gobby.workflows.task_tool_bindings import pending_fencing_calls

logger = logging.getLogger(__name__)


def _resolve_target(task_manager: Any, project_id: str | None, task_ref: Any) -> str | None:
    """Resolve a claim target to its UUID; None when it cannot be proven."""
    if not isinstance(task_ref, str) or not task_ref:
        return None
    try:
        return str(UUID(task_ref))
    except ValueError:
        pass
    if task_manager is None or not project_id:
        return None
    try:
        return str(task_manager.resolve_task_reference(task_ref, project_id))
    except (TaskNotFoundError, ValueError) as exc:
        logger.debug("selection fence: unresolved task ref %r: %s", task_ref, exc)
        return None


def pending_selection_fence_calls(
    task_manager: Any,
    variables: Mapping[str, Any],
    project_id: str | None,
    task_ref: Any,
) -> list[str]:
    """Return fencing calls that a selection of ``task_ref`` would strand.

    A None ``task_ref`` selects a task that does not exist yet. An unresolvable ref
    fails closed: every bound fencing call counts, as for an unknown target.
    """
    return pending_fencing_calls(variables, _resolve_target(task_manager, project_id, task_ref))
