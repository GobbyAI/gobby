"""Agent spawn routes for Gobby HTTP server.

Provides endpoints for spawning agents on tasks from the web UI,
including single-task and batch spawning, plus per-category launch defaults.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from gobby.storage.task_dependencies import TaskDependencyManager
from gobby.utils.metrics import get_metrics_collector

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class AgentSpawnRequest(BaseModel):
    """Request body for spawning an agent on a task."""

    task_id: str
    agent_name: str = "default"
    prompt: str | None = None
    mode: Literal["terminal", "web_chat", "headless"] = "terminal"
    isolation: Literal["none", "worktree", "clone"] | None = None
    provider: str | None = None
    model: str | None = None
    workflow: str | None = None
    branch_name: str | None = None
    base_branch: str | None = None
    timeout: float | None = None
    max_turns: int | None = None


class AgentSpawnResponse(BaseModel):
    """Response from agent spawn."""

    success: bool
    run_id: str | None = None
    child_session_id: str | None = None
    conversation_id: str | None = None
    mode: str
    isolation: str | None = None
    branch_name: str | None = None
    pid: int | None = None
    error: str | None = None


class BatchSpawnRequest(BaseModel):
    """Request body for batch agent spawning."""

    spawns: list[AgentSpawnRequest]


class BatchSpawnResponse(BaseModel):
    """Response from batch agent spawn."""

    results: list[AgentSpawnResponse]
    succeeded: int
    failed: int


class LaunchDefaultsRequest(BaseModel):
    """Request body for saving per-category launch defaults."""

    project_id: str
    category: str
    agent_name: str = "default"
    mode: Literal["terminal", "web_chat", "headless"] = "terminal"
    isolation: Literal["none", "worktree", "clone"] = "none"
    model: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BUILT_IN_DEFAULTS: dict[str, Any] = {
    "agent_name": "default",
    "mode": "inherit",
    "isolation": "inherit",
    "model": None,
}

# Semaphore to cap concurrent batch spawns
_BATCH_SEMAPHORE = asyncio.Semaphore(5)


def _build_task_prompt(
    task: Any, deps: list[Any] | None = None, comments: list[Any] | None = None
) -> str:
    """Build an auto-generated prompt from task context."""
    parts: list[str] = []

    # Title and description
    ref = f"#{task.seq_num}" if task.seq_num else task.id
    parts.append(f"## Task {ref}: {task.title}")
    if task.description:
        parts.append(f"\n{task.description}")

    # Validation criteria
    if task.validation_criteria:
        parts.append(f"\n### Validation Criteria\n{task.validation_criteria}")

    # Dependencies
    if deps:
        dep_lines = []
        for d in deps:
            d_ref = f"#{d.seq_num}" if d.seq_num else d.id
            status = getattr(d, "status", "unknown")
            dep_lines.append(f"- {d_ref}: {d.title} ({status})")
        if dep_lines:
            parts.append("\n### Dependencies\n" + "\n".join(dep_lines))

    # Recent comments (last 5)
    if comments:
        recent = comments[-5:]
        comment_lines = []
        for c in recent:
            author = getattr(c, "author", "unknown")
            body = getattr(c, "body", str(c))
            comment_lines.append(f"- **{author}**: {body}")
        if comment_lines:
            parts.append("\n### Recent Comments\n" + "\n".join(comment_lines))

    parts.append(
        "\n---\n"
        "Work on this task. When done, validate and close it. "
        "If blocked, escalate with a clear explanation."
    )
    return "\n".join(parts)


def _get_config_store(server: HTTPServer) -> Any:
    """Get the config store from server services."""
    if server.services.config_store:
        return server.services.config_store
    # Fallback: create one from the database
    from gobby.storage.config_store import ConfigStore

    return ConfigStore(server.services.database)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_agent_spawn_router(server: HTTPServer) -> APIRouter:
    """Create agent spawn router with endpoints bound to server instance."""
    router = APIRouter(prefix="/api/agents", tags=["agent-spawn"])
    metrics = get_metrics_collector()

    async def _get_or_create_launcher_session(project_id: str) -> str:
        """Get or create a persistent web_launcher session for HTTP-initiated spawns."""
        sm = server.services.session_manager
        # Look for existing launcher session
        sessions = sm.list(project_id=project_id)
        for s in sessions:
            if getattr(s, "source", None) == "web_launcher":
                return s.id

        # Create a new one
        session_id = str(uuid.uuid4())
        from gobby.utils.machine_id import get_machine_id

        sm.register(
            external_id=f"web-launcher-{project_id[:8]}",
            machine_id=get_machine_id() or "web",
            source="web_launcher",
            project_id=project_id,
            title="Web Launcher",
            agent_depth=0,
        )
        # Fetch the just-created session to get its DB id
        sessions = sm.list(project_id=project_id)
        for s in sessions:
            if getattr(s, "source", None) == "web_launcher":
                return s.id
        return session_id

    async def _do_spawn(
        req: AgentSpawnRequest, project_id: str | None = None
    ) -> AgentSpawnResponse:
        """Execute a single spawn request."""
        task_manager = server.services.task_manager
        if not task_manager:
            return AgentSpawnResponse(
                success=False, mode=req.mode, error="Task manager unavailable"
            )

        # Resolve task
        try:
            task = task_manager.get_task(req.task_id)
        except Exception as e:
            logger.debug(f"Failed to get task {req.task_id}: {e}")
            task = None
        if not task:
            return AgentSpawnResponse(
                success=False, mode=req.mode, error=f"Task '{req.task_id}' not found"
            )

        effective_project_id = project_id or getattr(task, "project_id", None)
        if not effective_project_id:
            return AgentSpawnResponse(
                success=False, mode=req.mode, error="Could not determine project_id"
            )

        # Build prompt
        prompt = req.prompt
        if not prompt:
            deps = None
            try:
                dep_manager = TaskDependencyManager(task_manager.db)
                dep_records = dep_manager.get_all_dependencies(req.task_id)
                if dep_records:
                    deps = [
                        task_manager.get_task(d.depends_on)
                        for d in dep_records
                        if task_manager.get_task(d.depends_on)
                    ]
            except Exception:
                pass
            prompt = _build_task_prompt(task, deps)

        # Handle web_chat mode — return conversation_id for frontend to open
        if req.mode == "web_chat":
            conversation_id = str(uuid.uuid4())

            # Update task status
            try:
                task_manager.update_task(
                    req.task_id, status="in_progress", assignee=conversation_id
                )
            except Exception as e:
                logger.warning(f"Failed to update task status: {e}")

            # Broadcast task update
            _broadcast_task_update(server, req.task_id)

            return AgentSpawnResponse(
                success=True,
                mode="web_chat",
                conversation_id=conversation_id,
                # Store the prompt so the frontend can send it as the first message
            )

        # Terminal / headless mode — use spawn_agent_impl
        runner = server.services.agent_runner
        if not runner:
            return AgentSpawnResponse(
                success=False, mode=req.mode, error="Agent runner unavailable"
            )

        # Get parent session for spawning
        parent_session_id = await _get_or_create_launcher_session(effective_project_id)

        # Load agent definition
        from gobby.workflows.agent_resolver import AgentResolutionError, resolve_agent

        agent_body = None
        try:
            agent_body = resolve_agent(
                req.agent_name, server.services.database, project_id=effective_project_id
            )
        except AgentResolutionError:
            if req.agent_name != "default":
                return AgentSpawnResponse(
                    success=False,
                    mode=req.mode,
                    error=f"Agent definition '{req.agent_name}' not found",
                )

        # Compose prompt with preamble
        effective_prompt = prompt
        if agent_body:
            preamble = agent_body.build_prompt_preamble()
            if preamble:
                effective_prompt = f"{preamble}\n\n---\n\n{prompt}"

        # Build initial_variables
        initial_variables: dict[str, Any] = {}
        if agent_body:
            initial_variables["_agent_type"] = agent_body.name
            if agent_body.workflows.rules:
                initial_variables["_agent_rules"] = agent_body.workflows.rules
            if agent_body.workflows.variables:
                initial_variables.update(agent_body.workflows.variables)

        # Determine effective workflow
        effective_workflow = req.workflow
        if effective_workflow is None and agent_body and agent_body.workflows.pipeline:
            effective_workflow = agent_body.workflows.pipeline

        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        # Map "terminal"/"headless" to the mode enum spawn_agent_impl expects
        spawn_mode: Literal["terminal", "embedded", "headless", "self"] = (
            "headless" if req.mode == "headless" else "terminal"
        )

        result = await spawn_agent_impl(
            prompt=effective_prompt,
            runner=runner,
            agent_body=agent_body,
            agent_lookup_name=req.agent_name,
            task_id=req.task_id,
            task_manager=task_manager,
            isolation=req.isolation,
            branch_name=req.branch_name,
            base_branch=req.base_branch,
            worktree_storage=server.services.worktree_storage,
            git_manager=server.services.git_manager,
            clone_storage=server.services.clone_storage,
            clone_manager=None,
            workflow=effective_workflow,
            mode=spawn_mode,
            provider=req.provider,
            model=req.model,
            timeout=req.timeout,
            max_turns=req.max_turns,
            parent_session_id=parent_session_id,
            initial_variables=initial_variables,
            session_manager=server.services.session_manager,
            db=server.services.database,
        )

        if result.get("success"):
            # Update task status
            try:
                child_sid = result.get("child_session_id", "")
                task_manager.update_task(req.task_id, status="in_progress", assignee=child_sid)
            except Exception as e:
                logger.warning(f"Failed to update task after spawn: {e}")

            _broadcast_task_update(server, req.task_id)

            return AgentSpawnResponse(
                success=True,
                run_id=result.get("run_id"),
                child_session_id=result.get("child_session_id"),
                mode=req.mode,
                isolation=result.get("isolation"),
                branch_name=result.get("branch_name"),
                pid=result.get("pid"),
            )
        else:
            return AgentSpawnResponse(
                success=False,
                mode=req.mode,
                error=result.get("error", "Unknown spawn error"),
            )

    def _broadcast_task_update(server: HTTPServer, task_id: str) -> None:
        """Fire a task_updated WebSocket event."""
        ws = server.websocket_server
        if ws and hasattr(ws, "broadcast"):
            try:
                import asyncio as _asyncio

                _asyncio.ensure_future(ws.broadcast({"type": "task_updated", "task_id": task_id}))
            except Exception as e:
                logger.debug(f"Failed to broadcast task update: {e}")

    # -----------------------------------------------------------------------
    # POST /api/agents/spawn
    # -----------------------------------------------------------------------
    @router.post("/spawn")
    async def spawn_agent(request: AgentSpawnRequest) -> dict[str, Any]:
        """Spawn an agent to work on a task."""
        metrics.inc_counter("http_requests_total")
        metrics.inc_counter("agent_spawns_total")

        try:
            # Resolve project context
            from gobby.utils.project_context import get_project_context

            ctx = get_project_context()
            project_id = ctx.get("id") if ctx else None

            result = await _do_spawn(request, project_id)
            if not result.success:
                raise HTTPException(status_code=400, detail=result.error)
            return result.model_dump(exclude_none=True)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error spawning agent: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    # -----------------------------------------------------------------------
    # POST /api/agents/spawn/batch
    # -----------------------------------------------------------------------
    @router.post("/spawn/batch")
    async def spawn_batch(request: BatchSpawnRequest) -> dict[str, Any]:
        """Spawn agents for multiple tasks concurrently."""
        metrics.inc_counter("http_requests_total")

        if not request.spawns:
            raise HTTPException(status_code=400, detail="No spawn requests provided")

        if len(request.spawns) > 20:
            raise HTTPException(status_code=400, detail="Maximum 20 spawns per batch")

        from gobby.utils.project_context import get_project_context

        ctx = get_project_context()
        project_id = ctx.get("id") if ctx else None

        async def _limited_spawn(req: AgentSpawnRequest) -> AgentSpawnResponse:
            async with _BATCH_SEMAPHORE:
                try:
                    return await _do_spawn(req, project_id)
                except Exception as e:
                    logger.error(f"Batch spawn error for task {req.task_id}: {e}")
                    return AgentSpawnResponse(success=False, mode=req.mode, error=str(e))

        results = await asyncio.gather(*[_limited_spawn(s) for s in request.spawns])
        succeeded = sum(1 for r in results if r.success)
        failed = len(results) - succeeded

        return BatchSpawnResponse(
            results=list(results),
            succeeded=succeeded,
            failed=failed,
        ).model_dump(exclude_none=True)

    # -----------------------------------------------------------------------
    # GET /api/agents/launch-defaults
    # -----------------------------------------------------------------------
    @router.get("/launch-defaults")
    async def get_launch_defaults(
        project_id: str = Query(..., description="Project ID"),
    ) -> dict[str, Any]:
        """Get per-category launch defaults for the project."""
        metrics.inc_counter("http_requests_total")
        try:
            store = _get_config_store(server)
            key = f"launch_defaults.{project_id}"
            saved = store.get(key) or {}
            # Merge with built-in defaults for any missing categories
            return {"status": "success", "defaults": saved, "built_in": _BUILT_IN_DEFAULTS}
        except Exception as e:
            logger.error(f"Error fetching launch defaults: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    # -----------------------------------------------------------------------
    # PUT /api/agents/launch-defaults
    # -----------------------------------------------------------------------
    @router.put("/launch-defaults")
    async def save_launch_defaults(request: LaunchDefaultsRequest) -> dict[str, Any]:
        """Save per-category launch defaults for the project."""
        metrics.inc_counter("http_requests_total")
        try:
            store = _get_config_store(server)
            key = f"launch_defaults.{request.project_id}"
            existing = store.get(key) or {}
            existing[request.category] = {
                "agent_name": request.agent_name,
                "mode": request.mode,
                "isolation": request.isolation,
                "model": request.model,
            }
            store.set(key, existing, source="web_ui")
            return {"status": "success", "category": request.category}
        except Exception as e:
            logger.error(f"Error saving launch defaults: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    # -----------------------------------------------------------------------
    # POST /api/agents/spawn/prompt-preview
    # -----------------------------------------------------------------------
    @router.post("/spawn/prompt-preview")
    async def preview_spawn_prompt(
        task_id: str = Query(..., description="Task ID to generate prompt for"),
        agent_name: str = Query("default", description="Agent definition name"),
    ) -> dict[str, Any]:
        """Generate a preview of the auto-generated spawn prompt for a task."""
        metrics.inc_counter("http_requests_total")
        try:
            task_manager = server.services.task_manager
            if not task_manager:
                raise HTTPException(status_code=500, detail="Task manager unavailable")

            try:
                task = task_manager.get_task(task_id)
            except (ValueError, Exception):
                task = None
            if not task:
                raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

            deps = None
            try:
                dep_manager = TaskDependencyManager(task_manager.db)
                dep_records = dep_manager.get_all_dependencies(task_id)
                if dep_records:
                    deps = [
                        task_manager.get_task(d.depends_on)
                        for d in dep_records
                        if task_manager.get_task(d.depends_on)
                    ]
            except Exception:
                pass

            prompt = _build_task_prompt(task, deps)

            # Optionally prepend agent preamble
            preamble = None
            if agent_name != "default" or True:  # Always show preamble if available
                from gobby.workflows.agent_resolver import AgentResolutionError, resolve_agent

                try:
                    from gobby.utils.project_context import get_project_context

                    ctx = get_project_context()
                    pid = ctx.get("id") if ctx else None
                    agent_body = resolve_agent(agent_name, server.services.database, project_id=pid)
                    if agent_body:
                        preamble = agent_body.build_prompt_preamble()
                except (AgentResolutionError, Exception):
                    pass

            return {
                "status": "success",
                "prompt": prompt,
                "preamble": preamble,
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error generating prompt preview: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    return router
