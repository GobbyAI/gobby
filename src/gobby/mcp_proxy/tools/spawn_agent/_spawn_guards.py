"""Concurrency guards shared by spawn-agent entry points."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.config.build import load_build_config
from gobby.dispatch.constants import DISPATCH_TTL_SECONDS, MAX_ACTIVE_AGENTS
from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.tasks.agentic_close_review import TASK_CLOSE_REVIEWER_AGENT
from gobby.tasks.state_semantics import (
    get_claimed_session_id,
    is_task_actionable,
    is_task_reviewable,
)
from gobby.utils.session_context import get_current_session_id

from ._idempotency import active_task_spawn_response, non_actionable_task_spawn_response
from ._runtime import _normalize_string_list
from ._step_state import (
    preclaimed_task_instruction,
    spawn_starts_after_claim,
    task_coordination_instruction,
)

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager
    from gobby.workflows.definitions import AgentDefinitionBody

logger = logging.getLogger(__name__)


_SLOT_LOCKS: dict[str, asyncio.Lock] = {}


def _nonempty_task_ref(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _claim_step_requires_task(agent_body: AgentDefinitionBody | None) -> bool:
    """A claim-first step workflow cannot start until a task is assigned."""
    workflow = None if agent_body is None else agent_body.step_workflow
    return bool(workflow is not None and workflow.steps and workflow.steps[0].name == "claim")


@dataclass(frozen=True, slots=True)
class SpawnTaskContext:
    """Resolved task metadata and prompt policy for one spawn request."""

    prompt: str
    resolved_task_id: str | None = None
    task_title: str | None = None
    task_seq_num: int | None = None
    task_category: str | None = None
    task_additional_skills: list[str] | None = None
    claimed_session_id: str | None = None
    refusal: dict[str, Any] | None = None


async def resolve_spawn_task_context(
    *,
    prompt: str,
    task_id: str | None,
    task_manager: LocalTaskManager | None,
    project_id: str,
    allow_closed_task: bool,
    agent_body: AgentDefinitionBody | None,
    initial_variables: dict[str, Any] | None,
) -> SpawnTaskContext:
    """Resolve task ownership, admission, metadata, and task-specific prompt policy."""
    resolved_task_id: str | None = None
    task_title: str | None = None
    task_seq_num: int | None = None
    task_category: str | None = None
    task_additional_skills: list[str] | None = None
    claimed_session_id: str | None = None
    resolved_task: Any | None = None
    assigned_task_id = initial_variables.get("assigned_task_id") if initial_variables else None
    if _claim_step_requires_task(agent_body) and not (
        _nonempty_task_ref(task_id) or _nonempty_task_ref(assigned_task_id)
    ):
        return SpawnTaskContext(
            prompt=prompt,
            refusal={
                "success": False,
                "skipped": True,
                "error": (
                    "Task-bound agent requires an assigned task; refusing to spawn "
                    "without task_id or assigned_task_id"
                ),
            },
        )

    if task_id and task_manager:
        try:
            resolved_task_id = await asyncio.to_thread(
                resolve_task_id_for_mcp, task_manager, task_id, project_id
            )
            resolved_task = await asyncio.to_thread(task_manager.get_task, resolved_task_id)
            if resolved_task:
                task_title = resolved_task.title
                task_seq_num = resolved_task.seq_num
                task_category = getattr(resolved_task, "category", None)
                if resolved_task.additional_skills is not None:
                    task_additional_skills = _normalize_string_list(resolved_task.additional_skills)
                claimed_session_id = get_claimed_session_id(resolved_task)
        except Exception as exc:
            # Continuing task-less would leave the child unable to edit while spawn reports
            # success, so reject a task assignment that cannot be resolved (#22402).
            logger.warning("Failed to resolve task_id %s: %s", task_id, exc)
            return SpawnTaskContext(
                prompt=prompt,
                refusal={
                    "success": False,
                    "skipped": True,
                    "task_id": task_id,
                    "error": (
                        f"Task {task_id} could not be resolved; refusing to spawn agent: {exc}"
                    ),
                },
            )

    if resolved_task_id and resolved_task is not None and not is_task_actionable(resolved_task):
        if not (allow_closed_task and is_task_reviewable(resolved_task)):
            return SpawnTaskContext(
                prompt=prompt,
                refusal=non_actionable_task_spawn_response(
                    resolved_task,
                    task_ref=task_id,
                    resolved_task_id=resolved_task_id,
                ),
            )

    task_will_be_owned_by_child = bool(
        resolved_task_id
        and resolved_task is not None
        and is_task_actionable(resolved_task)
        and claimed_session_id is None
    )
    if agent_body is not None and spawn_starts_after_claim(
        agent_body.step_workflow,
        task_owned_by_child=task_will_be_owned_by_child,
    ):
        assert resolved_task_id is not None
        task_ref = f"#{task_seq_num}" if task_seq_num else resolved_task_id
        prompt = f"{prompt}\n\n{preclaimed_task_instruction(task_ref)}"
    initial_task_ref = initial_variables.get("assigned_task_id") if initial_variables else None
    if resolved_task_id is not None or isinstance(initial_task_ref, str):
        prompt = f"{prompt}\n\n{task_coordination_instruction()}"

    return SpawnTaskContext(
        prompt=prompt,
        resolved_task_id=resolved_task_id,
        task_title=task_title,
        task_seq_num=task_seq_num,
        task_category=task_category,
        task_additional_skills=task_additional_skills,
        claimed_session_id=claimed_session_id,
    )


class TaskSpawnLease:
    """Owns the direct-spawn task mutex until a run is attached or spawn fails."""

    def __init__(
        self,
        *,
        db: Any | None,
        task_id: str | None,
        held_mutex: Any | None = None,
    ) -> None:
        self._db = db
        self._task_id = task_id
        self._held_mutex = held_mutex
        self._mutex: Any | None = None
        self._owns_mutex = False
        self._holder = f"spawn-agent:{uuid.uuid4().hex}"

    def acquire(self) -> dict[str, Any] | None:
        if self._held_mutex is not None or self._db is None or self._task_id is None:
            return None

        from gobby.dispatch.mutex import DispatchMutexUnavailableError, RuntimeDispatchMutex

        self._mutex = RuntimeDispatchMutex(
            TaskDispatchMutexManager(self._db),
            task_id=self._task_id,
            holder=self._holder,
            action_kind="spawn_agent",
            ttl_seconds=DISPATCH_TTL_SECONDS,
        )
        try:
            self._mutex.__enter__()
        except DispatchMutexUnavailableError:
            self._mutex = None
            return {
                "success": False,
                "error": f"task {self._task_id} already has an agent spawn in progress",
                "task_id": self._task_id,
            }
        except Exception as exc:
            try:
                self._mutex.__exit__(type(exc), exc, exc.__traceback__)
            finally:
                self._mutex = None
            raise
        self._owns_mutex = True
        return None

    def attach(self, run_id: str) -> str | None:
        if not self._owns_mutex or self._mutex is None:
            return None
        try:
            self._mutex.attach(run_id)
        except Exception as exc:
            logger.warning("Failed to attach direct spawn mutex to run %s", run_id, exc_info=True)
            return str(exc)
        return None

    def release_unattached(self) -> None:
        if self._owns_mutex and self._mutex is not None and self._mutex.run_id is None:
            self._mutex.release()


def active_task_spawn_blocker(
    run_storage: Any,
    task_id: str,
    *,
    requested_agent_name: str | None,
    parent_session_id: str,
) -> Any | None:
    """Return an active run that should block spawning another agent for a task."""
    if not run_storage.has_active_run_for_task(task_id):
        return None

    active_runs: list[Any] = []
    try:
        maybe_runs = run_storage.list_active_global(task_ids=[task_id], limit=100)
    except (AttributeError, TypeError):
        maybe_runs = None
    if isinstance(maybe_runs, list | tuple):
        active_runs = list(maybe_runs)

    if not active_runs:
        active_run = run_storage.get_active_run_for_task(task_id)
        active_runs = [active_run] if active_run is not None else []

    for active_run in active_runs:
        if _is_parent_merge_orchestrator_run(
            active_run,
            requested_agent_name=requested_agent_name,
            parent_session_id=parent_session_id,
        ):
            continue
        return active_run
    return None


def max_active_agents_for_project(project_path: str) -> int:
    try:
        return load_build_config(project_path).max_active_agents
    except Exception:
        logger.debug("Failed to load build max_active_agents from %s", project_path, exc_info=True)
        return MAX_ACTIVE_AGENTS


def agent_slot_cap_refusal(
    db: Any,
    *,
    project_id: str,
    project_path: str,
    caller_session_id: str | None,
) -> dict[str, Any] | None:
    """Return the cap-reached refusal when the project has no free agent slot."""
    cap = max_active_agents_for_project(project_path)
    active_count = _count_active_agents(db, project_id)
    if active_count < cap:
        return None
    caller_active_count = 0
    if caller_session_id:
        caller_active_count = _count_active_agents(
            db, project_id, parent_session_id=caller_session_id
        )
    return {
        "success": False,
        "error": (
            f"max_active_agents cap reached ({active_count}/{cap}); "
            f"{caller_active_count} of these were spawned by this session"
        ),
        "cap_reached": True,
    }


def slot_cap_response(
    db: Any,
    project_context: Mapping[str, object] | None,
    parent_session_id: str,
) -> dict[str, Any] | None:
    """Return a can_spawn_agent refusal when the context project has no free slot."""
    if not project_context:
        return None
    project_id = project_context.get("id") or project_context.get("project_id")
    project_path = project_context.get("project_path")
    if not isinstance(project_id, str) or not isinstance(project_path, str):
        return None
    refusal = agent_slot_cap_refusal(
        db,
        project_id=project_id,
        project_path=project_path,
        caller_session_id=parent_session_id,
    )
    if refusal is None:
        return None
    return {"success": True, "can_spawn": False, "reason": refusal["error"], "cap_reached": True}


@contextlib.asynccontextmanager
async def reserve_agent_slot(
    *,
    db: Any | None,
    project_id: str,
    project_path: str,
) -> AsyncIterator[dict[str, Any] | None]:
    if db is None:
        yield None
        return

    caller_session_id = get_current_session_id()
    lock = _SLOT_LOCKS.setdefault(project_id, asyncio.Lock())
    async with lock:
        yield await asyncio.to_thread(
            agent_slot_cap_refusal,
            db,
            project_id=project_id,
            project_path=project_path,
            caller_session_id=caller_session_id,
        )


def active_task_response_if_blocked(
    *,
    run_storage: Any,
    task_id: str,
    task_ref: str | None,
    requested_agent_name: str | None,
    parent_session_id: str,
) -> dict[str, Any] | None:
    active_run = active_task_spawn_blocker(
        run_storage,
        task_id,
        requested_agent_name=requested_agent_name,
        parent_session_id=parent_session_id,
    )
    if active_run is None:
        return None
    return active_task_spawn_response(active_run, task_ref)


def _count_active_agents(
    db: Any,
    project_id: str,
    *,
    parent_session_id: str | None = None,
) -> int:
    parent_filter = "AND ar.parent_session_id = %s" if parent_session_id else ""
    params = (
        (project_id, TASK_CLOSE_REVIEWER_AGENT, parent_session_id)
        if parent_session_id
        else (project_id, TASK_CLOSE_REVIEWER_AGENT)
    )
    row = db.fetchone(
        f"""
        SELECT COUNT(*) AS count
        FROM agent_runs ar
        JOIN sessions parent_s ON parent_s.id = ar.parent_session_id
        WHERE ar.status IN ('pending', 'running')
          AND parent_s.project_id = %s
          AND ar.agent_name IS DISTINCT FROM %s
          {parent_filter}
        """,
        params,
    )
    return int(row["count"]) if row else 0


def _run_string_attr(run: Any, name: str) -> str | None:
    value = getattr(run, name, None)
    return value if isinstance(value, str) and value else None


def _is_parent_merge_orchestrator_run(
    run: Any,
    *,
    requested_agent_name: str | None,
    parent_session_id: str,
) -> bool:
    return (
        requested_agent_name == "merge-worker"
        and _run_string_attr(run, "agent_name") == "merge-orchestrator"
        and _run_string_attr(run, "child_session_id") == parent_session_id
    )
