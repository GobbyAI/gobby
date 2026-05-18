"""WebSocket broadcasting setup for GobbyRunner.

Registers callbacks that forward agent lifecycle, pipeline, and cron events
to WebSocket clients. Extracted from runner.py to reduce file size.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.scheduler.scheduler import CronScheduler
    from gobby.servers.websocket.server import WebSocketServer
    from gobby.workflows.pipeline_executor import PipelineExecutor

logger = logging.getLogger(__name__)

RunDbHook = Callable[..., Awaitable[Any]] | Callable[..., Any]

# Module-level reference so broadcast_agent_event can be called directly
# from spawn and completion paths without going through the registry.
_agent_event_callback: Any | None = None


@dataclass(frozen=True, slots=True)
class PipelineTerminalPayload:
    execution_id: str
    data: dict[str, Any]


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
                f"Skipping agent event broadcast for {event_type}/{run_id} (no running event loop)",
            )
            return

        def _log_broadcast_exception(task: asyncio.Task[None]) -> None:
            """Log exceptions from broadcast task to avoid silent failures."""
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"Failed to broadcast agent event {event_type}: {e}")

        # Handle tmux output reader start for tmux terminal agents
        if event_type == "agent_started":
            tmux_name = data.get("tmux_session_name")
            if tmux_name:
                _tmux_name = tmux_name  # bind for closure

                async def start_tmux_reader() -> None:
                    await tmux_reader.start_reader(run_id, _tmux_name)

                task = asyncio.create_task(start_tmux_reader())
                task.add_done_callback(_log_broadcast_exception)

                # Notify Terminals page so it auto-refreshes
                _ws = websocket_server

                async def broadcast_tmux_created() -> None:
                    if _ws:
                        await _ws.broadcast_tmux_session_event(
                            event="session_created",
                            session_name=_tmux_name,
                            socket="gobby",
                        )

                task = asyncio.create_task(broadcast_tmux_created())
                task.add_done_callback(_log_broadcast_exception)

        elif event_type in (
            "agent_completed",
            "agent_failed",
            "agent_cancelled",
            "agent_timeout",
        ):
            # Stop PTY reader when agent finishes

            async def stop_pty_reader() -> None:
                await pty_manager.stop_reader(run_id)

            task = asyncio.create_task(stop_pty_reader())
            task.add_done_callback(_log_broadcast_exception)

            # Stop tmux reader when agent finishes

            async def stop_tmux_reader() -> None:
                await tmux_reader.stop_reader(run_id)

            task = asyncio.create_task(stop_tmux_reader())
            task.add_done_callback(_log_broadcast_exception)

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

                task = asyncio.create_task(broadcast_tmux_killed())
                task.add_done_callback(_log_broadcast_exception)

        # Create async task to broadcast and attach exception callback
        task = asyncio.create_task(
            websocket_server.broadcast_agent_event(
                event=event_type,
                run_id=run_id,
                parent_session_id=data.get("parent_session_id", ""),
                session_id=data.get("session_id"),
                mode=data.get("mode"),
                provider=data.get("provider"),
                pid=data.get("pid"),
                tmux_session_name=data.get("tmux_session_name"),
            )
        )
        task.add_done_callback(_log_broadcast_exception)

    # Store module-level reference for direct invocation from spawn/completion paths
    global _agent_event_callback
    _agent_event_callback = broadcast_agent_event

    logger.debug("Agent event broadcasting and PTY reading enabled")


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
            # run_db hooks may be async (daemon executor) or sync (tests/adapters).
            run_db: RunDbHook | None = getattr(pipeline_executor, "run_db", None)
            try:
                if event == "pipeline_completed":
                    dispatch = _dispatch.on_pipeline_completed
                elif event == "pipeline_failed":
                    dispatch = _dispatch.on_pipeline_failed
                else:
                    dispatch = _dispatch.on_pipeline_cancelled

                if inspect.iscoroutinefunction(run_db):
                    await run_db(dispatch, payload, db=db)
                elif callable(run_db):
                    await asyncio.to_thread(run_db, dispatch, payload, db=db)
                else:
                    await asyncio.to_thread(dispatch, payload, db=db)
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
            event = "run_completed" if run.status == "completed" else "run_failed"
            await websocket_server.broadcast_cron_event(
                event=event,
                job_id=job.id,
                run_id=run.id,
                job_name=job.name,
                status=run.status,
            )

    cron_scheduler.on_run_complete = on_run_complete
    logger.debug("Cron event broadcasting enabled")


def setup_communications_event_broadcasting(
    websocket_server: WebSocketServer,
    communications_manager: Any,
) -> None:
    """Set up WebSocket broadcasting for communications events."""

    async def broadcast_comms_event(event: str, **kwargs: Any) -> None:
        """Broadcast communications events via WebSocket."""
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
            await websocket_server.broadcast_communications_event(
                event=event,
                **safe_kwargs,
            )

    communications_manager.event_callback = broadcast_comms_event
    logger.debug("Communications event broadcasting enabled")
