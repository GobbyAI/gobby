"""
Simple runner for Gobby daemon.

Runs HTTP server, WebSocket server, and MCP connections as a long-running async process.
Local-first version: uses SQLite storage instead of platform APIs.
"""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

os.environ["TOKENIZERS_PARALLELISM"] = "false"

from gobby.config.app import load_config
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.servers.http import HTTPServer
from gobby.servers.websocket import WebSocketConfig, WebSocketServer
from gobby.sessions.lifecycle import SessionLifecycleManager
from gobby.sessions.processor import SessionMessageProcessor
from gobby.storage.database import LocalDatabase
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.migrations import run_migrations
from gobby.storage.sessions import LocalSessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskSyncManager
from gobby.utils.logging import setup_file_logging, setup_mcp_logging
from gobby.utils.machine_id import get_machine_id

logger = logging.getLogger(__name__)


class GobbyRunner:
    """Runner for Gobby daemon."""

    def __init__(self, config_path: Path | None = None, verbose: bool = False):
        setup_file_logging(verbose=verbose)
        setup_mcp_logging(verbose=verbose)

        config_file = str(config_path) if config_path else None
        self.config = load_config(config_file)
        self.verbose = verbose
        self.machine_id = get_machine_id()
        self._shutdown_requested = False

        # Initialize local storage
        self.database = LocalDatabase()
        run_migrations(self.database)
        self.session_manager = LocalSessionManager(self.database)

        # MCP database manager (stores servers and tools in SQLite)
        self.mcp_db_manager = LocalMCPManager(self.database)
        self.task_manager = LocalTaskManager(self.database)
        self.task_sync_manager = TaskSyncManager(self.task_manager)

        # Wire up change listener for automatic export
        self.task_manager.add_change_listener(self.task_sync_manager.trigger_export)

        self.mcp_proxy = MCPClientManager(
            mcp_db_manager=self.mcp_db_manager,
        )

        # Configured WebSocket (created later if enabled)
        self.websocket_server = None

        # Message Processor
        self.message_processor = None
        if self.config.message_tracking.enabled:
            # We pass None for websocket_server initially, will attach later if enabled
            self.message_processor = SessionMessageProcessor(
                db=self.database,
                poll_interval=self.config.message_tracking.poll_interval,
            )

        # Session Lifecycle Manager (background jobs for expiring and processing)
        self.lifecycle_manager = SessionLifecycleManager(
            db=self.database,
            config=self.config.session_lifecycle,
        )

        # HTTP server with local session storage
        self.http_server = HTTPServer(
            port=self.config.daemon_port,
            mcp_manager=self.mcp_proxy,
            config=self.config,
            session_manager=self.session_manager,
            task_manager=self.task_manager,
            task_sync_manager=self.task_sync_manager,
        )

        # Share message processor with HTTP server (for HookManager injection)
        # Note: HTTPServer doesn't accept message_processor in init, but we can attach it to app state later
        # or update HTTPServer to accept it.
        # For now, let's inject it into app.state in run() after app creation,
        # BUT HookManager is created in lifespan handler in HTTPServer._create_app.
        # So we should probably pass it to HTTPServer constructor.
        # Let's update HTTPServer constructor in next step. For now, just keep reference.
        self.http_server.message_processor = self.message_processor

        # WebSocket server (optional)
        if self.config.websocket and getattr(self.config.websocket, "enabled", True):
            websocket_config = WebSocketConfig(
                host="localhost",
                port=self.config.websocket.port,
                ping_interval=self.config.websocket.ping_interval,
                ping_timeout=self.config.websocket.ping_timeout,
            )
            self.websocket_server = WebSocketServer(
                config=websocket_config,
                mcp_manager=self.mcp_proxy,
            )
            # Pass WebSocket server reference to HTTP server for broadcasting
            self.http_server.websocket_server = self.websocket_server

            # Pass WebSocket server to message processor if enabled
            if self.message_processor:
                self.message_processor.websocket_server = self.websocket_server

    def _setup_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: setattr(self, "_shutdown_requested", True))

    async def run(self) -> None:
        try:
            self._setup_signal_handlers()

            # Connect MCP servers
            try:
                await asyncio.wait_for(self.mcp_proxy.connect_all(), timeout=10.0)
            except TimeoutError:
                logger.warning("MCP connection timed out")
            except Exception as e:
                logger.error(f"MCP connection failed: {e}")

            # Start Message Processor
            if self.message_processor:
                await self.message_processor.start()

            # Start Session Lifecycle Manager
            await self.lifecycle_manager.start()

            # Start WebSocket server
            websocket_task = None
            if self.websocket_server:
                websocket_task = asyncio.create_task(self.websocket_server.start())

            # Start HTTP server
            import uvicorn

            config = uvicorn.Config(
                self.http_server.app,
                host="0.0.0.0",
                port=self.http_server.port,
                log_level="warning",
                access_log=False,
            )
            server = uvicorn.Server(config)
            server_task = asyncio.create_task(server.serve())

            # Wait for shutdown
            while not self._shutdown_requested:
                await asyncio.sleep(0.5)

            # Cleanup
            server.should_exit = True
            await server_task

            if self.message_processor:
                await self.message_processor.stop()

            await self.lifecycle_manager.stop()

            if websocket_task:
                websocket_task.cancel()
                try:
                    await websocket_task
                except asyncio.CancelledError:
                    pass

            await self.mcp_proxy.disconnect_all()  # Changed from self.mcp_manager to self.mcp_proxy

        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            sys.exit(1)


async def run_gobby(config_path: Path | None = None, verbose: bool = False) -> None:
    runner = GobbyRunner(config_path=config_path, verbose=verbose)
    await runner.run()


def main(config_path: Path | None = None, verbose: bool = False) -> None:
    try:
        asyncio.run(run_gobby(config_path=config_path, verbose=verbose))
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run Gobby daemon")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--config", type=Path, help="Path to config file")

    args = parser.parse_args()
    main(config_path=args.config, verbose=args.verbose)
