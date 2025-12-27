"""
Hook Manager - Clean Coordinator for Claude Code Hooks.

This is the refactored HookManager that serves purely as a coordinator,
delegating all work to focused subsystems. It replaces the 5,774-line
God Object with a ~300-line routing layer.

Architecture:
    HookManager creates and coordinates subsystems:
    - Session-agnostic: DaemonClient, TranscriptProcessor
    - Session-scoped: SessionManager
    - Workflow-driven: WorkflowEngine handles session handoff via generate_handoff action

Example:
    ```python
    from gobby.hooks.hook_manager import HookManager

    manager = HookManager(
        daemon_host="localhost",
        daemon_port=8765
    )

    result = manager.execute(
        hook_type="session-start",
        input_data={"external_id": "abc123", ...}
    )
    ```
"""

import asyncio
import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from gobby.hooks.events import HookEvent, HookEventType, HookResponse
from gobby.sessions.manager import SessionManager
from gobby.sessions.summary import SummaryFileGenerator
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import LocalSessionManager
from gobby.utils.daemon_client import DaemonClient
from gobby.workflows.engine import WorkflowEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.loader import WorkflowLoader
from gobby.workflows.state_manager import WorkflowStateManager

# Backward-compatible alias
TranscriptProcessor = ClaudeTranscriptParser

if TYPE_CHECKING:
    from gobby.llm.service import LLMService


