"""WebSocket broadcasting setup for GobbyRunner.

Registers callbacks that forward agent lifecycle, pipeline, and cron events
to WebSocket clients. Extracted from runner.py to reduce file size.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from gobby.scheduler.scheduler import CronScheduler
    from gobby.servers.websocket.server import WebSocketServer
    from gobby.workflows.pipeline_executor import PipelineExecutor

logger = logging.getLogger(__name__)

RunDbHook = Callable[..., Awaitable[Any]] | Callable[..., Any]


class CommunicationsEventBroadcaster(Protocol):
    """WebSocket surface used by communications event fan-out."""

    async def broadcast_communications_event(
        self,
        *,
        event: str,
        **kwargs: Any,
    ) -> None: ...


# Module-level reference so broadcast_agent_event can be called directly
# from spawn and completion paths without going through the registry.
_agent_event_callback: Any | None = None
_agent_broadcast_tasks: set[asyncio.Task[None]] = set()


def _schedule_agent_broadcast(
    coroutine: Coroutine[Any, Any, None],
    *,
    event_type: str,
) -> None:
    """Schedule and retain an agent broadcast until its completion callback runs."""
    task = asyncio.create_task(coroutine)
    _agent_broadcast_tasks.add(task)

    def _on_done(completed_task: asyncio.Task[None]) -> None:
        _agent_broadcast_tasks.discard(completed_task)
        try:
            completed_task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("Failed to broadcast agent event %s: %s", event_type, exc)

    task.add_done_callback(_on_done)


@dataclass(frozen=True, slots=True)
class PipelineTerminalPayload:
    execution_id: str
    data: dict[str, Any]


def _callable_accepts_keyword(callback: Callable[..., Any], keyword: str) -> bool:
    """Return whether ``callback`` can be called with ``keyword=...``."""
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return True
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if (
            parameter.kind
            in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            and parameter.name == keyword
        ):
            return True
    return False


async def _dispatch_pipeline_terminal_event(
    run_db: RunDbHook | None,
    dispatch: Callable[..., Any],
    payload: PipelineTerminalPayload,
    *,
    db: Any,
) -> None:
    """Run terminal pipeline hooks through run_db when available."""

    def invoke_run_db(hook: Callable[..., Any]) -> Any:
        if _callable_accepts_keyword(hook, "db"):
            return hook(dispatch, payload, db=db)
        return hook(dispatch, payload)

    def invoke_dispatch() -> Any:
        if _callable_accepts_keyword(dispatch, "db"):
            return dispatch(payload, db=db)
        return dispatch(payload)

    if callable(run_db):
        is_async = inspect.iscoroutinefunction(run_db) or inspect.iscoroutinefunction(
            type(run_db).__call__
        )
        result = (
            invoke_run_db(run_db) if is_async else await asyncio.to_thread(invoke_run_db, run_db)
        )
    else:
        is_async_dispatch = inspect.iscoroutinefunction(dispatch) or inspect.iscoroutinefunction(
            type(dispatch).__call__
        )
        result = (
            invoke_dispatch() if is_async_dispatch else await asyncio.to_thread(invoke_dispatch)
        )
    if inspect.isawaitable(result):
        await result


def setup_agent_event_broadcasting(websocket_server: WebSocketServer) -> None:
    """Set up WebSocket broadcasting for agent lifecycle events, PTY reading, and tmux streaming."""
    from gobby.agents.pty_reader import get_pty_reader_manager
    from gobby.agents.tmux import get_tmux_output_reader

    pty_manager = get_pty_reader_manager()
    tmux_reader = get_tmux_output_reader()

    # Set up output callbacks to broadcast via WebSocket
    async def broadcast_terminal_output(run_id: str, data: str) -> None:
        """Broadcast terminal output via WebSocket."""
        if websocket_server:
            await websocket_server.broadcast_terminal_output(run_id, data)

    pty_manager.set_output_callback(broadcast_terminal_output)
    tmux_reader.set_output_callback(broadcast_terminal_output)

    def broadcast_agent_event(event_type: str, run_id: str, data: dict[str, Any]) -> None:
        """Broadcast agent events via WebSocket (non-blocking).

        Can be called directly — no registry dependency. The ``data`` dict
        should contain tmux_session_name, session_id, parent_session_id, etc.
        """
        if not websocket_server:
            return

        # Guard: this callback may be invoked from a sync context (e.g.,
        # session_coordinator.complete_agent_run via hook handler) where no
        # event loop is running.  All work below uses asyncio.create_task
        # which requires a running loop.  Silently skip if unavailable —
        # the broadcast is non-critical UI refreshing.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "Skipping agent event broadcast for %s/%s (no running event loop)",
                event_type,
                run_id,
            )
            return

        # Handle tmux output reader start for tmux terminal agents
        if event_type == "agent_started":
            tmux_name = data.get("tmux_session_name")
            if tmux_name:
                _tmux_name = tmux_name  # bind for closure

                async def start_tmux_reader() -> None:
                    await tmux_reader.start_reader(run_id, _tmux_name)

                _schedule_agent_broadcast(start_tmux_reader(), event_type=event_type)

                # Notify Terminals page so it auto-refreshes
                _ws = websocket_server

                async def broadcast_tmux_created() -> None:
                    if _ws:
                        await _ws.broadcast_tmux_session_event(
                            event="session_created",
                            session_name=_tmux_name,
                            socket="gobby",
                        )

                _schedule_agent_broadcast(broadcast_tmux_created(), event_type=event_type)

        elif event_type in (
            "agent_completed",
            "agent_failed",
            "agent_cancelled",
            "agent_timeout",
        ):
            # Stop PTY reader when agent finishes

            async def stop_pty_reader() -> None:
                await pty_manager.stop_reader(run_id)

            _schedule_agent_broadcast(stop_pty_reader(), event_type=event_type)

            # Stop tmux reader when agent finishes

            async def stop_tmux_reader() -> None:
                await tmux_reader.stop_reader(run_id)

            _schedule_agent_broadcast(stop_tmux_reader(), event_type=event_type)

            # Notify Terminals page so it auto-refreshes
            _killed_name = data.get("tmux_session_name")
            _ws_kill = websocket_server
            if _killed_name and _ws_kill:

                async def broadcast_tmux_killed() -> None:
                    await _ws_kill.broadcast_tmux_session_event(
                        event="session_killed",
                        session_name=_killed_name,
                        socket="gobby",
                    )

                _schedule_agent_broadcast(broadcast_tmux_killed(), event_type=event_type)

        forwarded = {
            key: value
            for key, value in data.items()
            if key
            not in {
                "run_id",
                "parent_session_id",
                "session_id",
                "mode",
                "provider",
                "pid",
                "tmux_session_name",
            }
        }

        # Create async task to broadcast and attach exception callback
        _schedule_agent_broadcast(
            websocket_server.broadcast_agent_event(
                event=event_type,
                run_id=run_id,
                parent_session_id=data.get("parent_session_id", ""),
                session_id=data.get("session_id"),
                mode=data.get("mode"),
                provider=data.get("provider"),
                pid=data.get("pid"),
                tmux_session_name=data.get("tmux_session_name"),
                **forwarded,
            ),
            event_type=event_type,
        )

    # Store module-level reference for direct invocation from spawn/completion paths
    global _agent_event_callback
    _agent_event_callback = broadcast_agent_event

    logger.debug("Agent event broadcasting and PTY reading enabled")


def reset_agent_event_broadcasting() -> None:
    """Remove lifecycle-owned callbacks published during runner construction."""
    from gobby.agents.pty_reader import reset_pty_output_callback
    from gobby.agents.tmux import reset_tmux_output_callback

    global _agent_event_callback
    _agent_event_callback = None
    reset_pty_output_callback()
    reset_tmux_output_callback()


def fire_agent_event(event_type: str, run_id: str, data: dict[str, Any]) -> None:
    """Fire an agent lifecycle event for broadcasting.

    Call this from spawn code (agent_started) and completion code
    (agent_completed, agent_failed, etc.) to trigger WebSocket broadcasts.
    No-op if broadcasting hasn't been set up yet.
    """
    if _agent_event_callback is not None:
        _agent_event_callback(event_type, run_id, data)


def setup_pipeline_event_broadcasting(
    websocket_server: WebSocketServer,
    pipeline_executor: PipelineExecutor,
) -> None:
    """Set up WebSocket broadcasting for pipeline execution events."""

    async def broadcast_pipeline_event(event: str, execution_id: str, **kwargs: Any) -> None:
        """Broadcast pipeline events via WebSocket."""
        if event in {"pipeline_completed", "pipeline_failed", "pipeline_cancelled"}:
            from gobby.hooks.event_handlers import _dispatch

            payload = PipelineTerminalPayload(execution_id=execution_id, data=dict(kwargs))
            db = getattr(pipeline_executor, "db", None)
            # run_db may be an async callable object; inspect the returned value.
            run_db: RunDbHook | None = getattr(pipeline_executor, "run_db", None)
            try:
                if event == "pipeline_completed":
                    dispatch = _dispatch.on_pipeline_completed
                elif event == "pipeline_failed":
                    dispatch = _dispatch.on_pipeline_failed
                else:
                    dispatch = _dispatch.on_pipeline_cancelled

                await _dispatch_pipeline_terminal_event(run_db, dispatch, payload, db=db)
            except Exception as exc:
                logger.warning(
                    "Pipeline terminal dispatch handler raised for %s execution_id=%s: %s",
                    event,
                    execution_id,
                    exc,
                    exc_info=True,
                )
        if websocket_server:
            await websocket_server.broadcast_pipeline_event(
                event=event,
                execution_id=execution_id,
                **kwargs,
            )

    # Set the callback on the pipeline executor
    pipeline_executor.event_callback = broadcast_pipeline_event
    logger.debug("Pipeline event broadcasting enabled")


def setup_cron_event_broadcasting(
    websocket_server: WebSocketServer,
    cron_scheduler: CronScheduler,
) -> None:
    """Set up WebSocket broadcasting for cron run completion events."""
    from gobby.storage.cron_models import CronJob, CronRun

    async def on_run_complete(job: CronJob, run: CronRun) -> None:
        """Broadcast cron run completion via WebSocket."""
        if websocket_server:
            event_by_status = {
                "completed": "run_completed",
                "failed": "run_failed",
                "skipped": "run_skipped",
                "dispatched": "run_dispatched",
            }
            event = event_by_status.get(run.status)
            if event is None:
                logger.warning(
                    "Unknown cron run status %s for run %s; broadcasting run_unknown",
                    run.status,
                    run.id,
                )
                event = "run_unknown"
            await websocket_server.broadcast_cron_event(
                event=event,
                job_id=job.id,
                run_id=run.id,
                job_name=job.name,
                status=run.status,
                run=run.to_dict(),
            )

    cron_scheduler.on_run_complete = on_run_complete
    logger.debug("Cron event broadcasting enabled")


def setup_communications_event_broadcasting(
    websocket_server: CommunicationsEventBroadcaster | None,
    communications_manager: Any,
) -> None:
    """Fan communications events out to WebSocket clients and the responder."""

    async def broadcast_comms_event(event: str, **kwargs: Any) -> None:
        """Broadcast communications events and dispatch inbound responder work."""
        consumers: list[tuple[str, Awaitable[None]]] = []
        if websocket_server:
            # message can be a dict or a Pydantic model
            # We convert to dict if needed to ensure JSON serializability
            safe_kwargs = {}
            for k, v in kwargs.items():
                if hasattr(v, "model_dump"):
                    safe_kwargs[k] = v.model_dump()
                elif hasattr(v, "__dict__"):
                    safe_kwargs[k] = vars(v)
                else:
                    safe_kwargs[k] = v
            consumers.append(
                (
                    "websocket",
                    websocket_server.broadcast_communications_event(
                        event=event,
                        **safe_kwargs,
                    ),
                )
            )
        responder = getattr(communications_manager, "responder", None)
        if responder is not None:
            consumers.append(("responder", responder.handle_event(event, **kwargs)))

        if not consumers:
            return
        results = await asyncio.gather(
            *(consumer for _name, consumer in consumers),
            return_exceptions=True,
        )
        for (name, _consumer), result in zip(consumers, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "Communications %s consumer failed for %s: %s",
                    name,
                    event,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )

    communications_manager.event_callback = broadcast_comms_event
    logger.debug("Communications event fan-out enabled")
