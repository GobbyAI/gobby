"""WebSocket broadcasting setup for GobbyRunner.

Registers callbacks that forward agent lifecycle and pipeline events
to WebSocket clients. Extracted from runner.py to reduce file size.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.servers.websocket.server import WebSocketServer
    from gobby.workflows.pipeline_executor import PipelineExecutor

logger = logging.getLogger(__name__)


def setup_agent_event_broadcasting(websocket_server: WebSocketServer) -> None:
    """Set up WebSocket broadcasting for agent lifecycle events, PTY reading, and tmux streaming."""
    from gobby.agents.pty_reader import get_pty_reader_manager
    from gobby.agents.registry import get_running_agent_registry
    from gobby.agents.tmux import get_tmux_output_reader

    registry = get_running_agent_registry()
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
        """Broadcast agent events via WebSocket (non-blocking)."""
        if not websocket_server:
            return

        def _log_broadcast_exception(task: asyncio.Task[None]) -> None:
            """Log exceptions from broadcast task to avoid silent failures."""
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"Failed to broadcast agent event {event_type}: {e}")

        # Handle PTY reader start/stop for agents with PTY (legacy)
        if event_type == "agent_started" and data.get("master_fd") is not None:
            agent = registry.get(run_id)
            if agent and agent.master_fd is not None:
                _agent = agent  # bind for closure type narrowing

                async def start_pty_reader() -> None:
                    await pty_manager.start_reader(_agent)

                task = asyncio.create_task(start_pty_reader())
                task.add_done_callback(_log_broadcast_exception)

        # Handle tmux output reader start for tmux terminal agents
        if event_type == "agent_started":
            agent = registry.get(run_id)
            if agent and agent.tmux_session_name:
                session_name = agent.tmux_session_name

                async def start_tmux_reader() -> None:
                    await tmux_reader.start_reader(run_id, session_name)

                task = asyncio.create_task(start_tmux_reader())
                task.add_done_callback(_log_broadcast_exception)

                # Notify Terminals page so it auto-refreshes
                _created_name = session_name
                _ws = websocket_server

                async def broadcast_tmux_created() -> None:
                    if _ws:
                        await _ws.broadcast_tmux_session_event(
                            event="session_created",
                            session_name=_created_name,
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

    registry.add_event_callback(broadcast_agent_event)
    logger.debug("Agent event broadcasting and PTY reading enabled")


def setup_pipeline_event_broadcasting(
    websocket_server: WebSocketServer,
    pipeline_executor: PipelineExecutor,
) -> None:
    """Set up WebSocket broadcasting for pipeline execution events."""

    async def broadcast_pipeline_event(event: str, execution_id: str, **kwargs: Any) -> None:
        """Broadcast pipeline events via WebSocket."""
        if websocket_server:
            await websocket_server.broadcast_pipeline_event(
                event=event,
                execution_id=execution_id,
                **kwargs,
            )

    # Set the callback on the pipeline executor
    pipeline_executor.event_callback = broadcast_pipeline_event
    logger.debug("Pipeline event broadcasting enabled")