class HookManager:
    """
    Session-scoped coordinator for Claude Code hooks.

    Delegates all work to subsystems:
    - DaemonClient: HTTP communication with Gobby daemon
    - TranscriptProcessor: JSONL parsing and analysis
    - WorkflowEngine: Handles session handoff and LLM-powered summaries

    Attributes:
        daemon_host: Host for daemon communication
        daemon_port: Port for daemon communication
        log_file: Full path to log file
        logger: Configured logger instance
    """

    def __init__(
        self,
        daemon_host: str = "localhost",
        daemon_port: int = 8765,
        llm_service: "LLMService | None" = None,
        config: Any | None = None,
        log_file: str | None = None,
        log_max_bytes: int = 10 * 1024 * 1024,  # 10MB
        log_backup_count: int = 5,
        broadcaster: Any | None = None,
        mcp_manager: Any | None = None,
        message_processor: Any | None = None,
    ):
        """
        Initialize HookManager with subsystems.

        Args:
            daemon_host: Daemon host for communication
            daemon_port: Daemon port for communication
            llm_service: Optional LLMService for multi-provider support
            config: Optional DaemonConfig instance for feature configuration
            log_file: Full path to log file (default: ~/.gobby/logs/hook-manager.log)
            log_max_bytes: Max log file size before rotation
            log_backup_count: Number of backup log files
            broadcaster: Optional HookEventBroadcaster instance
            mcp_manager: Optional MCPClientManager instance
            message_processor: SessionMessageProcessor instance
        """
        self.daemon_host = daemon_host
        self.daemon_port = daemon_port
        self.daemon_url = f"http://{daemon_host}:{daemon_port}"
        self.log_file = log_file or str(Path.home() / ".gobby" / "logs" / "hook-manager.log")
        self.log_max_bytes = log_max_bytes
        self.log_backup_count = log_backup_count
        self.broadcaster = broadcaster
        self.mcp_manager = mcp_manager
        self._message_processor = message_processor

        # Capture event loop for thread-safe broadcasting (if running in async context)
        self._loop: asyncio.AbstractEventLoop | None
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        # Setup logging first
        self.logger = self._setup_logging()

        # Store LLM service
        self._llm_service = llm_service

        # Load configuration - prefer passed config over loading new one
        self._config = config
        if not self._config:
            try:
                from gobby.config.app import load_config

                self._config = load_config()
            except Exception as e:
                self.logger.error(
                    f"Failed to load config in HookManager, using defaults: {e}",
                    exc_info=True,
                )

        # Extract config values
        if self._config:
            health_check_interval = self._config.daemon_health_check_interval

        else:
            health_check_interval = 10.0

        # Create session-agnostic subsystems (shared across all sessions)
        self._daemon_client = DaemonClient(
            host=daemon_host,
            port=daemon_port,
            timeout=5.0,
            logger=self.logger,
        )
        self._transcript_processor = TranscriptProcessor(logger_instance=self.logger)

        # Create local storage for sessions
        self._database = LocalDatabase()
        run_migrations(self._database)
        self._session_storage = LocalSessionManager(self._database)
        self._session_task_manager = SessionTaskManager(self._database)

        # Initialize Workflow Engine (Phase 0-2 + 3 Integration)
        # Initialize Workflow Engine (Phase 0-2 + 3 Integration)
        from gobby.workflows.actions import ActionExecutor
        from gobby.workflows.templates import TemplateEngine

        # Workflow loader now handles project-specific paths dynamically via project_path parameter
        # Global workflows are loaded from ~/.gobby/workflows/
        # Project-specific workflows are loaded from {project_path}/.gobby/workflows/
        self._workflow_loader = WorkflowLoader()
        self._workflow_state_manager = WorkflowStateManager(self._database)

        # Initialize Template Engine
        # We can pass template directory from package templates or user templates
        # For now, let's include the built-in templates dir if we can find it
        # Assuming templates are in package 'gobby.templates.workflows'?
        # Or just use the one we are about to create in project root?
        # Ideally, we should look for templates in typical locations.
        # But 'TemplateEngine' constructor takes optional dirs.
        self._template_engine = TemplateEngine()

        self._action_executor = ActionExecutor(
            db=self._database,
            session_manager=self._session_storage,
            template_engine=self._template_engine,
            llm_service=self._llm_service,
            transcript_processor=self._transcript_processor,
            config=self._config,
            mcp_manager=self.mcp_manager,
        )
        self._workflow_engine = WorkflowEngine(
            loader=self._workflow_loader,
            state_manager=self._workflow_state_manager,
            action_executor=self._action_executor,
        )
        workflow_timeout = 30.0
        if self._config:
            workflow_timeout = self._config.workflow.timeout

        self._workflow_handler = WorkflowHookHandler(
            engine=self._workflow_engine, loop=self._loop, timeout=workflow_timeout
        )

        # Initialize Failover Summary Generator
        self._summary_file_generator = SummaryFileGenerator(
            transcript_processor=self._transcript_processor,
            logger_instance=self.logger,
            llm_service=self._llm_service,
            config=self._config,
        )

        # Session manager handles registration, lookup, and status updates
        # Note: source is passed explicitly per call (Phase 2C+), not stored in manager
        self._session_manager = SessionManager(
            session_storage=self._session_storage,
            logger_instance=self.logger,
            config=self._config,
        )

        # Session registration tracking (to avoid noisy logs)
        # Tracks which sessions have been registered with daemon
        self._registered_sessions: set[str] = set()
        self._registered_sessions_lock = threading.Lock()

        # Session title synthesis tracking
        # Tracks which sessions have had titles synthesized
        self._title_synthesized_sessions: set[str] = set()
        self._title_synthesized_lock = threading.Lock()

        # Agent message cache (session_id -> (message, timestamp))
        # Used to pass agent responses from stop hook to post-tool-use hook
        self._agent_message_cache: dict[str, tuple[str, float]] = {}
        self._cache_lock = threading.Lock()

        # Lock for session lookups to prevent race conditions (double firing)
        self._lookup_lock = threading.Lock()

        # Daemon health check monitoring
        self._cached_daemon_is_ready: bool = False
        self._cached_daemon_message: str | None = None
        self._cached_daemon_status: str = "not_running"
        self._cached_daemon_error: str | None = None
        self._health_check_interval = health_check_interval
        self._health_check_timer: threading.Timer | None = None
        self._health_check_lock = threading.Lock()
        self._is_shutdown: bool = False  # Guard to prevent timer reschedule after shutdown

        # Event handler map for unified HookEvent handling
        # Maps HookEventType enum values to handler methods
        self._event_handler_map: dict[HookEventType, Any] = {
            HookEventType.SESSION_START: self._handle_event_session_start,
            HookEventType.SESSION_END: self._handle_event_session_end,
            HookEventType.BEFORE_AGENT: self._handle_event_before_agent,
            HookEventType.AFTER_AGENT: self._handle_event_after_agent,
            HookEventType.BEFORE_TOOL: self._handle_event_before_tool,
            HookEventType.AFTER_TOOL: self._handle_event_after_tool,
            HookEventType.PRE_COMPACT: self._handle_event_pre_compact,
            HookEventType.SUBAGENT_START: self._handle_event_subagent_start,
            HookEventType.SUBAGENT_STOP: self._handle_event_subagent_stop,
            HookEventType.NOTIFICATION: self._handle_event_notification,
            # Gemini-only events (Phase 3)
            HookEventType.BEFORE_TOOL_SELECTION: self._handle_event_before_tool_selection,
            HookEventType.BEFORE_MODEL: self._handle_event_before_model,
            HookEventType.AFTER_MODEL: self._handle_event_after_model,
            # Claude Code only
            HookEventType.PERMISSION_REQUEST: self._handle_event_permission_request,
        }

        # Start background health check monitoring
        self._start_health_check_monitoring()

        self.logger.debug("HookManager initialized")

    def _setup_logging(self) -> logging.Logger:
        """
        Setup structured logging with rotation.

        Returns:
            Configured logger instance
        """
        # Create logger
        logger = logging.getLogger("gobby.hooks")
        logger.setLevel(logging.DEBUG)

        # Avoid duplicate handlers if logger already configured
        if logger.handlers:
            return logger

        # File handler with rotation - use full path from config
        # Expand ~ to home directory before creating directories
        log_file_path = Path(self.log_file).expanduser()
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file_path,
            maxBytes=self.log_max_bytes,
            backupCount=self.log_backup_count,
        )
        file_handler.setLevel(logging.DEBUG)

        # Formatter with context
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(formatter)

        logger.addHandler(file_handler)

        return logger

    def _start_health_check_monitoring(self) -> None:
        """Start background daemon health check monitoring."""
        with self._health_check_lock:
            if self._health_check_timer is not None:
                return  # Already running

            def health_check_loop() -> None:
                """Background health check loop."""
                try:
                    # Update daemon status cache
                    # check_status() returns tuple: (is_ready, message, status, error)
                    is_ready, message, status, error = self._daemon_client.check_status()
                    with self._health_check_lock:
                        self._cached_daemon_is_ready = is_ready
                        self._cached_daemon_message = message
                        self._cached_daemon_status = status
                        self._cached_daemon_error = error
                except Exception as e:
                    # Daemon not responding is expected when stopped, log at debug level
                    # but preserve stack trace for unexpected errors
                    self.logger.debug(f"Health check failed: {e}", exc_info=True)
                    with self._health_check_lock:
                        self._cached_daemon_is_ready = False
                        self._cached_daemon_status = "not_running"
                        self._cached_daemon_error = str(e)
                finally:
                    # Schedule next check only if not shutting down
                    with self._health_check_lock:
                        if not self._is_shutdown:
                            self._health_check_timer = threading.Timer(
                                self._health_check_interval,
                                health_check_loop,
                            )
                            self._health_check_timer.daemon = True
                            self._health_check_timer.start()

            # Start first check
            self._health_check_timer = threading.Timer(0, health_check_loop)
            self._health_check_timer.daemon = True
            self._health_check_timer.start()

    def _get_cached_daemon_status(self) -> tuple[bool, str | None, str, str | None]:
        """
        Get cached daemon status without making HTTP call.

        Returns:
            Tuple of (is_ready, message, status, error)
        """
        with self._health_check_lock:
            return (
                self._cached_daemon_is_ready,
                self._cached_daemon_message,
                self._cached_daemon_status,
                self._cached_daemon_error,
            )

    def handle(self, event: HookEvent) -> HookResponse:
        """
        Handle unified HookEvent from any CLI source.

        This is the main entry point for hook handling. Adapters translate
        CLI-specific payloads to HookEvent and call this method.

        Args:
            event: Unified HookEvent with event_type, session_id, source, and data.

        Returns:
            HookResponse with decision, context, and reason fields.

        Raises:
            ValueError: If event_type has no registered handler.
        """
        # Check daemon status (cached)
        is_ready, _, daemon_status, error_reason = self._get_cached_daemon_status()
        if not is_ready:
            self.logger.warning(
                f"Daemon not available, skipping hook execution: {event.event_type}. "
                f"Status: {daemon_status}, Error: {error_reason}"
            )
            return HookResponse(
                decision="allow",  # Fail-open
                reason=f"Daemon {daemon_status}: {error_reason or 'Unknown'}",
            )

        # Look up platform session_id from cli_key (event.session_id is the cli_key)
        external_id = event.session_id
        platform_session_id = None

        if external_id:
            # Check SessionManager's cache first
            platform_session_id = self._session_manager.get_session_id(external_id)

            # If not in mapping and not session-start, try to query database
            if not platform_session_id and event.event_type != HookEventType.SESSION_START:
                with self._lookup_lock:
                    # Double check in case another thread finished lookup
                    platform_session_id = self._session_manager.get_session_id(external_id)

                    if not platform_session_id:
                        self.logger.debug(
                            f"Session not in mapping, querying database for external_id={external_id}"
                        )
                        # Pass source for multi-CLI support
                        machine_id = event.machine_id or self.get_machine_id()
                        platform_session_id = self._session_manager.lookup_session_id(
                            external_id, source=event.source.value, machine_id=machine_id
                        )
                        if platform_session_id:
                            self.logger.debug(
                                f"Found session_id {platform_session_id} for external_id {external_id}"
                            )
                        else:
                            # Auto-register session if not found
                            self.logger.debug(
                                f"Session not found for external_id={external_id}, auto-registering"
                            )
                            # Resolve project_id from cwd
                            cwd = event.data.get("cwd")
                            project_id = self._resolve_project_id(event.data.get("project_id"), cwd)
                            platform_session_id = self._session_manager.register_session(
                                external_id=external_id,
                                machine_id=machine_id,
                                project_id=project_id,
                                parent_session_id=None,
                                jsonl_path=event.data.get("transcript_path"),
                                source=event.source.value,
                                project_path=cwd,
                            )

            # Resolve active task for this session if we have a platform session ID
            if platform_session_id:
                try:
                    # Get tasks linked with 'worked_on' action which implies active focus
                    session_tasks = self._session_task_manager.get_session_tasks(
                        platform_session_id
                    )
                    # Filter for active 'worked_on' tasks - taking the most recent one
                    active_tasks = [t for t in session_tasks if t.get("action") == "worked_on"]
                    if active_tasks:
                        # Use the most recent task
                        event.task_id = active_tasks[0]["task"].id
                        event.metadata["_task_title"] = active_tasks[0]["task"].title
                except Exception as e:
                    self.logger.warning(f"Failed to resolve active task: {e}")

            # Store platform session_id in event metadata for handlers
            event.metadata["_platform_session_id"] = platform_session_id

        # Get handler for this event type
        handler = self._get_event_handler(event.event_type)
        if handler is None:
            self.logger.warning(f"No handler for event type: {event.event_type}")
            return HookResponse(decision="allow")  # Fail-open for unknown events

        # --- Workflow Engine Evaluation (Phase 3) ---
        # Evalute workflow rules before executing specific handlers
        workflow_context = None
        try:
            workflow_response = self._workflow_handler.handle(event)

            # If workflow blocks or asks, return immediately
            if workflow_response.decision != "allow":
                self.logger.info(f"Workflow blocked/modified event: {workflow_response.decision}")
                return workflow_response

            # Capture context to merge later
            if workflow_response.context:
                workflow_context = workflow_response.context

        except Exception as e:
            self.logger.error(f"Workflow evaluation failed: {e}", exc_info=True)
            # Fail-open for workflow errors
        # --------------------------------------------

        # Execute handler
        try:
            response = handler(event)

            # Merge workflow context if present
            if workflow_context:
                if response.context:
                    response.context = f"{response.context}\n\n{workflow_context}"
                else:
                    response.context = workflow_context

            # Broadcast event (fire-and-forget)
            if self.broadcaster:
                try:
                    # Case 1: Running in an event loop (e.g. from app-server client)
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.broadcaster.broadcast_event(event, response))
                except RuntimeError:
                    # Case 2: Running in a thread (e.g. from HTTP endpoint via to_thread)
                    if self._loop:
                        try:
                            # Use the main loop captured at init
                            asyncio.run_coroutine_threadsafe(
                                self.broadcaster.broadcast_event(event, response),
                                self._loop,
                            )
                        except Exception as e:
                            self.logger.warning(f"Failed to schedule broadcast threadsafe: {e}")
                    else:
                        self.logger.debug("No event loop available for broadcasting")

            return response
        except Exception as e:
            self.logger.error(f"Event handler {event.event_type} failed: {e}", exc_info=True)
            # Fail-open on handler errors
            return HookResponse(
                decision="allow",
                reason=f"Handler error: {e}",
            )

    def _get_event_handler(self, event_type: HookEventType) -> Any | None:
        """
        Get the handler method for a given HookEventType.

        Args:
            event_type: The unified event type enum value.

        Returns:
            Handler method or None if not found.
        """
        return self._event_handler_map.get(event_type)

    def shutdown(self) -> None:
        """
        Clean up HookManager resources on daemon shutdown.

        Stops background health check monitoring and transcript watchers.
        """
        self.logger.debug("HookManager shutting down")

        # Stop health check monitoring
        with self._health_check_lock:
            self._is_shutdown = True  # Prevent new timer from being scheduled
            if self._health_check_timer is not None:
                self._health_check_timer.cancel()
                self._health_check_timer = None

        self.logger.debug("HookManager shutdown complete")

    # ==================== HELPER METHODS ====================

    def get_machine_id(self) -> str:
        """Get unique machine identifier."""
        from gobby.utils.machine_id import get_machine_id as _get_machine_id

        result: str = _get_machine_id()
        return result

    def _resolve_project_id(self, project_id: str | None, cwd: str | None) -> str:
        """
        Resolve project_id from cwd if not provided.

        If project_id is given, returns it directly.
        Otherwise, looks up project from .gobby/project.json in the cwd.
        If no project.json exists, automatically initializes the project.

        Args:
            project_id: Optional explicit project ID
            cwd: Current working directory path

        Returns:
            Project ID (existing or newly created)
        """
        if project_id:
            return project_id

        # Get cwd or use current directory
        working_dir = Path(cwd) if cwd else Path.cwd()

        # Look up project from .gobby/project.json
        from gobby.utils.project_context import get_project_context

        project_context = get_project_context(working_dir)
        if project_context and project_context.get("id"):
            return str(project_context["id"])

        # No project.json found - auto-initialize the project
        from gobby.utils.project_init import initialize_project

        result = initialize_project(cwd=working_dir)
        self.logger.info(f"Auto-initialized project '{result.project_name}' in {working_dir}")
        return cast(str, result.project_id)

    # ==================== EVENT HANDLERS ====================
    # These handlers work with unified HookEvent and return HookResponse.

    def _handle_event_session_start(self, event: HookEvent) -> HookResponse:
        """
        Handle SESSION_START event.
        Register session and execute session-handoff workflow.
        """
        external_id = event.session_id
        input_data = event.data
        transcript_path = input_data.get("transcript_path")
        cli_source = event.source.value
        cwd = input_data.get("cwd")

        # Resolve project_id (auto-creates if needed)
        project_id = self._resolve_project_id(input_data.get("project_id"), cwd)

        # Get machine ID
        machine_id = event.machine_id or self.get_machine_id()

        self.logger.debug(
            f"🟢 Session start: cli={cli_source}, project={project_id}, source={input_data.get('source')}"
        )

        # Step 1: Register new session (no parent initially)
        # The workflow will handle finding and linking parent via 'find_parent_session' action
        session_id = self._session_manager.register_session(
            external_id=external_id,
            machine_id=machine_id,
            project_id=project_id,
            parent_session_id=None,
            jsonl_path=transcript_path,
            source=cli_source,
            project_path=cwd,
        )

        # Step 2: Track registered session
        if transcript_path:
            try:
                with self._registered_sessions_lock:
                    self._registered_sessions.add(external_id)
            except Exception as e:
                self.logger.error(f"Failed to setup session tracking: {e}", exc_info=True)

        # Step 3: Update event metadata with the newly registered session_id
        # This is required so the workflow engine can access the session correctly
        event.metadata["_platform_session_id"] = session_id

        # Step 3.5: Register with Message Processor
        if self._message_processor and transcript_path:
            try:
                self._message_processor.register_session(session_id, transcript_path)
            except Exception as e:
                self.logger.warning(f"Failed to register session with message processor: {e}")

        # Step 4: Execute lifecycle workflows (discovers all workflows, evaluates triggers)
        # This handles: find_parent_session, restore_context, mark_session_status, etc.
        wf_response = self._workflow_handler.handle_all_lifecycles(event)

        # Step 5: Build response with context and system message
        context_parts = []
        context_parts.append(f"Session registered: {session_id}")
        system_message = None

        # Reload session to check if workflow linked a parent
        current_session = self._session_storage.get(session_id)
        if current_session and current_session.parent_session_id:
            context_parts.append(f"Parent session: {current_session.parent_session_id}")
            context_parts.append("Handoff completed successfully.")

            if wf_response.context:
                context_parts.append(wf_response.context)

            system_message = (
                f"⏺ Context restored from previous session.\n"
                f"  Session ID: {session_id}\n"
                f"  Parent ID: {current_session.parent_session_id}\n"
                f"  Project: {project_id}"
            )
        else:
            # Include any workflow context even if no parent linked (e.g. greeting)
            if wf_response.context:
                context_parts.append(wf_response.context)

        # Inject active task context if available
        if event.task_id:
            task_title = event.metadata.get("_task_title", "Unknown Task")
            context_parts.append("\n## Active Task Context\n")
            context_parts.append(f"You are working on task: {task_title} ({event.task_id})")

        return HookResponse(
            decision="allow",
            context="\n".join(context_parts) if context_parts else None,
            system_message=system_message,
            metadata={
                "session_id": session_id,
                "machine_id": machine_id,
                "external_id": external_id,
                "task_id": event.task_id,
            },
        )

    def _handle_event_session_end(self, event: HookEvent) -> HookResponse:
        """
        Handle SESSION_END event.

        Generates session summary and marks session as handoff_ready.
        Available triggers: on_session_end
        """
        external_id = event.session_id

        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug(f"🔴 Session end: session {session_id}")
        else:
            self.logger.warning(
                f"🔴 Session end: session_id not found for external_id={external_id}"
            )

        # If not in mapping, query database
        if not session_id and external_id:
            self.logger.debug(f"external_id {external_id} not in mapping, querying database")
            machine_id = event.machine_id or self.get_machine_id()
            session_id = self._session_manager.lookup_session_id(
                external_id, source=event.source.value, machine_id=machine_id
            )

        # Execute lifecycle workflow triggers (e.g. generate_handoff on /clear)
        self.logger.debug("Calling workflow handler for all lifecycle workflows on session_end")
        try:
            result = self._workflow_handler.handle_all_lifecycles(event)
            self.logger.debug(f"Workflow handler returned: {result}")
        except Exception as e:
            self.logger.error(
                f"Failed to execute lifecycle workflows on session_end: {e}",
                exc_info=True,
            )

        # FAILOVER: Generate independent session summary file
        # This acts as a flight recorder, independent of workflow success/failure
        self.logger.debug("Executing failover session summary generation")
        try:
            # We construct input data for SummaryFileGenerator
            summary_input = {
                "session_id": external_id,
                "transcript_path": event.data.get("transcript_path"),
            }
            summary_result = self._summary_file_generator.generate_session_summary(
                session_id=session_id or external_id,
                input_data=summary_input,
            )
            if summary_result.get("status") == "success":
                self.logger.info(
                    f"💾 Failover summary created: {summary_result.get('file_written')}"
                )
            else:
                self.logger.warning(f"Failover summary skipped/failed: {summary_result}")
        except Exception as e:
            self.logger.error(f"Failed to generate failover summary: {e}")

        # Unregister from message processor
        if self._message_processor and (session_id or external_id):
            try:
                # We use whatever ID we found (preferably session_id)
                target_id = session_id or external_id
                self._message_processor.unregister_session(target_id)
            except Exception as e:
                self.logger.warning(f"Failed to unregister session from message processor: {e}")

        return HookResponse(decision="allow")

    def _handle_event_before_agent(self, event: HookEvent) -> HookResponse:
        """
        Handle BEFORE_AGENT event (user prompt submit).

        Synthesizes session title if null and updates status to active.
        Executes lifecycle workflow triggers for on_before_agent / on_prompt_submit.

        Args:
            event: HookEvent with prompt data

        Returns:
            HookResponse with optional context injection
        """
        input_data = event.data
        prompt = input_data.get("prompt", "")

        transcript_path = input_data.get("transcript_path")
        session_id = event.metadata.get("_platform_session_id")

        context_parts = []

        if session_id:
            self.logger.debug(f"💬 User prompt: session {session_id}")
            self.logger.debug(f"   Prompt: {prompt[:100]}...")

            # Synthesize title if null
            # (Features moved to workflows or removed in legacy cleanup)

            # Update status to active (unless /clear or /exit)
            prompt_lower = prompt.strip().lower()
            if prompt_lower not in ("/clear", "/exit"):
                try:
                    self._session_manager.update_session_status(session_id, "active")
                    # Reset transcript_processed so background job will re-process
                    # when session expires again (catches new messages)
                    self._session_storage.reset_transcript_processed(session_id)
                except Exception as e:
                    self.logger.warning(f"Failed to update session status: {e}")

            # Handle /clear command - prepare handoff
            # Note: The lifecycle workflows will handle this via on_before_agent triggers
            # with appropriate 'when' conditions (e.g., prompt == '/clear')
            if prompt_lower in ("/clear", "/exit") and transcript_path:
                self.logger.debug(
                    f"Detected {prompt_lower} command - lifecycle workflows will handle handoff"
                )

        # Execute all lifecycle workflow triggers for on_before_agent / on_prompt_submit
        # This allows workflows to inject phase context, handle /clear, check pending reflection, etc.
        try:
            wf_response = self._workflow_handler.handle_all_lifecycles(event)
            if wf_response.context:
                context_parts.append(wf_response.context)
            # If workflow blocks, return immediately
            if wf_response.decision != "allow":
                return wf_response
        except Exception as e:
            self.logger.error(
                f"Failed to execute lifecycle workflows on before_agent: {e}",
                exc_info=True,
            )

        return HookResponse(
            decision="allow",
            context="\n\n".join(context_parts) if context_parts else None,
        )

    def _handle_event_after_agent(self, event: HookEvent) -> HookResponse:
        """
        Handle AFTER_AGENT event (stop).

        Updates session status to paused.
        For Codex: synthesizes title on first event if prompt is available.

        Args:
            event: HookEvent with agent response data

        Returns:
            HookResponse (always allow)
        """
        session_id = event.metadata.get("_platform_session_id")
        cli_source = event.source.value

        if session_id:
            self.logger.debug(f"🛑 Agent stop: session {session_id}, cli={cli_source}")
            try:
                self._session_manager.update_session_status(session_id, "paused")
            except Exception as e:
                self.logger.warning(f"Failed to update session status: {e}")

            # Synthesize title on first event if prompt is available (for Codex)
            # (Features moved to workflows or removed in legacy cleanup)
        else:
            self.logger.debug(f"🛑 Agent stop: cli={cli_source}")

        return HookResponse(decision="allow")

    def _handle_event_before_tool(self, event: HookEvent) -> HookResponse:
        """
        Handle BEFORE_TOOL event (pre-tool-use).

        Logs tool usage and executes lifecycle workflow triggers for on_before_tool / on_tool_call.
        Workflows can validate tool permissions, evaluate rules, and check transitions.

        Args:
            event: HookEvent with tool_name and tool_input

        Returns:
            HookResponse (allow by default, or block/modify from workflow)
        """
        input_data = event.data
        tool_name = input_data.get("tool_name", "unknown")
        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug(f"🔧 Pre-tool: {tool_name}, session {session_id}")
        else:
            self.logger.debug(f"🔧 Pre-tool: {tool_name}")

        # Execute all lifecycle workflow triggers for on_before_tool / on_tool_call
        # This allows workflows to validate tool permissions, evaluate rules, block tools, etc.
        context_parts = []
        try:
            wf_response = self._workflow_handler.handle_all_lifecycles(event)
            if wf_response.context:
                context_parts.append(wf_response.context)
            # If workflow blocks or modifies, return immediately
            if wf_response.decision != "allow":
                self.logger.info(
                    f"Workflow {wf_response.decision} tool '{tool_name}': {wf_response.reason}"
                )
                return wf_response
        except Exception as e:
            self.logger.error(
                f"Failed to execute lifecycle workflows on before_tool: {e}",
                exc_info=True,
            )

        return HookResponse(
            decision="allow",
            context="\n\n".join(context_parts) if context_parts else None,
        )

    def _handle_event_after_tool(self, event: HookEvent) -> HookResponse:
        """
        Handle AFTER_TOOL event (post-tool-use).

        Logs tool execution results and executes lifecycle workflow triggers
        for on_after_tool / on_tool_result. Workflows can capture observations,
        update action counts, and check error transitions.

        Args:
            event: HookEvent with tool_name, tool_input, and tool_output

        Returns:
            HookResponse with optional context injection
        """
        input_data = event.data
        tool_name = input_data.get("tool_name", "unknown")
        session_id = event.metadata.get("_platform_session_id")

        # Check if this was a failure (from post-tool-use-failure)
        is_failure = event.metadata.get("is_failure", False)

        if session_id:
            status = "❌" if is_failure else "✅"
            self.logger.debug(f"{status} Post-tool: {tool_name}, session {session_id}")
        else:
            status = "❌" if is_failure else "✅"
            self.logger.debug(f"{status} Post-tool: {tool_name}")

        # Execute all lifecycle workflow triggers for on_after_tool / on_tool_result
        # This allows workflows to capture observations, update action counts,
        # check error transitions, etc.
        context_parts = []
        try:
            wf_response = self._workflow_handler.handle_all_lifecycles(event)
            if wf_response.context:
                context_parts.append(wf_response.context)
            # If workflow modifies response, return it
            if wf_response.decision != "allow":
                return wf_response
        except Exception as e:
            self.logger.error(
                f"Failed to execute lifecycle workflows on after_tool: {e}",
                exc_info=True,
            )

        return HookResponse(
            decision="allow",
            context="\n\n".join(context_parts) if context_parts else None,
        )

    def _handle_event_pre_compact(self, event: HookEvent) -> HookResponse:
        """
        Handle PRE_COMPACT event.

        Logs context compaction event.

        Args:
            event: HookEvent

        Returns:
            HookResponse (always allow)
        """
        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug(f"🗜️  Pre-compact: session {session_id}")
        else:
            self.logger.debug("🗜️  Pre-compact")

        return HookResponse(decision="allow")

    def _handle_event_subagent_start(self, event: HookEvent) -> HookResponse:
        """
        Handle SUBAGENT_START event.

        Logs subagent spawn event.

        Args:
            event: HookEvent with agent_id and subagent_id

        Returns:
            HookResponse (always allow)
        """
        input_data = event.data
        session_id = event.metadata.get("_platform_session_id")
        agent_id = input_data.get("agent_id")
        subagent_id = input_data.get("subagent_id")

        if session_id:
            log_msg = f"🤖 Subagent start: session {session_id}"
            if agent_id:
                log_msg += f", agent_id={agent_id}"
            if subagent_id:
                log_msg += f", subagent_id={subagent_id}"
            self.logger.debug(log_msg)
        else:
            self.logger.debug("🤖 Subagent start")

        return HookResponse(decision="allow")

    def _handle_event_subagent_stop(self, event: HookEvent) -> HookResponse:
        """
        Handle SUBAGENT_STOP event.

        Logs subagent termination event.

        Args:
            event: HookEvent

        Returns:
            HookResponse (always allow)
        """
        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug(f"🤖 Subagent stop: session {session_id}")
        else:
            self.logger.debug("🤖 Subagent stop")

        return HookResponse(decision="allow")

    def _handle_event_notification(self, event: HookEvent) -> HookResponse:
        """
        Handle NOTIFICATION event.

        Updates session status to paused.

        Args:
            event: HookEvent with notification details

        Returns:
            HookResponse (always allow)
        """
        input_data = event.data
        notification_type = (
            input_data.get("notification_type")
            or input_data.get("notificationType")
            or input_data.get("type")
            or "general"
        )
        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug(f"🔔 Notification ({notification_type}): session {session_id}")
            try:
                self._session_manager.update_session_status(session_id, "paused")
            except Exception as e:
                self.logger.warning(f"Failed to update session status: {e}")
        else:
            self.logger.debug(f"🔔 Notification ({notification_type})")

        return HookResponse(decision="allow")

    def _handle_event_permission_request(self, event: HookEvent) -> HookResponse:
        """
        Handle PERMISSION_REQUEST event (Claude Code only).

        Logs permission request. Future: workflow policy evaluation.

        Args:
            event: HookEvent with permission details

        Returns:
            HookResponse (allow by default)
        """
        input_data = event.data
        session_id = event.metadata.get("_platform_session_id")
        permission_type = input_data.get("permission_type", "unknown")

        if session_id:
            self.logger.debug(f"🔐 Permission request ({permission_type}): session {session_id}")
        else:
            self.logger.debug(f"🔐 Permission request ({permission_type})")

        # Future: workflow policy evaluation
        return HookResponse(decision="allow")

    def _handle_event_before_tool_selection(self, event: HookEvent) -> HookResponse:
        """
        Handle BEFORE_TOOL_SELECTION event (Gemini only).

        Allows modifying tool configuration before tool selection.

        Args:
            event: HookEvent with tool selection context

        Returns:
            HookResponse (allow by default)
        """
        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug(f"🔧 Before tool selection: session {session_id}")
        else:
            self.logger.debug("🔧 Before tool selection")

        return HookResponse(decision="allow")

    def _handle_event_before_model(self, event: HookEvent) -> HookResponse:
        """
        Handle BEFORE_MODEL event (Gemini only).

        Allows modifying LLM request before sending to model.

        Args:
            event: HookEvent with model request context

        Returns:
            HookResponse (allow by default)
        """
        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug(f"🧠 Before model: session {session_id}")
        else:
            self.logger.debug("🧠 Before model")

        return HookResponse(decision="allow")

    def _handle_event_after_model(self, event: HookEvent) -> HookResponse:
        """
        Handle AFTER_MODEL event (Gemini only).

        Called after model response received.

        Args:
            event: HookEvent with model response context

        Returns:
            HookResponse (always allow)
        """
        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug(f"🧠 After model: session {session_id}")
        else:
            self.logger.debug("🧠 After model")

        return HookResponse(decision="allow")
