import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import uvicorn

from gobby.config.app import load_config
from gobby.hooks.broadcaster import HookEventBroadcaster
from gobby.hooks.hook_manager import HookManager
from gobby.llm import LLMService, create_llm_service
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.memory.manager import MemoryManager
from gobby.memory.skills import SkillLearner
from gobby.servers.http import HTTPServer
from gobby.servers.websocket import WebSocketConfig, WebSocketServer
from gobby.sessions.lifecycle import SessionLifecycleManager
from gobby.sessions.processor import SessionMessageProcessor
from gobby.storage.database import LocalDatabase
from gobby.storage.messages import LocalMessageManager
from gobby.storage.migrations import run_migrations
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import LocalSessionManager
from gobby.storage.skills import LocalSkillManager
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.memories import MemorySyncManager
from gobby.sync.tasks import TaskSyncManager
from gobby.tasks.expansion import TaskExpander
from gobby.tasks.validation import TaskValidator
from gobby.utils.logging import setup_file_logging
from gobby.utils.machine_id import get_machine_id

os.environ["TOKENIZERS_PARALLELISM"] = "false"

logger = logging.getLogger(__name__)


class GobbyRunner:
    """Runner for Gobby daemon."""

    def __init__(self, config_path: Path | None = None, verbose: bool = False):
        setup_file_logging(verbose=verbose)
        # setup_mcp_logging(verbose=verbose) # Removed as per instruction

        config_file = str(config_path) if config_path else None
        self.config = load_config(config_file)
        self.verbose = verbose
        self.machine_id = get_machine_id()
        self._shutdown_requested = False

        # Initialize local storage
        self.database = LocalDatabase()
        run_migrations(self.database)
        self.session_manager = LocalSessionManager(self.database)
        self.skill_storage = LocalSkillManager(self.database)
        self.message_manager = LocalMessageManager(self.database)
        self.task_manager = LocalTaskManager(self.database)
        self.session_task_manager = SessionTaskManager(self.database)

        # Initialize LLM Service (needed for SkillLearner)
        self.llm_service: LLMService | None = None  # Added type hint
        try:
            self.llm_service = create_llm_service(self.config)
            logger.debug(f"LLM service initialized: {self.llm_service.enabled_providers}")
        except Exception as e:
            logger.error(f"Failed to initialize LLM service: {e}")

        # Initialize Memory & Skills
        self.memory_manager: MemoryManager | None = None  # Added type hint
        if hasattr(self.config, "memory"):
            try:
                self.memory_manager = MemoryManager(self.database, self.config.memory)
            except Exception as e:
                logger.error(f"Failed to initialize MemoryManager: {e}")

        self.skill_learner: SkillLearner | None = None  # Added type hint
        if hasattr(self.config, "skills") and self.llm_service:
            try:
                self.skill_learner = SkillLearner(
                    storage=self.skill_storage,
                    message_manager=self.message_manager,
                    llm_service=self.llm_service,
                    config=self.config.skills,
                )
            except Exception as e:
                logger.error(f"Failed to initialize SkillLearner: {e}")

        # MCP Proxy Manager - Initialize early for tool access
        # Pass memory manager to proxy manager so tools can access it (Phase 5)
        # We need the MCP DB manager for persistent tool storage
        self.mcp_db_manager = self.database  # Reuse main database for MCP storage

        self.mcp_proxy = MCPClientManager()

        # Hook Event Broadcaster
        # Check if websocket config exists first to avoid NoneType errors
        self.broadcaster = HookEventBroadcaster(websocket_server=None, config=self.config)

        # Task Sync Manager
        self.task_sync_manager = TaskSyncManager(self.task_manager)
        # Wire up change listener for automatic export
        self.task_manager.add_change_listener(self.task_sync_manager.trigger_export)

        # Initialize Memory Sync Manager (Phase 7) & Wire up listeners
        self.memory_sync_manager: MemorySyncManager | None = None  # Added type hint
        if hasattr(self.config, "memory_sync") and self.config.memory_sync.enabled:
            # Only if memory/skills are enabled
            if self.memory_manager:
                try:
                    self.memory_sync_manager = MemorySyncManager(
                        db=self.database,
                        memory_manager=self.memory_manager,
                        skill_manager=self.skill_storage,
                        config=self.config.memory_sync,
                    )
                    # Wire up listeners to trigger export on changes
                    # Access underlying storage for listener registration
                    self.memory_manager.storage.add_change_listener(
                        self.memory_sync_manager.trigger_export
                    )
                    self.skill_storage.add_change_listener(self.memory_sync_manager.trigger_export)
                    logger.debug("MemorySyncManager initialized and listeners attached")
                except Exception as e:
                    logger.error(f"Failed to initialize MemorySyncManager: {e}")

        # Session Message Processor (Phase 6)
        # Needs to be created before HookManager to register sessions
        self.message_processor: SessionMessageProcessor | None = None
        if getattr(self.config, "message_tracking", None) and self.config.message_tracking.enabled:
            self.message_processor = SessionMessageProcessor(
                db=self.database,
                poll_interval=self.config.message_tracking.poll_interval,
            )

        # Hook Manager
        self.hook_manager = HookManager(
            daemon_host="localhost",
            daemon_port=self.config.daemon_port,
            llm_service=self.llm_service,
            config=self.config,
            broadcaster=self.broadcaster,
            mcp_manager=self.mcp_proxy,
            message_processor=self.message_processor,
            memory_sync_manager=self.memory_sync_manager,
        )

        # Initialize Task Managers (Phase 7.1)
        self.task_expander: TaskExpander | None = None
        self.task_validator: TaskValidator | None = None

        if self.llm_service:
            task_expansion_config = getattr(self.config, "task_expansion", None)
            if task_expansion_config:
                try:
                    self.task_expander = TaskExpander(
                        llm_service=self.llm_service,
                        config=task_expansion_config,
                    )
                except Exception as e:
                    logger.error(f"Failed to initialize TaskExpander: {e}")

            task_validation_config = getattr(self.config, "task_validation", None)
            if task_validation_config:
                try:
                    self.task_validator = TaskValidator(
                        llm_service=self.llm_service,
                        config=task_validation_config,
                    )
                except Exception as e:
                    logger.error(f"Failed to initialize TaskValidator: {e}")

        # Session Lifecycle Manager (background jobs for expiring and processing)
        self.lifecycle_manager = SessionLifecycleManager(
            db=self.database,
            config=self.config.session_lifecycle,
        )

        # HTTP Server
        self.http_server = HTTPServer(
            port=self.config.daemon_port,
            mcp_manager=self.mcp_proxy,
            mcp_db_manager=self.database,
            config=self.config,
            session_manager=self.session_manager,
            task_manager=self.task_manager,
            task_sync_manager=self.task_sync_manager,
            message_manager=self.message_manager,
            memory_manager=self.memory_manager,
            skill_learner=self.skill_learner,
            llm_service=self.llm_service,
            message_processor=self.message_processor,
            memory_sync_manager=self.memory_sync_manager,
        )

        # Share message processor with HTTP server (for HookManager injection if needed)
        # HTTP Server already receives message_processor in init, but ensuring property is set:
        self.http_server.message_processor = self.message_processor

        # WebSocket Server (Optional)
        self.websocket_server: WebSocketServer | None = None
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

            # Pass WebSocket server to global broadcaster
            if self.broadcaster:
                self.broadcaster.websocket_server = self.websocket_server

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

            # Stop in reverse startup order
            await self.lifecycle_manager.stop()

            if self.message_processor:
                await self.message_processor.stop()

            if websocket_task:
                websocket_task.cancel()
                try:
                    await websocket_task
                except asyncio.CancelledError:
                    pass

            await self.mcp_proxy.disconnect_all()

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
