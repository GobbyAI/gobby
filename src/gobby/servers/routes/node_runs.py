"""Node transport adapters for hub-owned launch outcomes."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, ValidationError

from gobby.agents.agent_health import (
    bootstrap_accounting_stall_error,
    session_never_initialized,
    timeout_reason,
)
from gobby.agents.capture import terminate_managed_runtime_async
from gobby.agents.recovery_state import is_recovery_protected
from gobby.agents.run_completion import bound_task_is_closed
from gobby.agents.spawn_executor_providers import _append_code_index_warning, code_index_warning
from gobby.agents.spawn_executor_support import _codex_prompt_failure_reason
from gobby.config.tmux import TmuxConfig
from gobby.hub.node_commands import (
    CONTRACT_VERSION,
    CodeIndexWarningBody,
    PromptDeliveryFailureBody,
    TerminateBody,
    TerminateCheckpoint,
)
from gobby.hub.remote_pane import remote_pane_store
from gobby.servers.routes.node_hooks import _node_machine
from gobby.sessions.machine_scope import is_local_machine_owner
from gobby.storage.agents import AgentRun, LocalAgentRunManager, TerminalAction
from gobby.storage.checkpoints import Checkpoint, LocalCheckpointManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import parse_stored_datetime
from gobby.utils.terminal_output import redact_terminal_output
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

_TAIL_LIMIT = 2000
_DOOM_LOOP_ERROR = "doom loop: dismissed loop prompt 3+ times"


@dataclass(frozen=True)
class _Verdict:
    """The hub's answer to a terminate post: a kill with its error, or no kill."""

    error: str | None = None
    action: TerminalAction = "fail"
    retry: bool = False


@dataclass(frozen=True)
class _Intent:
    """The termination a run is running: its action and the error its cleanup records."""

    action: TerminalAction
    error: str


def _kill_error(text: str, pane_tail: str) -> str:
    if not pane_tail:
        return text
    return f"{text}\n\n--- Last terminal output ---\n{pane_tail[-_TAIL_LIMIT:]}"


def _timeout_verdict(run: AgentRun, session: Any, pane_tail: str) -> _Verdict:
    started = parse_stored_datetime(run.started_at) if run.started_at else None
    if not run.timeout_seconds or started is None:
        return _Verdict(retry=True)
    age = (datetime.now(UTC) - started).total_seconds()
    if age <= run.timeout_seconds:
        return _Verdict(retry=True)
    reason = timeout_reason(run.timeout_seconds)
    stall = bootstrap_accounting_stall_error(run, session, age_seconds=age, pane_snapshot=pane_tail)
    if stall is None:
        return _Verdict(error=_kill_error(reason, pane_tail), action="timeout")
    return _Verdict(error=_kill_error(stall, pane_tail), action="fail")


def _init_verdict(run: AgentRun, session: Any, init_timeout_seconds: int) -> _Verdict:
    started = parse_stored_datetime(run.started_at) if run.started_at else None
    if started is None or session is None or not session.updated_at or not session.created_at:
        return _Verdict(retry=True)
    if parse_stored_datetime(session.created_at) is None:
        return _Verdict(retry=True)
    age = (datetime.now(UTC) - started).total_seconds()
    if age < init_timeout_seconds:
        return _Verdict(retry=True)
    if not session_never_initialized(run, session):
        return _Verdict(retry=False)
    return _Verdict(
        error=(
            f"Provider connection timed out: agent never initialized "
            f"after {age:.0f}s (provider={run.provider})"
        ),
    )


async def _terminate_verdict(
    server: HTTPServer, run: AgentRun, body: TerminateBody, detail: str, pane_tail: str
) -> _Verdict:
    if body.cause == "doom_loop":
        return _Verdict(error=_DOOM_LOOP_ERROR)
    if body.cause == "provider_stall":
        stall = f"Provider stall: {detail} (provider={run.provider})"
        return _Verdict(error=f"{stall}\n\n{pane_tail.strip()}")
    session = None
    session_manager = server.services.session_manager
    if run.child_session_id and session_manager is not None:
        session = await asyncio.to_thread(session_manager.get, run.child_session_id)
    if body.cause == "timeout":
        return _timeout_verdict(run, session, pane_tail)
    config = server.config
    tmux = config.tmux if config is not None else TmuxConfig()
    return _init_verdict(run, session, tmux.init_timeout_seconds)


def _terminalizer(
    manager: LocalAgentRunManager,
    cleanup: Callable[..., Awaitable[None]],
    run: AgentRun,
    reason: str,
) -> Callable[[TerminalAction, str | None], Awaitable[AgentRun | None]]:
    async def terminalize(action: TerminalAction, payload: str | None) -> AgentRun | None:
        await cleanup(
            run,
            terminal_payload=payload or reason,
            is_success=action == "complete",
            is_timeout=action == "timeout",
        )
        return await asyncio.to_thread(manager.get, run.id)

    return terminalize


