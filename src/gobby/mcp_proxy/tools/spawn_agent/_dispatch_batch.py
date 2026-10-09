"""Batch spawning through the public spawn closure and live daemon cap."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal

from gobby.mcp_proxy.tools.internal import InternalToolRegistry

from ._request import first_string, non_empty_string
from ._spawn_guards import configured_max_active_agents

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)


def _coalesce_string(mapping: dict[str, Any], key: str, fallback: str | None) -> str | None:
    return non_empty_string(mapping.get(key)) or fallback


def _coalesce_bool(mapping: dict[str, Any], key: str, fallback: bool | None) -> bool | None:
    value = mapping.get(key)
    if isinstance(value, bool):
        return value
    return fallback


def _coalesce_number(mapping: dict[str, Any], key: str, fallback: float | None) -> float | None:
    value = mapping.get(key)
    if isinstance(value, int | float):
        return float(value)
    return fallback


def _suggestion_task_description(
    task_manager: LocalTaskManager | None,
    task_id: str | None,
) -> str:
    if not task_manager or not task_id:
        return ""
    try:
        full_task = task_manager.get_task(task_id)
    except Exception:
        logger.debug("Failed to load dispatch suggestion task %s", task_id, exc_info=True)
        return ""
    description = getattr(full_task, "description", "") if full_task else ""
    return description if isinstance(description, str) else ""


def register_dispatch_batch(
    registry: InternalToolRegistry,
    *,
    spawn_agent: Callable[..., Awaitable[dict[str, Any]]],
    task_manager: LocalTaskManager | None,
) -> None:
    """Register batch dispatch with the factory's spawn implementation."""

    @registry.tool(
        name="dispatch_batch",
        description=(
            "Dispatch multiple agents in parallel for non-conflicting tasks. "
            "Takes task briefs from suggest_next_task and spawns an agent for each. "
            "Bounds concurrent spawning by max_active_agents."
        ),
    )
    async def dispatch_batch(
        suggestions: list[dict[str, Any]],
        agent: str = "developer",
        worktree_id: str | None = None,
        clone_id: str | None = None,
        checkout_mode: Literal["none", "worktree", "clone"] | None = None,
        branch_name: str | None = None,
        base_branch: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        reasoning_required: bool | None = None,
        parent_session_id: str | None = None,
        timeout: float | None = None,
        notify_parent_on_completion: bool = True,
        extra_write_paths: list[str] | None = None,
        write_paths_reason: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch multiple agents for non-conflicting tasks.

        Args:
            suggestions: Task briefs from suggest_next_task output
            agent: Agent definition name (default: "developer")
            worktree_id: Shared worktree for all agents (full UUID or unique id prefix)
            clone_id: Existing clone ID for all agents
            checkout_mode: Checkout mode (none/worktree/clone)
            branch_name: Git branch name for isolation
            base_branch: Base branch for worktree/clone
            provider: AI provider override
            model: Model override
            parent_session_id: Parent session reference
            timeout: Timeout in seconds for each agent
            notify_parent_on_completion: Whether to notify parent sessions on completion

        Returns:
            Dict with dispatched count and per-task results
        """
        if not suggestions:
            return {"dispatched": 0, "results": []}
        batch_cap = configured_max_active_agents()
        semaphore = asyncio.Semaphore(max(1, min(len(suggestions), batch_cap)))

        async def _spawn_one(suggestion: dict[str, Any]) -> dict[str, Any]:
            if not isinstance(suggestion, dict):
                return {
                    "task_ref": "",
                    "run_id": "",
                    "success": False,
                    "error": "dispatch_batch suggestions must be objects",
                }

            task_id = first_string(suggestion, "task_id", "id")
            task_ref = first_string(suggestion, "ref", "task_ref", "task_id", "id")
            if not task_ref:
                return {
                    "task_ref": "",
                    "run_id": "",
                    "success": False,
                    "error": (
                        "dispatch_batch suggestion is missing ref, task_ref, task_id, or id; "
                        "refusing to spawn an unknown task"
                    ),
                }
            if not task_id:
                task_id = task_ref

            task_title = first_string(suggestion, "title", "summary") or ""
            prompt = non_empty_string(suggestion.get("prompt"))
            if prompt is None:
                if not task_title:
                    return {
                        "task_ref": task_ref,
                        "run_id": "",
                        "success": False,
                        "error": (
                            "dispatch_batch suggestion is missing prompt and title; "
                            f"refusing to spawn {task_ref}"
                        ),
                    }
                task_desc = await asyncio.to_thread(
                    _suggestion_task_description, task_manager, task_id
                )
                desc_block = f"\n\nDescription:\n{task_desc}" if task_desc else ""
                prompt = f"Implement task {task_ref}: {task_title}{desc_block}"

            suggestion_agent = _coalesce_string(suggestion, "agent", agent)
            try:
                result = await spawn_agent(
                    extra_write_paths=suggestion.get("extra_write_paths", extra_write_paths),
                    write_paths_reason=suggestion.get("write_paths_reason", write_paths_reason),
                    prompt=prompt,
                    agent=suggestion_agent or "developer",
                    execution_mode="one_shot",
                    task_id=task_id,
                    worktree_id=_coalesce_string(suggestion, "worktree_id", worktree_id),
                    clone_id=_coalesce_string(suggestion, "clone_id", clone_id),
                    checkout_mode=_coalesce_string(suggestion, "checkout_mode", checkout_mode),
                    branch_name=_coalesce_string(suggestion, "branch_name", branch_name),
                    base_branch=_coalesce_string(suggestion, "base_branch", base_branch),
                    provider=_coalesce_string(suggestion, "provider", provider),
                    model=_coalesce_string(suggestion, "model", model),
                    reasoning_effort=_coalesce_string(
                        suggestion, "reasoning_effort", reasoning_effort
                    ),
                    reasoning_required=_coalesce_bool(
                        suggestion, "reasoning_required", reasoning_required
                    ),
                    timeout=_coalesce_number(suggestion, "timeout", timeout),
                    parent_session_id=_coalesce_string(
                        suggestion, "parent_session_id", parent_session_id
                    ),
                    notify_parent_on_completion=_coalesce_bool(
                        suggestion,
                        "notify_parent_on_completion",
                        notify_parent_on_completion,
                    )
                    is not False,
                )
                out: dict[str, Any] = {
                    "task_ref": task_ref,
                    "run_id": result.get("run_id", ""),
                    "success": result.get("success", False),
                    "agent": suggestion_agent or "developer",
                    "external_write_grant": result.get("external_write_grant"),
                }
                if not out["success"] and result.get("error"):
                    out["error"] = result["error"]
                return out
            except Exception as e:
                logger.error("Failed to spawn agent for %s: %s", task_ref, e)
                return {
                    "task_ref": task_ref,
                    "run_id": "",
                    "success": False,
                    "error": str(e),
                }

        async def _spawn_one_bounded(suggestion: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                return await _spawn_one(suggestion)

        results = await asyncio.gather(*[_spawn_one_bounded(s) for s in suggestions])
        dispatched = sum(1 for r in results if r["success"])

        return {
            "dispatched": dispatched,
            "results": list(results),
        }
