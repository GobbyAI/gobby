"""Protective task-label condition helper registered in the rule condition namespace."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp

logger = logging.getLogger(__name__)


def task_may_carry_label(
    task_manager: Any | None, task_ref: Any, label: str, project_id: str | None
) -> bool:
    """False only when the task a task-MCP mutation would target provably lacks ``label``.

    ``task_ref`` resolves exactly as the task MCP tools resolve it: project-scoped
    ``#N``, bare ``N`` and dotted paths, and UUIDs directly. A protective rule
    therefore sees the mutation's real target, and any lookup it cannot prove
    fails closed.
    """
    if task_manager is None or not isinstance(task_ref, str) or not task_ref:
        return True
    try:
        task = task_manager.get_task(resolve_task_id_for_mcp(task_manager, task_ref, project_id))
    except Exception:
        logger.debug("task_may_carry_label: unresolved task ref %r", task_ref, exc_info=True)
        return True
    labels = getattr(task, "labels", None)
    return not isinstance(labels, list | tuple | set) or label in labels


def task_condition_helpers(task_manager: Any | None) -> dict[str, Callable[..., Any]]:
    """Bind the protective task helpers to the evaluator's task manager."""
    return {
        "task_may_carry_label": lambda task_ref, label, project_id: task_may_carry_label(
            task_manager, task_ref, label, project_id
        ),
    }
