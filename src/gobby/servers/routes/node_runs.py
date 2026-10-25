"""Node transport adapters for hub-owned launch outcomes."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from gobby.agents.spawn_executor_providers import _append_code_index_warning, code_index_warning
from gobby.agents.spawn_executor_support import _codex_prompt_failure_reason
from gobby.hub.node_commands import (
    CONTRACT_VERSION,
    CodeIndexWarningBody,
    PromptDeliveryFailureBody,
)
from gobby.servers.routes.node_hooks import _node_machine
from gobby.sessions.machine_scope import is_local_machine_owner
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.utils.terminal_output import redact_terminal_output
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


async def _body[T: BaseModel](request: Request, model: type[T]) -> T:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(422, "invalid_body") from exc
    if not isinstance(body, dict) or type(body.get("v")) is not int:
        raise HTTPException(422, "invalid_body")
    if body["v"] != CONTRACT_VERSION:
        raise HTTPException(422, "unsupported_version")
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(422, "invalid_body") from exc


async def _run(manager: LocalAgentRunManager, run_id: str, machine: str) -> AgentRun:
    run = await asyncio.to_thread(manager.get, run_id)
    if run is None or not is_local_machine_owner(run.machine_id, machine):
        raise HTTPException(404, "run_not_found")
    return run


def create_node_runs_router(server: HTTPServer) -> APIRouter:
    router = APIRouter(prefix="/api/nodes", tags=["nodes"])

    @router.post("/runs/prompt-delivery-failure")
    async def prompt_delivery_failure(request: Request) -> dict[str, bool]:
        machine = _node_machine(server, request)
        body = await _body(request, PromptDeliveryFailureBody)
        pane = redact_terminal_output(body.pane or "")
        detail = redact_terminal_output(body.detail)
        code = redact_terminal_output(body.code)
        manager = LocalAgentRunManager(server.services.database)
        run = await _run(manager, body.run_id, machine)
        terminals = server.services.terminal_services
        # The failure transition retires the terminal row. Keep its live handle
        # so runtime termination can still cancel the node's process afterward.
        terminal = await asyncio.to_thread(terminals.terminal_for, run) if terminals else None
        error = _codex_prompt_failure_reason(pane, code=code, detail=detail)
        transitioned = await asyncio.to_thread(
            manager.fail_uninitialized_prompt_delivery, body.run_id, error=error
        )
        if transitioned is None:
            return {"failed": False}
        logger.error("Codex prompt delivery failed for run %s: %s", transitioned.id, error)
        try:
            if terminals is not None and terminal is not None:
                await terminals.runtime_for(terminal).terminate(terminal, 5.0)
        except Exception as exc:
            logger.error("Cannot terminate prompt-failure terminal: %s", type(exc).__name__)
        try:
            monitor = server.services.agent_lifecycle_monitor
            cleanup = monitor.get_cleanup_agent() if monitor is not None else None
            if cleanup is not None:
                await cleanup(
                    transitioned, terminal_payload=error, preterminalized_run=transitioned
                )
            else:
                logger.warning(
                    "No immediate cleanup handler for prompt failure run %s", transitioned.id
                )
        except Exception as exc:
            logger.error("Prompt failure cleanup failed: %s", type(exc).__name__)
        return {"failed": True}

    @router.post("/runs/code-index-warning")
    async def code_index_preflight_warning(request: Request) -> dict[str, str]:
        machine = _node_machine(server, request)
        body = await _body(request, CodeIndexWarningBody)
        warning = code_index_warning(body.warning.cwd, body.warning.message)
        manager = LocalAgentRunManager(server.services.database)
        run = await _run(manager, body.run_id, machine)
        prompt = _append_code_index_warning(body.prompt, warning)
        if run.child_session_id is not None:
            variables = SessionVariableManager(server.services.database)
            existing = await asyncio.to_thread(variables.get_variables, run.child_session_id)
            updates: dict[str, Any] = {
                "code_index_preflight_warning": warning,
                "prompt": prompt,
            }
            skills = existing.get("additional_skills", [])
            if isinstance(skills, list):
                updates["additional_skills"] = [
                    skill for skill in skills if skill != "gobby:references/code-index/overview.md"
                ]
            await asyncio.to_thread(variables.merge_variables, run.child_session_id, updates)
        logger.warning(
            "Continuing spawn after scoped code index preflight failed for cwd=%s: %s",
            warning["cwd"],
            warning["message"],
        )
        return {"prompt": prompt}

    return router
