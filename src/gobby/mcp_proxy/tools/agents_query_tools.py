"""Agent query and status MCP tool registration."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Callable, Mapping
from typing import Any, cast
from uuid import UUID

from gobby.agents.completion_subscribers import (
    SubscriptionPersistenceError,
    remove_agent_completion_subscribers,
    subscribe_agent_completion,
)
from gobby.agents.detection.safe_regex import (
    InvalidPatternError,
    RegexOutcome,
    compile_safe_regex,
)
from gobby.agents.recovery_state import (
    daemon_resume_successor_id,
    is_daemon_stop_parked,
)
from gobby.agents.tmux import get_tmux_session_manager
from gobby.mcp_proxy.tools.agent_live_activity import (
    overlay_live_activity,
    overlay_runs_live_activity,
)
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.agents_payloads import (
    _AGENT_CAPTURE_PAGE_DEFAULT_CHARS,
    _AGENT_CAPTURE_PAGE_MAX_CHARS,
    _agent_capture_parts,
    _agent_result_payload,
)
from gobby.mcp_proxy.tools.agents_runtime import facade
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.wait_tools import (
    MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS,
    clamp_wait_tool_timeout,
)
from gobby.storage.agent_resume import register_daemon_resume_waiter
from gobby.storage.agents import AgentRunStatus

logger = logging.getLogger(__name__)

_WAIT_OUTPUT_CAPTURE_LINES = 200
_WAIT_OUTPUT_CAPTURE_FAILURE_LIMIT = 3
_WAIT_OUTPUT_EXCERPT_CHARS = 4_096


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, 100))


_RUN_ID_PREFIX_PATTERN = re.compile(r"[0-9a-f]{8,32}")


def _list_run_payload(run: Any) -> dict[str, Any]:
    """Return identity and coordinator decision fields for agent run lists."""
    metadata = getattr(run, "resume_metadata_json", None)
    if not isinstance(metadata, Mapping):
        metadata = {}
    return {
        "run_id": run.id,
        "task_ref": metadata.get("task_ref") or getattr(run, "task_id", None),
        "agent_name": getattr(run, "agent_name", None),
        "status": run.status,
        "started_at": getattr(run, "started_at", None),
        "branch_name": metadata.get("branch_name"),
        "tool_calls_count": getattr(run, "tool_calls_count", 0),
        "turns_used": getattr(run, "turns_used", 0),
    }


def _validated_run_ref(run_id: str) -> tuple[str, bool] | None:
    """Return normalized run reference and whether it is a full UUID."""
    run_ref = run_id.strip().lower()
    try:
        full_id = str(UUID(run_ref))
    except ValueError:
        full_id = None
    if full_id == run_ref:
        return run_ref, True
    if _RUN_ID_PREFIX_PATTERN.fullmatch(run_ref):
        return run_ref, False
    return None


def _invalid_run_ref(error: str, **details: Any) -> dict[str, Any]:
    return {
        "success": False,
        "status": "error",
        "error": error,
        "error_code": "INVALID_ARGUMENTS",
        **details,
    }


def _wait_for_output_error(code: str, message: str) -> dict[str, Any]:
    return {"success": False, "error": code, "message": message}


def _bounded_wait_output_excerpt(pane_output: str) -> str:
    if len(pane_output) <= _WAIT_OUTPUT_EXCERPT_CHARS:
        return pane_output
    half = (_WAIT_OUTPUT_EXCERPT_CHARS - len("\n… output omitted …\n")) // 2
    return f"{pane_output[:half]}\n… output omitted …\n{pane_output[-half:]}"


def _finite_number(value: float, *, name: str) -> tuple[float | None, dict[str, Any] | None]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, _wait_for_output_error("invalid_argument", f"{name} must be numeric")
    if not math.isfinite(number):
        return None, _wait_for_output_error("invalid_argument", f"{name} must be finite")
    return number, None


def _follow_daemon_resume_chain(
    run: Any,
    *,
    get_run: Callable[[str], Any | None],
) -> tuple[Any, bool]:
    """Follow consumed daemon-stop originals to their authoritative successor."""
    current = run
    visited: set[str] = set()
    while True:
        current_id = str(current.id)
        if current_id in visited:
            raise ValueError("Daemon resume successor chain contains a cycle")
        visited.add(current_id)
        successor_id = daemon_resume_successor_id(current)
        if not successor_id:
            return current, is_daemon_stop_parked(current)
        successor = get_run(successor_id)
        if successor is None:
            return current, True
        current = successor


def register_agent_query_tools(
    registry: InternalToolRegistry,
    ctx: AgentsRegistryContext,
) -> None:
    @registry.tool(
        name="get_agent_result",
        description=(
            "Look up an agent run's current status and result. Safe for explicit polling; "
            "creates no completion subscription."
        ),
    )
    async def get_agent_result(run_id: str) -> dict[str, Any]:
        run = ctx.runner.get_run(run_id)
        if not run:
            return {"success": False, "error": f"Agent run {run_id} not found"}
        try:
            run, recovery_pending = _follow_daemon_resume_chain(
                run,
                get_run=ctx.runner.get_run,
            )
        except ValueError as exc:
            return {
                "success": False,
                "error": str(exc),
                "error_code": "daemon_resume_chain_corrupt",
            }
        run = await overlay_live_activity(run, ctx.transcript_reader)
        return {
            "success": True,
            "recovery_pending": recovery_pending,
            **_agent_result_payload(run),
        }

    async def get_agent_capture(
        run_id: str,
        offset: int = 0,
        limit: int = _AGENT_CAPTURE_PAGE_DEFAULT_CHARS,
    ) -> dict[str, Any]:
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return {
                "success": False,
                "error": "offset must be a non-negative integer",
                "error_code": "invalid_arguments",
            }
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= _AGENT_CAPTURE_PAGE_MAX_CHARS
        ):
            return {
                "success": False,
                "error": (
                    f"limit must be an integer between 1 and {_AGENT_CAPTURE_PAGE_MAX_CHARS}"
                ),
                "error_code": "invalid_arguments",
            }

        run = ctx.runner.get_run(run_id)
        if run is None:
            return {"success": False, "error": f"Agent run {run_id} not found"}
        capture = _agent_capture_parts(run)
        if capture is None:
            return {
                "success": False,
                "error": f"Agent run {run_id} has no terminal capture",
                "error_code": "capture_not_found",
            }

        total_chars = len(capture.content)
        content = capture.content[offset : offset + limit]
        next_offset_value = offset + len(content)
        next_offset = next_offset_value if next_offset_value < total_chars else None
        page = {
            "run_id": run.id,
            "capture_id": capture.capture_id,
            "total_chars": total_chars,
            "offset": offset,
            "limit": limit,
            "content": content,
            "next_offset": next_offset,
        }
        if capture.malformed:
            return {
                "success": False,
                "error": "Agent capture start marker is missing",
                "error_code": "capture_corrupt",
                "malformed": True,
                **page,
            }
        return {"success": True, **page}

    registry.register(
        name="get_agent_capture",
        description=(
            "Read one Unicode-character page from a terminal capture referenced by "
            "get_agent_result or wait_for_agent capture metadata."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _AGENT_CAPTURE_PAGE_MAX_CHARS,
                    "default": _AGENT_CAPTURE_PAGE_DEFAULT_CHARS,
                },
            },
            "required": ["run_id"],
        },
        func=get_agent_capture,
    )

    @registry.tool(
        name="wait_for_agent",
        description=(
            "Create a one-shot durable subscription to an agent run, then end the turn. "
            "The daemon wakes this session with the result when the run completes. "
            "Repeated calls are idempotent recovery behavior."
        ),
    )
    async def wait_for_agent(run_id: str) -> dict[str, Any]:
        agents = facade()
        run = ctx.runner.get_run(run_id)
        if run is None:
            return {"success": False, "error": f"Agent run {run_id} not found"}
        requested_run = run
        try:
            run, recovery_pending = _follow_daemon_resume_chain(
                run,
                get_run=ctx.runner.get_run,
            )
        except ValueError as exc:
            return {
                "success": False,
                "error": str(exc),
                "error_code": "daemon_resume_chain_corrupt",
            }
        if run.status in agents._TERMINAL_AGENT_STATUSES and not recovery_pending:
            payload = _agent_result_payload(
                await overlay_live_activity(run, ctx.transcript_reader),
                include_prompt=False,
            )
            return {
                "success": True,
                "completed": True,
                "notification_registered": False,
                **payload,
            }

        session_id = ctx.get_current_session_id()
        if session_id is None:
            return {
                "success": False,
                "error": "wait_for_agent requires an active MCP session",
                "error_code": "missing_session_context",
            }
        if ctx.completion_registry is None or ctx.db is None:
            return {
                "success": False,
                "error": "Agent completion notification services are unavailable",
                "error_code": "completion_services_unavailable",
            }

        if recovery_pending or run.id != requested_run.id:
            try:
                wait_target = register_daemon_resume_waiter(
                    ctx.db,
                    run_id=requested_run.id,
                    subscriber_session_id=session_id,
                )
            except ValueError as exc:
                return {
                    "success": False,
                    "error": str(exc),
                    "error_code": "daemon_resume_wait_registration_failed",
                }
            target_run = ctx.runner.get_run(wait_target.run_id)
            if target_run is None:
                return {
                    "success": False,
                    "error": f"Agent run {wait_target.run_id} not found",
                }
            run = target_run
            if run.status in agents._TERMINAL_AGENT_STATUSES and not wait_target.recovery_pending:
                payload = _agent_result_payload(
                    await overlay_live_activity(run, ctx.transcript_reader),
                    include_prompt=False,
                )
                return {
                    "success": True,
                    "completed": True,
                    "notification_registered": False,
                    **payload,
                }
            ctx.completion_registry.register(
                run.id,
                subscribers=[session_id],
                continuation_prompt=getattr(run, "continuation_prompt", None),
            )
            refreshed = ctx.runner.get_run(run.id)
            if refreshed is not None and daemon_resume_successor_id(refreshed):
                # Finalization consumed this original between the fenced DB
                # registration and the in-memory register. The durable
                # subscription was copied to the successor under the fence,
                # so drop the stale local entry instead of leaking it.
                ctx.completion_registry.cleanup(run.id)
            payload = _agent_result_payload(
                await overlay_live_activity(run, ctx.transcript_reader),
                include_prompt=False,
            )
            return {
                "success": True,
                "completed": False,
                "recovery_pending": wait_target.recovery_pending,
                "notification_registered": True,
                "notification_session_id": session_id,
                **payload,
            }

        payload = _agent_result_payload(
            await overlay_live_activity(run, ctx.transcript_reader),
            include_prompt=False,
        )

        # INVARIANT: this handler runs on the completion registry's owning event loop.
        # The region from this status re-read through conditional cleanup contains no
        # await, so notify cannot start, resume, or snapshot subscribers inside it.
        # Every terminal producer commits its DB transition before scheduling or
        # resuming notify/delivery on this loop; the §1.4 terminal-producer contract
        # owns that ordering. The first and post-registration reads therefore cover
        # transitions before and during this region without enumerating producers.
        run = ctx.runner.get_run(run_id)
        if run is None:
            return {"success": False, "error": f"Agent run {run_id} not found"}
        terminal = run if run.status in agents._TERMINAL_AGENT_STATUSES else None
        if terminal is None:
            try:
                subscription = subscribe_agent_completion(
                    completion_registry=ctx.completion_registry,
                    run_id=run_id,
                    subscriber_session_id=session_id,
                    db=ctx.db,
                    strict=True,
                )
            except SubscriptionPersistenceError:
                return {
                    "success": False,
                    "error": "Failed to persist agent completion subscription",
                    "error_code": "subscription_persistence_failed",
                }

            latest_run = ctx.runner.get_run(run_id)
            terminal = (
                latest_run
                if latest_run is not None and latest_run.status in agents._TERMINAL_AGENT_STATUSES
                else None
            )
            if terminal is not None and subscription.created_fresh_entry:
                remove_agent_completion_subscribers(
                    db=ctx.db,
                    run_id=run_id,
                    session_ids=subscription.inserted_session_ids,
                )
                ctx.completion_registry.cleanup(run_id)
        # ---- end of no-await critical region ----

        if terminal is not None:
            payload = _agent_result_payload(
                await overlay_live_activity(terminal, ctx.transcript_reader),
                include_prompt=False,
            )
            return {
                "success": True,
                "completed": True,
                "notification_registered": False,
                **payload,
            }
        return {
            "success": True,
            "completed": False,
            "notification_registered": True,
            "notification_session_id": session_id,
            **payload,
        }

    @registry.tool(
        name="wait_for_output",
        description="Block until an agent's terminal output matches a bounded regular expression.",
    )
    async def wait_for_output(
        run_id: str,
        pattern: str,
        timeout_seconds: float = MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS,
        poll_interval_seconds: float = 2.0,
    ) -> dict[str, Any]:
        agents = facade()
        run = ctx.runner.get_run(run_id)
        if run is None:
            return _wait_for_output_error("invalid_run", f"Agent run {run_id} not found")
        if not run.terminal_id:
            return _wait_for_output_error("no_terminal", f"Agent run {run_id} has no terminal")

        try:
            compiled_pattern = compile_safe_regex(pattern)
        except InvalidPatternError as exc:
            return _wait_for_output_error("invalid_pattern", str(exc))

        timeout_value, error = _finite_number(timeout_seconds, name="timeout_seconds")
        if error is not None:
            return error
        interval_value, error = _finite_number(
            poll_interval_seconds,
            name="poll_interval_seconds",
        )
        if error is not None:
            return error
        if timeout_value is None or interval_value is None:
            return _wait_for_output_error(
                "invalid_argument",
                "timeout_seconds and poll_interval_seconds must be numeric",
            )

        timeout = clamp_wait_tool_timeout(
            "wait_for_output",
            timeout_value,
            default=MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS,
        )
        interval = max(0.1, min(interval_value, 30.0))
        deadline = agents.time.monotonic() + timeout
        consecutive_capture_failures = 0
        from gobby.storage.terminals import TerminalManager
        from gobby.terminals.runtime import TerminalRuntime
        from gobby.terminals.tmux_runtime import TmuxTerminalRuntime

        db = getattr(ctx.runner, "database", None) or getattr(ctx.runner, "db", None)
        injected_manager = getattr(ctx.runner, "terminal_manager", None)
        terminal_manager = injected_manager or (TerminalManager(db) if db is not None else None)
        registry = getattr(ctx.runner, "terminal_runtime_registry", None)
        runtime: TerminalRuntime | None = None

        while True:
            pane_output: str | None = None
            capture_failed = False
            try:
                terminal = (
                    None
                    if terminal_manager is None or not run.terminal_id
                    else terminal_manager.get(run.terminal_id)
                )
                if terminal is None:
                    capture_failed = True
                else:
                    if registry is not None:
                        runtime = registry.resolve(terminal.backend)
                    elif runtime is None:
                        runtime = TmuxTerminalRuntime(get_tmux_session_manager())
                    snapshot = await runtime.snapshot(terminal, _WAIT_OUTPUT_CAPTURE_LINES)
                    pane_output = snapshot.text
                    capture_failed = pane_output is None
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Failed to capture agent terminal output",
                    extra={
                        "run_id": run_id,
                        "terminal_id": run.terminal_id,
                    },
                    exc_info=True,
                )
                capture_failed = True

            if pane_output is not None:
                consecutive_capture_failures = 0
                match = compiled_pattern.search(pane_output)
                if match.outcome is RegexOutcome.PATTERN_TIMEOUT:
                    return _wait_for_output_error(
                        "pattern_timeout",
                        "pattern execution exceeded its time budget",
                    )
                if match.matched:
                    return {
                        "success": True,
                        "matched": True,
                        "excerpt": _bounded_wait_output_excerpt(pane_output),
                    }

            if run.status in agents._TERMINAL_AGENT_STATUSES:
                return {
                    "success": True,
                    "matched": False,
                    "reason": "terminal",
                    "status": run.status,
                }

            if capture_failed:
                try:
                    terminal = (
                        None
                        if terminal_manager is None or not run.terminal_id
                        else terminal_manager.get(run.terminal_id)
                    )
                    if terminal is None:
                        pane_exists = False
                    else:
                        if runtime is None:
                            if registry is not None:
                                runtime = registry.resolve(terminal.backend)
                            else:
                                runtime = TmuxTerminalRuntime(get_tmux_session_manager())
                        pane_exists = await runtime.is_live(terminal)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "Failed to check agent terminal session",
                        extra={
                            "run_id": run_id,
                            "terminal_id": run.terminal_id,
                        },
                        exc_info=True,
                    )
                    pane_exists = True
                if not pane_exists:
                    return {
                        "success": True,
                        "matched": False,
                        "reason": "pane_lost",
                        "status": run.status,
                    }
                consecutive_capture_failures += 1
                if consecutive_capture_failures >= _WAIT_OUTPUT_CAPTURE_FAILURE_LIMIT:
                    return _wait_for_output_error(
                        "capture_failed",
                        "terminal capture failed three consecutive times",
                    )

            remaining = deadline - agents.time.monotonic()
            if remaining <= 0:
                return {
                    "success": True,
                    "matched": False,
                    "reason": "timeout",
                    "status": run.status,
                }

            await agents.asyncio.sleep(min(interval, remaining))
            run = ctx.runner.get_run(run_id)
            if run is None:
                return _wait_for_output_error("invalid_run", f"Agent run {run_id} not found")
            if not run.terminal_id:
                return {
                    "success": True,
                    "matched": False,
                    "reason": "pane_lost",
                    "status": run.status,
                }

    @registry.tool(
        name="list_agent_runs",
        description=(
            "List agent runs for a session. Defaults to current session. "
            "Accepts #N, N, UUID, or prefix for session_id."
        ),
    )
    async def list_agent_runs(
        parent_session_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        limit = _clamp_limit(limit)
        effective_parent_ref = parent_session_id or ctx.get_current_session_id()
        if not effective_parent_ref:
            return {
                "success": False,
                "error": "No parent_session_id provided and no context available",
            }

        try:
            resolved_parent_id = ctx.resolve_session_id(effective_parent_ref)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        runs = ctx.runner.list_runs(resolved_parent_id, status=status, limit=limit)

        return {
            "success": True,
            "runs": [_list_run_payload(run) for run in runs],
            "count": len(runs),
        }

    @registry.tool(
        name="can_spawn_agent",
        description=(
            "Check if an agent can be spawned. Defaults to checking for the current session. "
            "Accepts #N, N, UUID, or prefix for session_id."
        ),
    )
    async def can_spawn_agent(parent_session_id: str | None = None) -> dict[str, Any]:
        effective_parent_ref = parent_session_id or ctx.get_current_session_id()
        if not effective_parent_ref:
            return {
                "success": False,
                "can_spawn": False,
                "reason": "No parent_session_id provided and no context available",
            }

        try:
            resolved_parent_id = ctx.resolve_session_id(effective_parent_ref)
        except ValueError as e:
            return {"success": False, "can_spawn": False, "reason": str(e)}

        can_spawn, reason, _parent_depth = ctx.runner.can_spawn(resolved_parent_id)
        return {
            "success": True,
            "can_spawn": can_spawn,
            "reason": reason,
        }

    @registry.tool(
        name="list_running_agents",
        description=(
            "List active agent runs. Defaults to build-wide scope. Pass "
            "scope='parent' or parent_session_id to filter by parent session; "
            "pass status='running' to match `gobby agents runs list --status running`."
        ),
    )
    async def list_running_agents(
        parent_session_id: str | None = None,
        scope: str = "all",
        status: str = "active",
        limit: int = 100,
    ) -> dict[str, Any]:
        limit = _clamp_limit(limit)
        scope_key = scope.strip().lower().replace("_", "-")
        if scope_key in {"build", "build-wide"}:
            scope_key = "all"
        if parent_session_id is not None or scope_key == "current":
            scope_key = "parent"
        if scope_key not in {"all", "parent"}:
            return {
                "success": False,
                "error": "scope must be one of: all, build, build-wide, parent, current",
            }

        status_key = status.strip().lower()
        if status_key not in {"active", "pending", "running"}:
            return {"success": False, "error": "status must be one of: active, pending, running"}

        resolved_parent_id = None
        if scope_key == "parent":
            effective_parent_ref = parent_session_id or ctx.get_current_session_id()
            if not effective_parent_ref:
                return {
                    "success": False,
                    "error": "No parent_session_id or session context available",
                }
            try:
                resolved_parent_id = ctx.resolve_session_id(effective_parent_ref)
            except ValueError as e:
                return {"success": False, "error": str(e)}
            if status_key == "active":
                runs = ctx.agent_run_manager.list_by_parent(resolved_parent_id, limit=limit)
            else:
                runs = ctx.agent_run_manager.list_by_parent(
                    resolved_parent_id,
                    limit=limit,
                    status=cast(AgentRunStatus, status_key),
                )
        elif status_key == "active":
            runs = ctx.agent_run_manager.list_active_global(limit=limit)
        elif status_key == "running":
            runs = ctx.agent_run_manager.list_running(limit=limit)
        else:
            runs = ctx.agent_run_manager.list_by_status(status="pending", limit=limit)
        runs = await overlay_runs_live_activity(runs, ctx.transcript_reader)

        return {
            "success": True,
            "agents": [_list_run_payload(run) for run in runs],
            "count": len(runs),
            "scope": scope_key,
            "status": status_key,
            "parent_session_id": resolved_parent_id,
        }

    @registry.tool(
        name="get_running_agent",
        description="Get process state for a running agent.",
    )
    async def get_running_agent(
        run_id: str,
        include_resume_metadata: bool = False,
    ) -> dict[str, Any]:
        validated_ref = _validated_run_ref(run_id)
        if validated_ref is None:
            return _invalid_run_ref(
                "run_id must be a UUID or a hexadecimal prefix of at least 8 characters",
                run_id=run_id,
            )

        run_ref, is_full_id = validated_ref
        if is_full_id:
            run = ctx.agent_run_manager.get(run_ref)
        else:
            matches = ctx.agent_run_manager.find_by_id_prefix(run_ref, limit=2)
            if len(matches) > 1:
                return _invalid_run_ref(
                    f"Ambiguous agent run ID prefix: {run_ref}",
                    run_id=run_id,
                    matches=[match.id for match in matches],
                )
            run = matches[0] if matches else None

        if not run or run.status not in ("running", "pending"):
            return {"success": False, "error": f"No running agent found with ID {run_id}"}
        run = await overlay_live_activity(run, ctx.transcript_reader)

        agent = run.to_dict()
        if not include_resume_metadata:
            agent.pop("resume_metadata_json", None)
        return {"success": True, "agent": agent}

    @registry.tool(
        name="unregister_agent",
        description="Mark an active agent run as cancelled/unregistered (internal use).",
    )
    async def unregister_agent(run_id: str) -> dict[str, Any]:
        from gobby.agents.terminal_delivery import (
            deliver_existing_terminal_run,
            run_terminal_delivery_offload,
        )

        run = ctx.agent_run_manager.get(run_id)
        if run and run.status in ("running", "pending"):
            if ctx.runner.cancel_run(run_id):
                if ctx.db is not None:
                    await deliver_existing_terminal_run(
                        db=ctx.db,
                        agent_run_manager=ctx.agent_run_manager,
                        completion_registry=ctx.completion_registry,
                        run_id=run_id,
                        run_db=run_terminal_delivery_offload,
                    )
                return {"success": True, "message": f"Unregistered agent {run_id}"}
            run = ctx.agent_run_manager.get(run_id)
        if run and run.status in facade()._TERMINAL_AGENT_STATUSES and ctx.db is not None:
            await deliver_existing_terminal_run(
                db=ctx.db,
                agent_run_manager=ctx.agent_run_manager,
                completion_registry=ctx.completion_registry,
                run_id=run_id,
                run_db=run_terminal_delivery_offload,
            )
        if run:
            return {"success": True, "message": f"Agent {run_id} already in status {run.status}"}
        return {"success": False, "error": f"No agent found with ID {run_id}"}

    @registry.tool(
        name="running_agent_stats",
        description="Get statistics about running agents.",
    )
    async def running_agent_stats() -> dict[str, Any]:
        all_runs = ctx.agent_run_manager.list_active_global()
        by_parent: dict[str, int] = {}

        for run in all_runs:
            by_parent[run.parent_session_id] = by_parent.get(run.parent_session_id, 0) + 1

        return {
            "success": True,
            "total": len(all_runs),
            "by_parent_count": len(by_parent),
        }