def _persisted_intent(run: AgentRun) -> _Intent | None:
    action, error = run.pending_terminal_action, run.pending_terminal_reason
    if action is None or error is None:
        return None
    return _Intent(action, error)


def _record_checkpoint(
    db: HubDatabase, run: AgentRun, task_id: str, checkpoint: TerminateCheckpoint
) -> None:
    # The id keys the run and commit, so a retried post finds the record instead of adding one.
    manager = LocalCheckpointManager(db)
    record_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{run.id}:{checkpoint.commit_sha}"))
    if manager.get(record_id) is not None:
        return
    try:
        manager.create(
            Checkpoint(
                id=record_id,
                task_id=task_id,
                session_id=run.child_session_id,
                run_id=run.id,
                ref_name=checkpoint.ref_name,
                commit_sha=checkpoint.commit_sha,
                parent_sha=checkpoint.parent_sha,
                files_changed=checkpoint.files_changed,
                message=checkpoint.message,
                created_at=datetime.now(UTC),
            )
        )
    except UniqueViolation as exc:
        # Another request may insert this run/commit after our lookup missed.
        if exc.diag.constraint_name != "checkpoints_pkey":
            raise


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

    @router.post("/runs/terminate")
    async def terminate_run(request: Request) -> dict[str, bool]:
        machine = _node_machine(server, request)
        body = await _body(request, TerminateBody)
        detail = redact_terminal_output(body.detail)
        pane_tail = redact_terminal_output(body.pane_tail)
        db = server.services.database
        manager = LocalAgentRunManager(db)
        run = await _run(manager, body.run_id, machine)
        if run.status not in ("pending", "running"):
            return {"terminated": False, "retry": False}
        if is_recovery_protected(run) or (
            not run.is_interactive and await asyncio.to_thread(bound_task_is_closed, db, run)
        ):
            return {"terminated": False, "retry": True}
        terminals = server.services.terminal_services
        if terminals is None:
            return {"terminated": False, "retry": True}
        # A retry finishes the termination an earlier post persisted, on the terminal it bound.
        intent = _persisted_intent(run)
        lookup = terminals.terminal_for if intent is None else terminals.bound_terminal_for
        terminal = await asyncio.to_thread(lookup, run)
        if terminal is None:
            return {"terminated": False, "retry": True}
        if intent is None:
            verdict = await _terminate_verdict(server, run, body, detail, pane_tail)
            if verdict.error is None:
                return {"terminated": False, "retry": verdict.retry}
            intent = _Intent(verdict.action, verdict.error)
            if body.checkpoint is not None and run.task_id is not None:
                await asyncio.to_thread(_record_checkpoint, db, run, run.task_id, body.checkpoint)
            # The capture reads this tail from the store, so it lands before the kill.
            remote_pane_store().record_tail(
                terminal.id, terminal.machine_id, run.id, pane_tail[-_TAIL_LIMIT:]
            )
        monitor = server.services.agent_lifecycle_monitor
        cleanup = monitor.get_cleanup_agent() if monitor is not None else None
        if cleanup is None:
            logger.warning("No immediate cleanup handler for terminate run %s", run.id)
        result = await terminate_managed_runtime_async(
            storage=manager,
            run=run,
            terminal=terminal,
            runtime=terminals.runtime_for(terminal),
            action=intent.action,
            reason=intent.error,
            terminalize=None
            if cleanup is None
            else _terminalizer(manager, cleanup, run, intent.error),
        )
        if not result.success:
            logger.error("Terminate kill failed for run %s: %s", run.id, result.error_code)
            return {"terminated": False, "retry": True}
        return {"terminated": True, "retry": False}

    @router.get("/detection-manifests")
    async def detection_manifests(request: Request) -> Response:
        _node_machine(server, request)
        rows = await asyncio.to_thread(
            server.services.database.fetchall,
            "SELECT provider_id, content FROM detection_manifests ORDER BY provider_id",
        )
        digest = hashlib.sha256()
        for row in rows:
            content_sha = hashlib.sha256(row["content"].encode()).hexdigest()
            digest.update(f"{row['provider_id']}\n{content_sha}\n".encode())
        etag = f'"{digest.hexdigest()}"'
        requested = {tag.strip() for tag in request.headers.get("if-none-match", "").split(",")}
        if etag in requested:
            return Response(status_code=304, headers={"ETag": etag})
        manifests = [{"provider_id": row["provider_id"], "content": row["content"]} for row in rows]
        return JSONResponse({"manifests": manifests}, headers={"ETag": etag})

    return router
